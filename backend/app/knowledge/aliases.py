"""数据库驱动的别名解析（第二十一节 ↔ 第十节 R2.1 的接缝）。

Phase 2 的 Grounding Check 走的是**同步**协议
（:class:`app.grounding.aliases.AliasResolver`），而数据库访问是异步的 ——
两者不能直接对接。

做法：**一次性把 ``entities + entity_aliases`` 读成内存索引**，再把其中的
``canonical → aliases`` 映射交给同步的 Grounding Check。这样

* 协议不用改成 async（不反向污染 Phase 2）
* 每条 claim 的多次查询不会退化成 N 次数据库往返
* Phase 4 的顺序是「load_index() → 跑 Grounding → 写库」

索引里的 canonical **直接来自数据库**（``entities.canonical_name``），
不做任何「谁的别名更长谁是 canonical」这类猜测。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Entity, EntityAlias
from app.grounding.aliases import StaticAliasResolver
from app.grounding.folding import fold_for_matching
from app.obsidian.renderer import LinkedEntity


@dataclass(frozen=True)
class AliasIndex:
    """一次快照的全部结果。"""

    #: canonical → 别名（不含 canonical 自身）
    groups: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: 折叠后的表面形式 → canonical（canonical 名与别名都在里面）
    lookup: dict[str, str] = field(default_factory=dict)

    def resolver(self) -> StaticAliasResolver:
        return StaticAliasResolver(groups=self.groups)

    def canonical_of(self, name: str) -> str | None:
        return self.lookup.get(fold_for_matching(name))

    def aliases_of(self, canonical: str) -> tuple[str, ...]:
        return self.groups.get(canonical, ())


@dataclass
class DatabaseAliasLoader:
    session_factory: async_sessionmaker[AsyncSession]
    _index: AliasIndex | None = field(default=None, init=False, repr=False)

    async def load_index(self, *, use_cache: bool = True) -> AliasIndex:
        """读取别名索引。

        ``use_cache=False`` 表示**强制重建并更新缓存** ——
        调用方用它来「刷新」：重建之后缓存里不该还是旧的。
        """
        if use_cache and self._index is not None:
            return self._index

        index = await self._build_index()
        self._index = index
        return index

    async def _build_index(self) -> AliasIndex:
        statement = (
            select(Entity.canonical_name, EntityAlias.alias)
            .outerjoin(EntityAlias, EntityAlias.entity_id == Entity.id)
            .order_by(Entity.canonical_name, EntityAlias.alias)
        )
        async with self.session_factory() as session:
            result = await session.execute(statement)
            rows = result.all()

        groups: dict[str, list[str]] = {}
        lookup: dict[str, str] = {}
        for canonical_name, alias in rows:
            bucket = groups.setdefault(canonical_name, [])
            lookup.setdefault(fold_for_matching(canonical_name), canonical_name)
            if alias:
                bucket.append(alias)
                lookup.setdefault(fold_for_matching(alias), canonical_name)

        return AliasIndex(
            groups={canonical: tuple(aliases) for canonical, aliases in groups.items()},
            lookup=lookup,
        )

    def invalidate(self) -> None:
        self._index = None

    async def snapshot(self, *, use_cache: bool = True) -> StaticAliasResolver:
        """给 Grounding Check 用的**同步**解析器。"""
        return (await self.load_index(use_cache=use_cache)).resolver()

    async def aliases_for(self, canonical_name: str, *, use_cache: bool = True) -> frozenset[str]:
        index = await self.load_index(use_cache=use_cache)
        canonical = index.canonical_of(canonical_name) or canonical_name
        return frozenset({canonical, *index.aliases_of(canonical)})

    async def linked_entities(
        self,
        items: list[tuple[str, str]],
        *,
        use_cache: bool = True,
    ) -> tuple[LinkedEntity, ...]:
        """``[(name, entity_type), ...]`` → 渲染用的 :class:`LinkedEntity`。

        走 alias 表回退：名字命中别名时，链接指向它的 canonical。
        库里查不到的名字原样保留（链接仍然生成，只是没有别名信息）。
        """
        index = await self.load_index(use_cache=use_cache)
        linked: list[LinkedEntity] = []
        seen: set[str] = set()

        for name, entity_type in items:
            cleaned = " ".join((name or "").split())
            if not cleaned:
                continue
            canonical = index.canonical_of(cleaned) or cleaned
            if canonical in seen:
                continue
            seen.add(canonical)
            extras = tuple(
                alias for alias in index.aliases_of(canonical) if alias != canonical
            )
            linked.append(
                LinkedEntity(
                    canonical_name=canonical,
                    aliases=extras,
                    entity_type=entity_type or "other",
                )
            )
        return tuple(linked)


__all__ = ["DatabaseAliasLoader", "AliasIndex"]
