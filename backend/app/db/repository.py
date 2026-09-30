"""Repository 层：所有数据库读写的唯一入口。

关键点（定稿文档第四节强制）：

* reprocess **新增一行 analyses**，绝不 UPDATE 旧行。
* 唯一的「当前分析真源」是 ``contents.current_analysis_id``。
* 写新 analyses 行 + 更新 ``current_analysis_id`` 必须在**同一事务**内完成。
"""

from __future__ import annotations

from typing import Sequence

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.mappers import AnalysisInsert, ContentInsert
from app.db.models import Analysis, Content
from app.errors import DatabaseError
from app.utils import utc_now_iso


class ContentRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # ------------------------------------------------------------------ #
    # contents
    # ------------------------------------------------------------------ #
    async def insert_content(self, insert: ContentInsert) -> str:
        row = Content(
            id=insert.id,
            source=insert.source,
            source_id=insert.source_id,
            source_url=insert.source_url,
            title=insert.title,
            author=insert.author,
            author_id=insert.author_id,
            description=insert.description,
            media_type=insert.media_type,
            raw_text=insert.raw_text,
            transcript=insert.transcript,
            ocr_text=insert.ocr_text,
            content_hash=insert.content_hash,
            content_hash_version=insert.content_hash_version,
            needs_manual_review=insert.needs_manual_review,
            current_analysis_id=insert.current_analysis_id,
            status=insert.status,
            created_at=insert.created_at,
            processed_at=insert.processed_at,
            error_type=insert.error_type,
            error_message=insert.error_message,
        )
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    session.add(row)
        except IntegrityError as exc:
            raise DatabaseError(
                f"违反映唯一/非空约束（source={insert.source!r}, source_id={insert.source_id!r}）：{exc.orig}",
                context={"source": insert.source, "source_id": insert.source_id},
            ) from exc
        except SQLAlchemyError as exc:  # pragma: no cover - 兜底
            raise DatabaseError(str(exc)) from exc
        return insert.id

    async def get_content(self, content_id: str) -> Content | None:
        async with self._session_factory() as session:
            return await session.get(Content, content_id)

    async def get_content_by_source(self, source: str, source_id: str) -> Content | None:
        stmt = select(Content).where(Content.source == source, Content.source_id == source_id)
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def find_by_content_hash(
        self, content_hash: str, *, content_hash_version: int | None = None
    ) -> Sequence[Content]:
        stmt = select(Content).where(Content.content_hash == content_hash)
        if content_hash_version is not None:
            stmt = stmt.where(Content.content_hash_version == content_hash_version)
        stmt = stmt.order_by(Content.created_at, Content.id)
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return result.scalars().all()

    async def find_by_source_and_content_hash(
        self,
        source: str,
        content_hash: str,
        *,
        content_hash_version: int | None = None,
    ) -> Sequence[Content]:
        """**同 source 内**按 ``content_hash`` 找内容（Phase 5 去重）。

        为什么限定 source：定稿第七节的哈希白名单**含 ``source``**，
        所以跨来源（抖音 vs 手贴）的同一段文案本来就哈希不同 —— 那是定稿的定义，
        不在 Phase 5 的范围内（见进度文档 Q1）。

        ``content_hash_version`` 参与匹配：v1 的哈希不能拿来跟 v2 的行比。
        """
        stmt = select(Content).where(
            Content.source == source, Content.content_hash == content_hash
        )
        if content_hash_version is not None:
            stmt = stmt.where(Content.content_hash_version == content_hash_version)
        stmt = stmt.order_by(Content.created_at, Content.id)
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return result.scalars().all()

    async def count_contents(self, status: str | None = None) -> int:
        stmt = select(func.count()).select_from(Content)
        if status:
            stmt = stmt.where(Content.status == status)
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return int(result.scalar_one())

    async def list_page(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Content]:
        """按 ``created_at`` **倒序**分页（最新的在前）。

        ``list_by_status`` 只按状态全量拉，没有分页。列表接口若「先全量拉再在
        内存里切片」，库一大就白读一遍全表 —— 所以分页在 SQL 里做。
        ``limit`` / ``offset`` 做了下限保护：负值会被 SQLAlchemy 原样拼进
        ``LIMIT -1``，语义随驱动而异。
        """
        stmt = select(Content)
        if status:
            stmt = stmt.where(Content.status == status)
        stmt = (
            stmt.order_by(Content.created_at.desc(), Content.id)
            .limit(max(0, int(limit)))
            .offset(max(0, int(offset)))
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return result.scalars().all()

    async def list_by_status(self, status: str) -> Sequence[Content]:
        stmt = (
            select(Content)
            .where(Content.status == status)
            .order_by(Content.created_at, Content.id)
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return result.scalars().all()

    async def list_stale_status(self, status: str, cutoff_iso: str) -> Sequence[Content]:
        """列出 ``status`` 且 ``created_at <= cutoff_iso`` 的任务（超时回收用）。

        时间戳统一是 ``...Z`` 的 UTC ISO 字符串，格式一致，因此字典序比较即时间序。
        边界取**含**（``<=``）：时间戳只有秒级精度，排除边界会让
        ``timeout=0`` 这类退化配置永远收不到任何任务。
        """
        stmt = (
            select(Content)
            .where(Content.status == status, Content.created_at <= cutoff_iso)
            .order_by(Content.created_at, Content.id)
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return result.scalars().all()

    async def reset_to_pending(self, content_ids: Sequence[str]) -> tuple[str, ...]:
        """把一批任务重置为 ``pending``，并清掉上一次的失败痕迹（第十七节）。"""
        ids = [content_id for content_id in content_ids if content_id]
        if not ids:
            return ()
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    update(Content)
                    .where(Content.id.in_(ids))
                    .values(
                        status="pending",
                        error_type=None,
                        error_message=None,
                        processed_at=None,
                    )
                )
        return tuple(ids)

    async def update_payload(self, content_id: str, insert: ContentInsert) -> bool:
        """更新一条已存在内容的「内容字段」（重跑同一 source_id 时用）。

        刻意**不碰** ``id`` / ``created_at`` / ``status`` / ``current_analysis_id``、
        也不碰 ``analyses`` 历史。
        """
        values: dict[str, object] = {
            "source_url": insert.source_url,
            "title": insert.title,
            "author": insert.author,
            "author_id": insert.author_id,
            "description": insert.description,
            "media_type": insert.media_type,
            "raw_text": insert.raw_text,
            "transcript": insert.transcript,
            "ocr_text": insert.ocr_text,
            "content_hash": insert.content_hash,
            "content_hash_version": insert.content_hash_version,
            "needs_manual_review": insert.needs_manual_review,
        }
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    update(Content).where(Content.id == content_id).values(**values)
                )
        return bool(result.rowcount)

    async def update_status(
        self,
        content_id: str,
        status: str,
        *,
        error_type: str | None = None,
        error_message: str | None = None,
        processed_at: str | None = None,
    ) -> bool:
        values: dict[str, object] = {"status": status}
        if status in ("pending", "processing"):
            # 重新排队/开始处理：清掉上一次的失败痕迹（否则 processing 状态带着旧 error_type）
            values["error_type"] = None
            values["error_message"] = None
            values["processed_at"] = None
        else:
            values["error_type"] = error_type
            values["error_message"] = error_message
            values["processed_at"] = processed_at or utc_now_iso()

        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    update(Content).where(Content.id == content_id).values(**values)
                )
        return bool(result.rowcount)

    # ------------------------------------------------------------------ #
    # analyses
    # ------------------------------------------------------------------ #
    async def save_analysis_atomic(self, insert: AnalysisInsert) -> str:
        """同一事务内：插入新 analyses 行 → 更新 contents.current_analysis_id。

        任一步失败整体回滚，不会留下「有分析但没生效」或「生效但没分析」的状态。
        """
        async with self._session_factory() as session:
            async with session.begin():
                content = await session.get(Content, insert.content_id)
                if content is None:
                    raise DatabaseError(
                        f"content 不存在，无法写入分析：{insert.content_id}",
                        context={"content_id": insert.content_id},
                    )
                session.add(
                    Analysis(
                        id=insert.id,
                        content_id=insert.content_id,
                        analysis_type=insert.analysis_type,
                        summary=insert.summary,
                        structured_json=insert.structured_json,
                        model=insert.model,
                        prompt_version=insert.prompt_version,
                        chunk_count=insert.chunk_count,
                        created_at=insert.created_at,
                    )
                )
                await session.flush()
                content.current_analysis_id = insert.id
        return insert.id

    async def list_analyses(self, content_id: str) -> Sequence[Analysis]:
        stmt = (
            select(Analysis)
            .where(Analysis.content_id == content_id)
            .order_by(Analysis.created_at, Analysis.id)
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return result.scalars().all()

    async def count_analyses(self, content_id: str) -> int:
        stmt = select(func.count()).select_from(Analysis).where(Analysis.content_id == content_id)
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return int(result.scalar_one())

    async def get_current_analysis(self, content_id: str) -> Analysis | None:
        content = await self.get_content(content_id)
        if content is None or not content.current_analysis_id:
            return None
        async with self._session_factory() as session:
            return await session.get(Analysis, content.current_analysis_id)


__all__ = ["ContentRepository"]
