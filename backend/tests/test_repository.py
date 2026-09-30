"""Repository：写入、查询、状态机，以及「分析历史永不覆盖」（第四节，强制）。"""

from __future__ import annotations

import pytest

from app.db import AnalysisInsert, ContentInsert
from app.db.repository import ContentRepository
from app.errors import DatabaseError
from app.normalization import CONTENT_HASH_VERSION

from tests.conftest import insert_raw

ANALYSIS_JSON = '{"title": "t", "summary": "s", "analysis_type": "mixed", "claims": [], "overall_confidence": 0.5}'


def analysis(content_id: str, *, prompt_version: str = "analysis_prompt_v1", summary: str = "第一版") -> AnalysisInsert:
    return AnalysisInsert(
        content_id=content_id,
        structured_json=ANALYSIS_JSON,
        prompt_version=prompt_version,
        analysis_type="mixed",
        summary=summary,
        model="mock-llm-v1",
        chunk_count=1,
    )


# --------------------------------------------------------------------------- #
# contents
# --------------------------------------------------------------------------- #
async def test_insert_and_get(repo: ContentRepository, raw_content) -> None:
    content_id = await insert_raw(repo, raw_content)
    row = await repo.get_content(content_id)
    assert row is not None
    assert row.source == "douyin"
    assert row.source_id == "7321567890123456789"
    assert row.status == "pending"
    assert row.content_hash_version == 2
    assert row.needs_manual_review is False
    assert row.current_analysis_id is None
    assert row.processed_at is None


async def test_get_missing_returns_none(repo: ContentRepository) -> None:
    assert await repo.get_content("nope") is None


async def test_get_by_source(repo: ContentRepository, raw_content) -> None:
    await insert_raw(repo, raw_content)
    found = await repo.get_content_by_source("douyin", "7321567890123456789")
    assert found is not None
    assert await repo.get_content_by_source("douyin", "other") is None


async def test_find_by_content_hash(repo: ContentRepository, raw_content) -> None:
    content_id = await insert_raw(repo, raw_content)
    row = await repo.get_content(content_id)
    assert row is not None

    hits = await repo.find_by_content_hash(row.content_hash)
    assert [item.id for item in hits] == [content_id]

    assert await repo.find_by_content_hash(
        row.content_hash, content_hash_version=CONTENT_HASH_VERSION + 1
    ) == []


async def test_normalized_fields_persisted(repo: ContentRepository, mixed_raw_content) -> None:
    """手贴内容：source_id 为空 → fallback 落库并标记需人工复核。"""
    content_id = await insert_raw(repo, mixed_raw_content)
    row = await repo.get_content(content_id)
    assert row is not None
    assert row.source_id.startswith("hash:")
    assert row.needs_manual_review is True
    assert row.error_type == "SOURCE_ID_RESOLUTION_FAILED"
    assert row.source_url == "https://www.douyin.com/video/7300000000000000001"


async def test_status_machine(repo: ContentRepository, raw_content) -> None:
    content_id = await insert_raw(repo, raw_content)

    assert await repo.update_status(content_id, "processing") is True
    row = await repo.get_content(content_id)
    assert row is not None and row.status == "processing" and row.processed_at is None

    assert await repo.update_status(content_id, "completed") is True
    row = await repo.get_content(content_id)
    assert row is not None and row.status == "completed" and row.processed_at


async def test_failed_status_records_error(repo: ContentRepository, raw_content) -> None:
    content_id = await insert_raw(repo, raw_content)
    await repo.update_status(
        content_id, "failed", error_type="LLM_INVALID_OUTPUT", error_message="三次都解析失败"
    )
    row = await repo.get_content(content_id)
    assert row is not None
    assert row.status == "failed"
    assert row.error_type == "LLM_INVALID_OUTPUT"
    assert row.error_message == "三次都解析失败"
    assert row.processed_at


async def test_update_status_unknown_id_returns_false(repo: ContentRepository) -> None:
    assert await repo.update_status("nope", "processing") is False


async def test_count_contents(repo: ContentRepository, raw_content, mixed_raw_content) -> None:
    await insert_raw(repo, raw_content)
    await insert_raw(repo, mixed_raw_content)
    assert await repo.count_contents() == 2


# --------------------------------------------------------------------------- #
# analyses：保留历史
# --------------------------------------------------------------------------- #
async def test_save_analysis_sets_current(repo: ContentRepository, raw_content) -> None:
    content_id = await insert_raw(repo, raw_content)
    analysis_id = await repo.save_analysis_atomic(analysis(content_id))

    row = await repo.get_content(content_id)
    assert row is not None and row.current_analysis_id == analysis_id

    current = await repo.get_current_analysis(content_id)
    assert current is not None and current.id == analysis_id


async def test_reprocess_adds_new_row_and_keeps_history(repo: ContentRepository, raw_content) -> None:
    """第十二/十三节：reprocess 新增 analyses 行，历史完整保留。"""
    content_id = await insert_raw(repo, raw_content)

    first = await repo.save_analysis_atomic(analysis(content_id, prompt_version="analysis_prompt_v1"))
    second = await repo.save_analysis_atomic(
        analysis(content_id, prompt_version="analysis_prompt_v2", summary="第二版")
    )

    assert first != second
    assert await repo.count_analyses(content_id) == 2

    # 同一秒内写入，created_at 相同 —— 顺序不保证，按 id 建索引来断言「两行都在」。
    rows = {row.id: row for row in await repo.list_analyses(content_id)}
    assert set(rows) == {first, second}
    assert rows[first].prompt_version == "analysis_prompt_v1"
    assert rows[second].prompt_version == "analysis_prompt_v2"
    assert rows[first].summary == "第一版"

    row = await repo.get_content(content_id)
    assert row is not None and row.current_analysis_id == second

    current = await repo.get_current_analysis(content_id)
    assert current is not None and current.prompt_version == "analysis_prompt_v2"


async def test_old_analysis_row_is_untouched(repo: ContentRepository, raw_content) -> None:
    content_id = await insert_raw(repo, raw_content)
    first = await repo.save_analysis_atomic(analysis(content_id, summary="第一版"))
    await repo.save_analysis_atomic(
        analysis(content_id, prompt_version="analysis_prompt_v2", summary="第二版")
    )

    rows = {row.id: row for row in await repo.list_analyses(content_id)}
    assert rows[first].summary == "第一版"
    assert rows[first].prompt_version == "analysis_prompt_v1"


async def test_save_analysis_atomic_rolls_back_when_content_missing(
    repo: ContentRepository,
) -> None:
    """同一事务：content 不存在 → 新 analyses 行也不能落库。"""
    with pytest.raises(DatabaseError):
        await repo.save_analysis_atomic(analysis("ghost-content-id"))
    assert await repo.count_analyses("ghost-content-id") == 0


async def test_current_analysis_none_when_unset(repo: ContentRepository, raw_content) -> None:
    content_id = await insert_raw(repo, raw_content)
    assert await repo.get_current_analysis(content_id) is None


async def test_current_analysis_none_for_missing_content(repo: ContentRepository) -> None:
    assert await repo.get_current_analysis("nope") is None


async def test_analysis_json_round_trip(repo: ContentRepository, raw_content, valid_analysis_payload) -> None:
    import json

    from app.schemas import ContentAnalysis

    content_id = await insert_raw(repo, raw_content)
    payload = json.dumps(valid_analysis_payload, ensure_ascii=False)
    insert = AnalysisInsert(
        content_id=content_id,
        structured_json=payload,
        prompt_version="analysis_prompt_v1",
        analysis_type=valid_analysis_payload["analysis_type"],
        summary=valid_analysis_payload["summary"],
        chunk_count=1,
    )
    await repo.save_analysis_atomic(insert)

    current = await repo.get_current_analysis(content_id)
    assert current is not None
    restored = ContentAnalysis.model_validate(json.loads(current.structured_json))
    assert len(restored.claims) == len(valid_analysis_payload["claims"])


async def test_content_insert_dataclass_required_fields() -> None:
    insert = ContentInsert(id="i", source="s", source_id="sid", content_hash="h")
    assert insert.status == "pending"
    assert insert.content_hash_version == 1
    assert insert.created_at.endswith("Z")
    assert insert.needs_manual_review is False
