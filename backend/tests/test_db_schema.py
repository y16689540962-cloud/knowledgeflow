"""数据库建表、约束与默认值（第四节，强制）。"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db import ALL_TABLES, Database, to_async_database_url
from app.db.repository import ContentRepository
from app.errors import DatabaseError
from app.db.mappers import ContentInsert

from tests.conftest import insert_raw


async def rows(conn: AsyncConnection, sql: str) -> list[dict[str, Any]]:
    result = await conn.exec_driver_sql(sql)
    return [dict(row._mapping) for row in result]


async def table_info(db: Database, table: str) -> dict[str, dict[str, Any]]:
    async with db.engine.connect() as conn:
        return {row["name"]: row for row in await rows(conn, f"PRAGMA table_info({table})")}


async def index_names(db: Database, table: str) -> set[str]:
    async with db.engine.connect() as conn:
        return {row["name"] for row in await rows(conn, f"PRAGMA index_list({table})")}


def column(
    info: dict[str, dict[str, Any]], name: str, table: str
) -> dict[str, Any]:
    assert name in info, f"{table}.{name} 缺失，实际列：{sorted(info)}"
    return info[name]


# --------------------------------------------------------------------------- #
# 表结构
# --------------------------------------------------------------------------- #
async def test_all_tables_created(db: Database) -> None:
    async with db.engine.connect() as conn:
        existing = {
            row["name"]
            for row in await rows(conn, "SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert set(ALL_TABLES).issubset(existing)


async def test_no_content_type_column_anywhere(db: Database) -> None:
    """禁止用 content_type 表示两个不同概念。"""
    async with db.engine.connect() as conn:
        for table in ALL_TABLES:
            columns = {row["name"] for row in await rows(conn, f"PRAGMA table_info({table})")}
            assert "content_type" not in columns, table


async def test_contents_columns_match_spec(db: Database) -> None:
    info = await table_info(db, "contents")
    expected = {
        "id",
        "source",
        "source_id",
        "source_url",
        "title",
        "author",
        "author_id",
        "description",
        "media_type",
        "raw_text",
        "transcript",
        "ocr_text",
        "content_hash",
        "content_hash_version",
        "needs_manual_review",
        "current_analysis_id",
        "status",
        "created_at",
        "processed_at",
        "error_type",
        "error_message",
    }
    assert set(info) == expected


async def test_contents_not_null_columns(db: Database) -> None:
    info = await table_info(db, "contents")
    for name in ("id", "source", "source_id", "content_hash", "content_hash_version", "needs_manual_review", "status", "created_at"):
        assert column(info, name, "contents")["notnull"] == 1, name


async def test_source_id_is_not_null(db: Database) -> None:
    """第五节强制：source_id 必须 NOT NULL。"""
    info = await table_info(db, "contents")
    assert column(info, "source_id", "contents")["notnull"] == 1


async def test_nullable_columns(db: Database) -> None:
    info = await table_info(db, "contents")
    for name in ("source_url", "title", "author", "processed_at", "error_type", "error_message", "current_analysis_id"):
        assert column(info, name, "contents")["notnull"] == 0, name


async def test_content_hash_version_default_is_one(db: Database) -> None:
    info = await table_info(db, "contents")
    row = column(info, "content_hash_version", "contents")
    assert row["notnull"] == 1
    assert row["dflt_value"] in ("1", "'1'", 1)


async def test_needs_manual_review_default_false(db: Database) -> None:
    info = await table_info(db, "contents")
    row = column(info, "needs_manual_review", "contents")
    assert row["notnull"] == 1
    assert str(row["dflt_value"]).strip("'") == "0"


async def test_unique_source_source_id_constraint_exists(db: Database) -> None:
    names = await index_names(db, "contents")
    assert "uq_contents_source_source_id" in names


async def test_content_hash_index_exists(db: Database) -> None:
    names = await index_names(db, "contents")
    assert "ix_contents_content_hash" in names


async def test_content_hash_index_covers_column(db: Database) -> None:
    async with db.engine.connect() as conn:
        infos = await rows(conn, "PRAGMA index_info(ix_contents_content_hash)")
    assert [row["name"] for row in infos] == ["content_hash"]


async def test_analyses_columns_match_spec(db: Database) -> None:
    info = await table_info(db, "analyses")
    assert set(info) == {
        "id",
        "content_id",
        "analysis_type",
        "summary",
        "structured_json",
        "model",
        "prompt_version",
        "chunk_count",
        "created_at",
    }


async def test_analyses_has_no_is_current_column(db: Database) -> None:
    """避免出现第二个「当前分析」真源。"""
    info = await table_info(db, "analyses")
    assert "is_current" not in info


async def test_analyses_not_null_columns(db: Database) -> None:
    info = await table_info(db, "analyses")
    for name in ("id", "content_id", "structured_json", "prompt_version", "chunk_count", "created_at"):
        assert column(info, name, "analyses")["notnull"] == 1, name


async def test_analyses_content_id_foreign_key(db: Database) -> None:
    async with db.engine.connect() as conn:
        fks = await rows(conn, "PRAGMA foreign_key_list(analyses)")
    assert any(fk["table"] == "contents" and fk["from"] == "content_id" for fk in fks)


async def test_entity_tables_columns(db: Database) -> None:
    entities = await table_info(db, "entities")
    assert set(entities) == {"id", "canonical_name", "entity_type", "description"}

    aliases = await table_info(db, "entity_aliases")
    assert set(aliases) == {"id", "entity_id", "alias"}

    assert set(await table_info(db, "content_entities")) == {"content_id", "entity_id", "confidence"}
    assert set(await table_info(db, "topics")) == {"id", "name"}
    assert set(await table_info(db, "content_topics")) == {"content_id", "topic_id", "confidence"}


async def test_alias_is_unique(db: Database) -> None:
    """别名必须全局唯一，否则 alias → canonical 的解析不唯一。"""
    assert "uq_entity_aliases_alias" in await index_names(db, "entity_aliases")


async def test_canonical_name_is_unique(db: Database) -> None:
    assert "uq_entities_canonical_name" in await index_names(db, "entities")


# --------------------------------------------------------------------------- #
# 约束行为
# --------------------------------------------------------------------------- #
async def test_duplicate_source_id_rejected(db: Database, repo: ContentRepository, raw_content) -> None:
    await insert_raw(repo, raw_content)
    with pytest.raises(DatabaseError) as excinfo:
        await insert_raw(repo, raw_content)
    assert excinfo.value.error_type_value == "DATABASE_ERROR"
    assert await repo.count_contents() == 1


async def test_same_source_id_different_source_allowed(db: Database, repo: ContentRepository, raw_content) -> None:
    await insert_raw(repo, raw_content)
    other = raw_content.model_copy(update={"source": "bilibili"})
    await insert_raw(repo, other)
    assert await repo.count_contents() == 2


async def test_direct_insert_violating_unique_raises_integrity_error(db: Database) -> None:
    from sqlalchemy.exc import IntegrityError

    from app.db.models import Content

    async with db.session_factory() as session:
        async with session.begin():
            session.add(
                Content(
                    id="a",
                    source="s",
                    source_id="same",
                    content_hash="h",
                    created_at="2026-01-01T00:00:00Z",
                    status="pending",
                )
            )
        # 让异常从 begin() 块内抛出，由 begin() 自己回滚并原样重抛。
        with pytest.raises(IntegrityError):
            async with session.begin():
                session.add(
                    Content(
                        id="b",
                        source="s",
                        source_id="same",
                        content_hash="h",
                        created_at="2026-01-01T00:00:00Z",
                        status="pending",
                    )
                )
                await session.flush()


async def test_orm_defaults_applied(db: Database, repo: ContentRepository, raw_content) -> None:
    """不显式传 content_hash_version / needs_manual_review 时，应落默认值。"""
    from app.db.models import Content

    async with db.session_factory() as session:
        async with session.begin():
            session.add(
                Content(
                    id="defaults",
                    source="s",
                    source_id="1",
                    content_hash="h",
                    created_at="2026-01-01T00:00:00Z",
                    status="pending",
                )
            )
    row = await repo.get_content("defaults")
    assert row is not None
    assert row.content_hash_version == 1
    assert row.needs_manual_review is False


async def test_url_normalization_helper() -> None:
    assert to_async_database_url("sqlite:///./a.db") == "sqlite+aiosqlite:///./a.db"
    assert to_async_database_url("sqlite+pysqlite:///./a.db") == "sqlite+aiosqlite:///./a.db"
    assert to_async_database_url("sqlite+aiosqlite:///./a.db") == "sqlite+aiosqlite:///./a.db"


async def test_insert_content_requires_unique(
    db: Database, repo: ContentRepository, raw_content
) -> None:
    insert = ContentInsert(
        id="x1",
        source="douyin",
        source_id="aweme-1",
        content_hash="hash-1",
    )
    await repo.insert_content(insert)
    duplicate = ContentInsert(
        id="x2",
        source="douyin",
        source_id="aweme-1",
        content_hash="hash-2",
    )
    with pytest.raises(DatabaseError):
        await repo.insert_content(duplicate)
