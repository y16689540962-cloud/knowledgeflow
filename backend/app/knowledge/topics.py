"""主题归一化（定稿文档第四节 ``topics`` / ``content_topics``）。

与实体同样的模式：先查（``name`` 唯一）→ 没有就建；关联表用幂等 upsert。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import ContentTopic, Topic
from app.errors import DatabaseError, ErrorType, KnowledgeFlowError
from app.utils import new_id


class TopicRegistryError(KnowledgeFlowError):
    error_type = ErrorType.DATABASE_ERROR
    default_message = "主题归一化失败"


@dataclass(frozen=True)
class ResolvedTopic:
    topic_id: str
    name: str
    created: bool = False


@dataclass(frozen=True)
class TopicLinkResult:
    content_id: str
    linked: tuple[str, ...] = ()


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


class TopicRegistry:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_by_name(self, name: str) -> Topic | None:
        stmt = select(Topic).where(Topic.name == _clean(name))
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def resolve_or_create(self, name: str) -> ResolvedTopic:
        cleaned = _clean(name)
        if not cleaned:
            raise TopicRegistryError("主题名不能为空")

        existing = await self.get_by_name(cleaned)
        if existing is not None:
            return ResolvedTopic(topic_id=existing.id, name=existing.name, created=False)

        row = Topic(id=new_id(), name=cleaned)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    session.add(row)
        except IntegrityError:
            again = await self.get_by_name(cleaned)
            if again is None:  # pragma: no cover - 理论上不可达
                raise TopicRegistryError(f"主题创建失败且回读不到：{cleaned}")
            return ResolvedTopic(topic_id=again.id, name=again.name, created=False)
        except SQLAlchemyError as exc:  # pragma: no cover - 兜底
            raise TopicRegistryError(str(exc)) from exc
        return ResolvedTopic(topic_id=row.id, name=row.name, created=True)

    async def resolve_many(self, names: Sequence[str]) -> tuple[ResolvedTopic, ...]:
        seen: set[str] = set()
        resolved: list[ResolvedTopic] = []
        for name in names:
            cleaned = _clean(name)
            if not cleaned:
                continue
            topic = await self.resolve_or_create(cleaned)
            if topic.topic_id in seen:
                continue
            seen.add(topic.topic_id)
            resolved.append(topic)
        return tuple(resolved)

    async def link_content(
        self,
        content_id: str,
        links: Sequence[tuple[str, float | None]],
    ) -> TopicLinkResult:
        linked: list[str] = []
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    for topic_id, confidence in links:
                        statement = sqlite_insert(ContentTopic).values(
                            content_id=content_id,
                            topic_id=topic_id,
                            confidence=confidence,
                        )
                        statement = statement.on_conflict_do_update(
                            index_elements=["content_id", "topic_id"],
                            set_={"confidence": confidence},
                        )
                        await session.execute(statement)
                        linked.append(topic_id)
        except SQLAlchemyError as exc:
            raise DatabaseError(str(exc)) from exc
        return TopicLinkResult(content_id=content_id, linked=tuple(linked))

    async def list_content_topics(self, content_id: str) -> tuple[ResolvedTopic, ...]:
        stmt = (
            select(Topic)
            .join(ContentTopic, ContentTopic.topic_id == Topic.id)
            .where(ContentTopic.content_id == content_id)
            .order_by(Topic.name)
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return tuple(
                ResolvedTopic(topic_id=topic.id, name=topic.name)
                for topic in result.scalars().all()
            )


__all__ = ["TopicRegistry", "TopicRegistryError", "ResolvedTopic", "TopicLinkResult"]
