"""实体归一化（定稿文档第二十一节）。

**禁止用简单字符串替换实现**。必须走 alias 表查询 + 回退建表：

1. 按 ``canonical_name`` 精确查 → 命中则复用
2. 按 ``entity_aliases.alias`` 精确查 → 命中则回退到该 canonical 实体
3. 都没命中 → **回退建表**：新建 ``entities`` 行，并把当前名字登记为别名

alias 在库层面全局唯一（``uq_entity_aliases_alias``）。若一个别名已经属于别的实体，
**不抢、不改**，只返回冲突信息让人工处理。

canonical_name 本身不重复写进 alias 表：别名表只放「额外的表面形式」，
canonical 永远由 :meth:`EntityRegistry.aliases_for` 补上。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable, Sequence

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import ContentEntity, Entity, EntityAlias
from app.errors import DatabaseError, EntityNotFoundError, ErrorType, KnowledgeFlowError
from app.logging_config import log_event
from app.utils import new_id

if TYPE_CHECKING:  # pragma: no cover - 只供类型标注，避免运行时循环导入
    from app.knowledge.aliases import DatabaseAliasLoader

logger = logging.getLogger("knowledgeflow.knowledge")

DEFAULT_ENTITY_TYPE = "other"


class EntityRegistryError(KnowledgeFlowError):
    error_type = ErrorType.DATABASE_ERROR
    default_message = "实体归一化失败"


class EntityMergeError(EntityRegistryError):
    """合并请求本身不合法（合并到自己 / id 为空）。

    刻意从 :class:`EntityRegistryError` 继承：调用方只关心「归一化层出问题了」，
    但 ``error_type`` 换成 ``ENTITY_MERGE_INVALID`` —— 这是**请求的问题**，
    不该被算成数据库故障（500）。
    """

    error_type = ErrorType.ENTITY_MERGE_INVALID
    default_message = "实体合并请求不合法"


@dataclass(frozen=True)
class AliasConflict:
    alias: str
    existing_entity_id: str


@dataclass(frozen=True)
class AliasRegistration:
    alias: str
    entity_id: str
    created: bool
    conflict: AliasConflict | None = None


@dataclass(frozen=True)
class ResolvedEntity:
    entity_id: str
    canonical_name: str
    entity_type: str
    aliases: tuple[str, ...] = ()
    created: bool = False
    matched_by: str = "canonical"  # canonical | alias | created

    def as_linked(self):  # pragma: no cover - 仅为类型提示方便
        from app.obsidian.renderer import LinkedEntity

        return LinkedEntity(
            canonical_name=self.canonical_name,
            aliases=self.aliases,
            entity_type=self.entity_type,
        )


@dataclass(frozen=True)
class EntityLinkResult:
    content_id: str
    linked: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    conflicts: tuple[AliasConflict, ...] = ()


@dataclass(frozen=True)
class MergePlan:
    """``merge_entities`` 的**预演结果**：只算不写。

    合并是破坏性的（源实体会被删掉），所以先给出「会动什么、卡在哪」，
    让人在真跑之前就能看见冲突 —— 与 :meth:`EntityRegistry.add_alias`
    「冲突时报告而不是抢」是同一条语义。
    """

    source_id: str
    target_id: str
    source_canonical_name: str
    target_canonical_name: str
    #: 会迁到目标名下的表面形式（源 canonical + 源的别名）
    aliases_to_move: tuple[str, ...] = ()
    #: 已经属于目标、无需改动的表面形式
    aliases_owned: tuple[str, ...] = ()
    #: 被**第三方**实体占着、不会动的表面形式
    alias_conflicts: tuple[AliasConflict, ...] = ()
    #: 会重挂到目标名下的 ``content_entities`` 条数
    links_to_move: int = 0
    #: 目标已经挂了同一条内容、源那条会被删掉的条数
    links_to_drop: int = 0

    @property
    def blocked(self) -> bool:
        """有冲突 → 源实体**不会被删除**（保留现场，等人工处理）。"""
        return bool(self.alias_conflicts)


@dataclass(frozen=True)
class MergeResult:
    """一次合并**实际发生**了什么。"""

    source_id: str
    target_id: str
    source_canonical_name: str
    target_canonical_name: str
    aliases_moved: tuple[str, ...] = ()
    alias_conflicts: tuple[AliasConflict, ...] = ()
    links_moved: int = 0
    links_dropped: int = 0
    #: 源实体是否已删除。有别名冲突时为 ``False`` —— 不删，避免那个表面形式被静默丢掉。
    source_deleted: bool = False
    #: ``False`` 表示只做了预演（``dry_run=True``）
    applied: bool = True


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


class EntityRegistry:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        alias_loader: "DatabaseAliasLoader | None" = None,
    ) -> None:
        self._session_factory = session_factory
        #: 可选：合并会改别名表，合并后顺手让 :class:`DatabaseAliasLoader` 的快照失效，
        #: 避免下一次 Grounding 还拿着旧索引（「合并完了但链接没变」这种假象）。
        self._alias_loader = alias_loader

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    async def get_by_id(self, entity_id: str) -> Entity | None:
        async with self._session_factory() as session:
            result = await session.execute(select(Entity).where(Entity.id == entity_id))
            return result.scalar_one_or_none()

    async def get_by_canonical_name(self, name: str) -> Entity | None:
        stmt = select(Entity).where(Entity.canonical_name == _clean(name))
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def find_by_alias(self, alias: str) -> tuple[Entity, ...]:
        """走 alias 表查询（不是字符串替换）。"""
        stmt = (
            select(Entity)
            .join(EntityAlias, EntityAlias.entity_id == Entity.id)
            .where(EntityAlias.alias == _clean(alias))
            .order_by(Entity.canonical_name)
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return tuple(result.scalars().all())

    async def aliases_for(self, entity_id: str) -> tuple[str, ...]:
        stmt = (
            select(EntityAlias.alias)
            .where(EntityAlias.entity_id == entity_id)
            .order_by(EntityAlias.alias)
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return tuple(result.scalars().all())

    async def all_aliases(self) -> list[tuple[str, str]]:
        """返回 ``(canonical_name, alias)`` 全量列表。"""
        stmt = (
            select(Entity.canonical_name, EntityAlias.alias)
            .join(EntityAlias, EntityAlias.entity_id == Entity.id)
            .order_by(Entity.canonical_name, EntityAlias.alias)
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return [(row[0], row[1]) for row in result.all()]

    async def count(self) -> int:
        stmt = select(Entity.id)
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return len(result.scalars().all())

    # ------------------------------------------------------------------ #
    # 回退建表
    # ------------------------------------------------------------------ #
    async def add_alias(self, entity_id: str, alias: str) -> AliasRegistration:
        """登记一个别名。已属于别的实体时**不抢**，只报冲突。"""
        cleaned = _clean(alias)
        if not cleaned:
            raise EntityRegistryError("别名不能为空")

        try:
            async with self._session_factory() as session:
                async with session.begin():
                    existing = await session.execute(
                        select(EntityAlias).where(EntityAlias.alias == cleaned)
                    )
                    row = existing.scalar_one_or_none()
                    if row is not None:
                        if row.entity_id == entity_id:
                            return AliasRegistration(
                                alias=cleaned, entity_id=entity_id, created=False
                            )
                        return AliasRegistration(
                            alias=cleaned,
                            entity_id=entity_id,
                            created=False,
                            conflict=AliasConflict(alias=cleaned, existing_entity_id=row.entity_id),
                        )
                    session.add(
                        EntityAlias(id=new_id(), entity_id=entity_id, alias=cleaned)
                    )
        except SQLAlchemyError as exc:  # pragma: no cover - 兜底
            raise EntityRegistryError(str(exc)) from exc
        return AliasRegistration(alias=cleaned, entity_id=entity_id, created=True)

    async def resolve_or_create(
        self,
        name: str,
        entity_type: str = DEFAULT_ENTITY_TYPE,
        *,
        aliases: Iterable[str] = (),
    ) -> ResolvedEntity:
        """查询 → 别名回退 → 建表。"""
        cleaned = _clean(name)
        if not cleaned:
            raise EntityRegistryError("实体名不能为空")
        cleaned_type = _clean(entity_type) or DEFAULT_ENTITY_TYPE

        entity = await self.get_by_canonical_name(cleaned)
        matched_by = "canonical"
        created = False

        if entity is None:
            by_alias = await self.find_by_alias(cleaned)
            if by_alias:
                entity = by_alias[0]
                matched_by = "alias"

        if entity is None:
            entity = await self._create_entity(cleaned, cleaned_type)
            matched_by = "created"
            created = True

        conflicts: list[AliasConflict] = []
        for alias in aliases:
            cleaned_alias = _clean(alias)
            if not cleaned_alias or cleaned_alias == entity.canonical_name:
                continue
            registration = await self.add_alias(entity.id, cleaned_alias)
            if registration.conflict is not None:
                conflicts.append(registration.conflict)
                log_event(
                    logger,
                    logging.WARNING,
                    stage="knowledge",
                    message="别名已被其他实体占用，已跳过",
                    alias=cleaned_alias,
                    entity_id=registration.conflict.existing_entity_id,
                )

        registered = await self.aliases_for(entity.id)
        return ResolvedEntity(
            entity_id=entity.id,
            canonical_name=entity.canonical_name,
            entity_type=entity.entity_type,
            aliases=registered,
            created=created,
            matched_by=matched_by,
        )

    async def _create_entity(self, canonical_name: str, entity_type: str) -> Entity:
        row = Entity(
            id=new_id(),
            canonical_name=canonical_name,
            entity_type=entity_type,
            description=None,
        )
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    session.add(row)
        except IntegrityError:
            # 并发/重复插入：回读已存在的行，不静默吞掉
            existing = await self.get_by_canonical_name(canonical_name)
            if existing is None:  # pragma: no cover - 理论上不可达
                raise EntityRegistryError(f"实体创建失败且回读不到：{canonical_name}")
            return existing
        except SQLAlchemyError as exc:  # pragma: no cover - 兜底
            raise EntityRegistryError(str(exc)) from exc
        return row

    async def resolve_many(
        self,
        items: Sequence[tuple[str, str]],
    ) -> tuple[ResolvedEntity, ...]:
        """``[(name, entity_type), ...]`` → 归一化结果（保序、按 canonical 去重）。"""
        seen: set[str] = set()
        resolved: list[ResolvedEntity] = []
        for name, entity_type in items:
            cleaned = _clean(name)
            if not cleaned:
                continue
            entity = await self.resolve_or_create(cleaned, entity_type)
            if entity.entity_id in seen:
                continue
            seen.add(entity.entity_id)
            resolved.append(entity)
        return tuple(resolved)

    # ------------------------------------------------------------------ #
    # content_entities
    # ------------------------------------------------------------------ #
    async def link_content(
        self,
        content_id: str,
        links: Sequence[tuple[str, float | None]],
    ) -> EntityLinkResult:
        """写 ``content_entities``（幂等；已存在则更新 confidence）。"""
        linked: list[str] = []
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    for entity_id, confidence in links:
                        statement = sqlite_insert(ContentEntity).values(
                            content_id=content_id,
                            entity_id=entity_id,
                            confidence=confidence,
                        )
                        statement = statement.on_conflict_do_update(
                            index_elements=["content_id", "entity_id"],
                            set_={"confidence": confidence},
                        )
                        await session.execute(statement)
                        linked.append(entity_id)
        except SQLAlchemyError as exc:
            raise DatabaseError(str(exc)) from exc
        return EntityLinkResult(content_id=content_id, linked=tuple(linked))

    async def list_content_entities(self, content_id: str) -> tuple[ResolvedEntity, ...]:
        stmt = (
            select(Entity)
            .join(ContentEntity, ContentEntity.entity_id == Entity.id)
            .where(ContentEntity.content_id == content_id)
            .order_by(Entity.canonical_name)
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            entities = list(result.scalars().all())

        if not entities:
            return ()

        alias_stmt = (
            select(EntityAlias.entity_id, EntityAlias.alias)
            .where(EntityAlias.entity_id.in_([entity.id for entity in entities]))
            .order_by(EntityAlias.alias)
        )
        async with self._session_factory() as session:
            alias_rows = await session.execute(alias_stmt)
            grouped: dict[str, list[str]] = {}
            for entity_id, alias in alias_rows.all():
                grouped.setdefault(entity_id, []).append(alias)

        return tuple(
            ResolvedEntity(
                entity_id=entity.id,
                canonical_name=entity.canonical_name,
                entity_type=entity.entity_type,
                aliases=tuple(grouped.get(entity.id, ())),
            )
            for entity in entities
        )

    # ------------------------------------------------------------------ #
    # 合并（别名迁移 + 归属重挂）
    # ------------------------------------------------------------------ #
    async def _require_entity(self, entity_id: str, role: str) -> Entity:
        entity = await self.get_by_id(entity_id)
        if entity is None:
            raise EntityNotFoundError(
                f"{role}实体不存在：{entity_id}", context={"entity_id": entity_id}
            )
        return entity

    async def _alias_owners(self, names: Sequence[str]) -> dict[str, str]:
        """``表面形式 → 目前占着它的 entity_id``（查不到就是没有行）。"""
        cleaned = [name for name in (_clean(raw) for raw in names) if name]
        if not cleaned:
            return {}
        stmt = select(EntityAlias.alias, EntityAlias.entity_id).where(
            EntityAlias.alias.in_(cleaned)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).all()
        return {alias: entity_id for alias, entity_id in rows}

    async def _links_of(self, entity_id: str) -> list[tuple[str, float | None]]:
        stmt = select(ContentEntity.content_id, ContentEntity.confidence).where(
            ContentEntity.entity_id == entity_id
        )
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).all()
        return [(row[0], row[1]) for row in rows]

    async def plan_merge(self, source_id: str, target_id: str) -> MergePlan:
        """预演一次合并：**只算不写**。

        合并会删掉源实体，所以先看清楚会动什么。别名冲突（该名字已被第三方占着）
        与 :meth:`add_alias` 同语义：不抢、不改，只报告。
        """
        if not source_id or not target_id:
            raise EntityMergeError("实体 id 不能为空")
        if source_id == target_id:
            raise EntityMergeError("不能把实体合并到它自己")

        source = await self._require_entity(source_id, "源")
        target = await self._require_entity(target_id, "目标")

        surface_forms = [source.canonical_name, *await self.aliases_for(source.id)]
        owners = await self._alias_owners(surface_forms)

        to_move: list[str] = []
        owned: list[str] = []
        conflicts: list[AliasConflict] = []
        for raw in surface_forms:
            name = _clean(raw)
            # 等于目标 canonical 的别名是冗余的（canonical 不进 alias 表）
            if not name or name == target.canonical_name:
                continue
            if name in to_move or name in owned:
                continue
            current = owners.get(name)
            if current is None or current == source.id:
                to_move.append(name)
            elif current == target.id:
                owned.append(name)
            else:
                conflicts.append(AliasConflict(alias=name, existing_entity_id=current))

        source_links = await self._links_of(source.id)
        target_contents = {content_id for content_id, _ in await self._links_of(target.id)}
        move = sum(1 for content_id, _ in source_links if content_id not in target_contents)

        return MergePlan(
            source_id=source.id,
            target_id=target.id,
            source_canonical_name=source.canonical_name,
            target_canonical_name=target.canonical_name,
            aliases_to_move=tuple(to_move),
            aliases_owned=tuple(owned),
            alias_conflicts=tuple(conflicts),
            links_to_move=move,
            links_to_drop=len(source_links) - move,
        )

    async def merge_entities(
        self,
        source_id: str,
        target_id: str,
        *,
        dry_run: bool = False,
        delete_source: bool = True,
    ) -> MergeResult:
        """把 ``source`` 合并进 ``target``（在一个事务里做完）。

        做的三件事：

        1. **别名迁移**：源 canonical 名与源的别名挂到目标名下（源 canonical
           必须迁，否则这个说法就查不到了）；
        2. **归属重挂**：``content_entities`` 从源改挂到目标；
           目标已经挂了同一条内容时删掉源那条，不产生重复；
        3. **删源实体**；但**只要有别名冲突就不删** ——
           冲突的那个表面形式没迁成，删了源它就彻底查不到了。留着现场给人工处理。

        ``dry_run=True`` 只返回 :meth:`plan_merge` 的等价结果，不写库。
        """
        plan = await self.plan_merge(source_id, target_id)
        if dry_run:
            return MergeResult(
                source_id=plan.source_id,
                target_id=plan.target_id,
                source_canonical_name=plan.source_canonical_name,
                target_canonical_name=plan.target_canonical_name,
                aliases_moved=plan.aliases_to_move,
                alias_conflicts=plan.alias_conflicts,
                links_moved=plan.links_to_move,
                links_dropped=plan.links_to_drop,
                source_deleted=False,
                applied=False,
            )

        source_links = await self._links_of(source_id)
        target_contents = {content_id for content_id, _ in await self._links_of(target_id)}

        moved: list[str] = []
        conflicts: list[AliasConflict] = list(plan.alias_conflicts)
        links_moved = 0
        links_dropped = 0
        source_deleted = False

        try:
            async with self._session_factory() as session:
                async with session.begin():
                    for name in plan.aliases_to_move:
                        row = (
                            await session.execute(
                                select(EntityAlias).where(EntityAlias.alias == name)
                            )
                        ).scalar_one_or_none()
                        if row is None:
                            session.add(
                                EntityAlias(id=new_id(), entity_id=target_id, alias=name)
                            )
                        elif row.entity_id == source_id:
                            await session.execute(
                                update(EntityAlias)
                                .where(EntityAlias.id == row.id)
                                .values(entity_id=target_id)
                            )
                        else:
                            # 计划时还没被占、执行这一刻被抢了：同样不抢，只报告。
                            conflicts.append(
                                AliasConflict(alias=name, existing_entity_id=row.entity_id)
                            )
                            log_event(
                                logger,
                                logging.WARNING,
                                stage="knowledge",
                                message="合并时别名已被其他实体占用，已跳过",
                                alias=name,
                                entity_id=row.entity_id,
                            )
                            continue
                        moved.append(name)

                    for content_id, _confidence in source_links:
                        if content_id in target_contents:
                            await session.execute(
                                delete(ContentEntity).where(
                                    ContentEntity.content_id == content_id,
                                    ContentEntity.entity_id == source_id,
                                )
                            )
                            links_dropped += 1
                        else:
                            await session.execute(
                                update(ContentEntity)
                                .where(
                                    ContentEntity.content_id == content_id,
                                    ContentEntity.entity_id == source_id,
                                )
                                .values(entity_id=target_id)
                            )
                            links_moved += 1

                    if delete_source and not conflicts:
                        # 剩下的别名行都是「等于目标 canonical」的冗余项，随源一起走。
                        await session.execute(
                            delete(EntityAlias).where(EntityAlias.entity_id == source_id)
                        )
                        await session.execute(delete(Entity).where(Entity.id == source_id))
                        source_deleted = True
        except SQLAlchemyError as exc:
            raise DatabaseError(str(exc)) from exc

        if conflicts:
            log_event(
                logger,
                logging.WARNING,
                stage="knowledge",
                message="实体合并存在别名冲突，源实体已保留",
                source_id=source_id,
                target_id=target_id,
                conflict_count=len(conflicts),
            )

        if self._alias_loader is not None:
            # 别名表变了，快照必须失效 —— 否则下一次 Grounding 还在用旧索引。
            self._alias_loader.invalidate()

        return MergeResult(
            source_id=source_id,
            target_id=target_id,
            source_canonical_name=plan.source_canonical_name,
            target_canonical_name=plan.target_canonical_name,
            aliases_moved=tuple(moved),
            alias_conflicts=tuple(conflicts),
            links_moved=links_moved,
            links_dropped=links_dropped,
            source_deleted=source_deleted,
            applied=True,
        )

    async def merge_by_name(
        self,
        source_name: str,
        target_name: str,
        **kwargs: object,
    ) -> MergeResult:
        """按名字合并（canonical 优先，其次别名回退）。查不到就报 ``ENTITY_NOT_FOUND``。"""
        source_id = await self._resolve_id(source_name)
        target_id = await self._resolve_id(target_name)
        return await self.merge_entities(source_id, target_id, **kwargs)  # type: ignore[arg-type]

    async def _resolve_id(self, name: str) -> str:
        cleaned = _clean(name)
        if not cleaned:
            raise EntityRegistryError("实体名不能为空")
        entity = await self.get_by_canonical_name(cleaned)
        if entity is None:
            by_alias = await self.find_by_alias(cleaned)
            if by_alias:
                entity = by_alias[0]
        if entity is None:
            raise EntityNotFoundError(f"实体不存在：{cleaned}", context={"name": cleaned})
        return entity.id

    async def list_entities(self) -> tuple[ResolvedEntity, ...]:
        """全量实体（含别名），按 canonical 排序。"""
        stmt = select(Entity).order_by(Entity.canonical_name)
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).scalars().all()
        if not rows:
            return ()
        alias_stmt = select(EntityAlias.entity_id, EntityAlias.alias).order_by(EntityAlias.alias)
        async with self._session_factory() as session:
            alias_rows = (await session.execute(alias_stmt)).all()
        raw: dict[str, list[str]] = {}
        for entity_id, alias in alias_rows:
            raw.setdefault(entity_id, []).append(alias)
        grouped = {entity_id: tuple(values) for entity_id, values in raw.items()}
        return tuple(
            ResolvedEntity(
                entity_id=row.id,
                canonical_name=row.canonical_name,
                entity_type=row.entity_type,
                aliases=grouped.get(row.id, ()),
            )
            for row in rows
        )


__all__ = [
    "EntityRegistry",
    "EntityRegistryError",
    "EntityMergeError",
    "ResolvedEntity",
    "AliasConflict",
    "AliasRegistration",
    "EntityLinkResult",
    "MergePlan",
    "MergeResult",
    "DEFAULT_ENTITY_TYPE",
]
