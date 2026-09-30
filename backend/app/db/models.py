"""数据库表结构（定稿文档第四节）。

强制点：

* ``contents.source_id`` ``NOT NULL`` + ``UNIQUE(source, source_id)``
* ``content_hash_version INTEGER NOT NULL DEFAULT 1``
* ``analyses`` 保留历史版本；当前生效版本只由 ``contents.current_analysis_id`` 决定
  （**不在 analyses 上放 ``is_current``，避免第二个真源**）
* 禁止出现 ``content_type`` 列（``media_type`` 与 ``analysis_type`` 是两个概念）
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Content(Base):
    __tablename__ = "contents"
    # 用「具名唯一索引」而不是匿名 UNIQUE 约束：SQLite 会为表内 UNIQUE 约束生成
    # sqlite_autoindex_* 内部索引名，具名索引则能在 PRAGMA index_list 里被直接验证。
    # 语义等价（都拒绝重复的 (source, source_id)）。
    __table_args__ = (
        Index("uq_contents_source_source_id", "source", "source_id", unique=True),
        Index("ix_contents_content_hash", "content_hash"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(Text)
    author_id: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    media_type: Mapped[str | None] = mapped_column(Text)
    raw_text: Mapped[str | None] = mapped_column(Text)
    transcript: Mapped[str | None] = mapped_column(Text)
    ocr_text: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    needs_manual_review: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    current_analysis_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    processed_at: Mapped[str | None] = mapped_column(Text)
    error_type: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)


class Analysis(Base):
    """历史版本容器：reprocess 只新增行，永不 UPDATE 旧行。"""

    __tablename__ = "analyses"
    __table_args__ = (Index("ix_analyses_content_id", "content_id"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    content_id: Mapped[str] = mapped_column(Text, ForeignKey("contents.id"), nullable=False)
    analysis_type: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    structured_json: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class Entity(Base):
    __tablename__ = "entities"
    __table_args__ = (Index("uq_entities_canonical_name", "canonical_name", unique=True),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)


class EntityAlias(Base):
    """别名表：``AI / 人工智能 / Artificial Intelligence → 人工智能``。

    别名必须全局唯一，否则 alias 查询无法确定唯一 canonical entity。
    """

    __tablename__ = "entity_aliases"
    __table_args__ = (
        Index("uq_entity_aliases_alias", "alias", unique=True),
        Index("ix_entity_aliases_entity_id", "entity_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    entity_id: Mapped[str] = mapped_column(Text, ForeignKey("entities.id"), nullable=False)
    alias: Mapped[str] = mapped_column(Text, nullable=False)


class ContentEntity(Base):
    __tablename__ = "content_entities"
    __table_args__ = (PrimaryKeyConstraint("content_id", "entity_id", name="pk_content_entities"),)

    content_id: Mapped[str] = mapped_column(Text, ForeignKey("contents.id"), nullable=False)
    entity_id: Mapped[str] = mapped_column(Text, ForeignKey("entities.id"), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)


class Topic(Base):
    __tablename__ = "topics"
    __table_args__ = (Index("uq_topics_name", "name", unique=True),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)


class ContentTopic(Base):
    __tablename__ = "content_topics"
    __table_args__ = (PrimaryKeyConstraint("content_id", "topic_id", name="pk_content_topics"),)

    content_id: Mapped[str] = mapped_column(Text, ForeignKey("contents.id"), nullable=False)
    topic_id: Mapped[str] = mapped_column(Text, ForeignKey("topics.id"), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)


#: 第四节定义的全部表名。
ALL_TABLES: tuple[str, ...] = (
    "contents",
    "analyses",
    "entities",
    "entity_aliases",
    "content_entities",
    "topics",
    "content_topics",
)


__all__ = [
    "Base",
    "Content",
    "Analysis",
    "Entity",
    "EntityAlias",
    "ContentEntity",
    "Topic",
    "ContentTopic",
    "ALL_TABLES",
]
