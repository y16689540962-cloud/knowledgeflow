"""Phase 1 端到端验收：fixture → (Mock 桩) → Pydantic → SQLite。

第三十一节验收要求：

* ``python -m pytest`` 全部通过
* 完成 ``fixture → (Mock 桩) → Pydantic → SQLite`` 的基础落库与约束验证
* 不依赖真实 LLM / 抖音 / Whisper / OCR / 网络
"""

from __future__ import annotations

import json

import pytest

from app.db.repository import ContentRepository
from app.normalization import normalize_content
from app.providers import MockProvider
from app.schemas import ContentAnalysis, RawContent
from app.testing import load_fixture_json, load_fixture_text


async def run_pipeline_fixture(
    repo: ContentRepository,
    *,
    raw_fixture: str,
    analysis_fixture: str,
    content_id: str = "phase1-acceptance",
) -> tuple[str, ContentAnalysis | None]:
    """Phase 1 能跑到的最远一段链路（没有 LLM 调用、没有 Markdown、没有 Obsidian）。"""
    raw = RawContent.from_fixture(load_fixture_json(raw_fixture))
    normalized = normalize_content(raw)

    from app.db.mappers import ContentInsert

    insert = ContentInsert.from_normalized(normalized, content_id=content_id, status="processing")
    await repo.insert_content(insert)

    provider = MockProvider(payload=load_fixture_json(analysis_fixture))
    raw_output = await provider.analyze(raw.source_text(), schema={})
    analysis = ContentAnalysis.model_validate(raw_output)

    from app.db.mappers import AnalysisInsert

    await repo.save_analysis_atomic(
        AnalysisInsert(
            content_id=content_id,
            structured_json=json.dumps(raw_output, ensure_ascii=False),
            prompt_version="analysis_prompt_v1",
            analysis_type=analysis.analysis_type,
            summary=analysis.summary,
            model=provider.model_name,
            chunk_count=1,
        )
    )
    await repo.update_status(content_id, "completed")
    return content_id, analysis


async def test_fixture_to_sqlite_happy_path(repo: ContentRepository) -> None:
    content_id, analysis = await run_pipeline_fixture(
        repo, raw_fixture="raw_content_mixed.json", analysis_fixture="llm_analysis_valid.json"
    )
    assert analysis is not None

    row = await repo.get_content(content_id)
    assert row is not None
    assert row.status == "completed"
    assert row.processed_at
    assert row.source == "manual"
    assert row.source_id.startswith("hash:")
    assert row.needs_manual_review is True
    assert row.content_hash_version == 2
    assert row.current_analysis_id

    current = await repo.get_current_analysis(content_id)
    assert current is not None
    assert current.prompt_version == "analysis_prompt_v1"
    assert current.model == "mock-llm-v1"
    assert current.chunk_count == 1
    assert json.loads(current.structured_json)["analysis_type"] == "mixed"


async def test_fixture_douyin_video_to_sqlite(repo: ContentRepository) -> None:
    content_id, _ = await run_pipeline_fixture(
        repo,
        raw_fixture="raw_content.json",
        analysis_fixture="llm_analysis_valid.json",
        content_id="douyin-video-1",
    )
    row = await repo.get_content(content_id)
    assert row is not None
    assert row.source == "douyin"
    assert row.source_id == "7321567890123456789"
    assert row.needs_manual_review is False


async def test_duplicate_input_does_not_create_duplicate_content(repo: ContentRepository) -> None:
    """第三十二节最后一步：重复输入 → 不产生重复内容（由 UNIQUE(source, source_id) 保证）。"""
    from app.errors import DatabaseError

    await run_pipeline_fixture(
        repo,
        raw_fixture="raw_content.json",
        analysis_fixture="llm_analysis_valid.json",
        content_id="dup-1",
    )
    with pytest.raises(DatabaseError):
        await run_pipeline_fixture(
            repo,
            raw_fixture="raw_content.json",
            analysis_fixture="llm_analysis_valid.json",
            content_id="dup-2",
        )
    assert await repo.count_contents() == 1


async def test_same_manual_content_dedupes_by_hash(repo: ContentRepository) -> None:
    """同一份文案两次手动粘贴（都没有平台 ID）→ source_id 都走 ``hash:`` fallback → 去重生效。"""
    from app.db.mappers import ContentInsert
    from app.errors import DatabaseError

    raw = RawContent.from_fixture(load_fixture_json("raw_content_mixed.json"))
    other_link = raw.model_copy(update={"source_id": "", "source_url": "https://v.douyin.com/xyz"})

    first = normalize_content(raw)
    second = normalize_content(other_link)
    assert first.source_id == second.source_id
    assert first.content_hash == second.content_hash

    await repo.insert_content(ContentInsert.from_normalized(first, content_id="c1"))
    with pytest.raises(DatabaseError):
        await repo.insert_content(ContentInsert.from_normalized(second, content_id="c2"))


async def test_invalid_llm_output_never_reaches_sqlite(repo: ContentRepository) -> None:
    """Pydantic 拒绝的输出，绝不允许落库。"""
    from app.db.mappers import ContentInsert
    from pydantic import ValidationError

    raw = RawContent.from_fixture(load_fixture_json("raw_content_mixed.json"))
    await repo.insert_content(ContentInsert.from_normalized(normalize_content(raw), content_id="r1"))

    payload = load_fixture_json("llm_analysis_invalid.json")
    with pytest.raises(ValidationError):
        ContentAnalysis.model_validate(payload)

    assert await repo.count_analyses("r1") == 0


async def test_legacy_arrays_never_reach_sqlite(repo: ContentRepository) -> None:
    from pydantic import ValidationError

    payload = load_fixture_json("llm_analysis_valid.json")
    payload["facts"] = payload["claims"]
    with pytest.raises(ValidationError):
        ContentAnalysis.model_validate(payload)


async def test_fenced_output_requires_phase2_repair(repo: ContentRepository) -> None:
    """Phase 1 的诚实结论：fenced 输出当前无法被解析，Phase 2 才做修复。"""
    provider = MockProvider(raw_text=load_fixture_text("llm_analysis_fenced.json"))
    with pytest.raises(json.JSONDecodeError):
        await provider.analyze("x", schema={})


async def test_long_input_still_persists_without_chunking(repo: ContentRepository) -> None:
    """超长输入在 Phase 1 不切分，但必须能完整落库（chunking 属于 Phase 2）。"""
    from app.db.mappers import ContentInsert

    raw = RawContent.from_fixture(load_fixture_json("raw_content_long.json"))
    normalized = normalize_content(raw)
    await repo.insert_content(ContentInsert.from_normalized(normalized, content_id="long-1"))

    row = await repo.get_content("long-1")
    assert row is not None
    assert len(row.transcript or "") > 12000
    assert row.content_hash_version == 2
