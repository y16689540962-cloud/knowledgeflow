"""ProcessingPipeline 主链路（定稿文档第十六节）。"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
import yaml

from app.chunking.budget import TextBudget, apply_budget, chunk_chars_for
from app.chunking.chunker import chunk_text
from app.config import Settings
from app.db.repository import ContentRepository
from app.errors import ErrorType, LLMError
from app.logging_config import setup_logging
from app.obsidian.writer import ObsidianWriter
from app.pipeline.result import PIPELINE_STEPS, ProcessingResult, StepRecord
from app.pipeline.service import ProcessingPipeline
from app.providers import MockProvider
from app.schemas import RawContent
from app.testing import load_fixture_text
from app.testing.providers import ScriptedProvider

VALID_LLM_FIXTURE = "llm_analysis_valid.json"


class BrokenWriter(ObsidianWriter):
    """写入时抛未预期异常，用于验证「未预期异常也必须留痕」。"""

    def write_rendered(self, *args, **kwargs):  # type: ignore[override]
        raise RuntimeError("磁盘炸了")


def notes_in(vault_root: Path, kind: str) -> list[Path]:
    directory = vault_root / "KnowledgeFlow" / kind
    return sorted(directory.glob("*.md")) if directory.is_dir() else []


def processed(vault_root: Path) -> list[Path]:
    return notes_in(vault_root, "Processed")


def failed(vault_root: Path) -> list[Path]:
    return notes_in(vault_root, "Failed")


def frontmatter_of(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8").split("---\n")[1])


# --------------------------------------------------------------------------- #
# 成功路径
# --------------------------------------------------------------------------- #
async def test_happy_path(pipeline, mixed_raw_content) -> None:
    result = await pipeline.process(mixed_raw_content)

    assert result.outcome == "completed"
    assert result.success is True
    assert result.status == "completed"
    assert result.content_id
    assert result.analysis_id
    assert result.note_path
    assert result.prompt_version == "analysis_prompt_v1"
    assert result.model == "mock-llm-v1"
    assert result.chunk_count == 1
    assert result.claim_count == 6
    assert result.unverified_count == 3
    assert result.needs_manual_review is True
    assert result.error_type is None
    assert result.elapsed_ms >= 0


async def test_all_steps_recorded_in_order(pipeline, mixed_raw_content) -> None:
    result = await pipeline.process(mixed_raw_content)
    assert result.step_names() == PIPELINE_STEPS
    assert all(record.ok for record in result.steps)
    assert result.failed_step is None


async def test_step_details_are_filled(pipeline, mixed_raw_content) -> None:
    result = await pipeline.process(mixed_raw_content)
    details = {record.step: record.detail for record in result.steps}
    assert details["normalize"] and "source_id=" in details["normalize"]
    assert details["deduplicate"] == "无重复"
    assert details["persist_content"] and details["persist_content"].startswith("content_id=")
    assert details["analyze"] and "chunks=1" in details["analyze"]
    assert details["write_obsidian"] and details["write_obsidian"].endswith("(created)")


async def test_database_state_after_success(pipeline, repo: ContentRepository, mixed_raw_content) -> None:
    result = await pipeline.process(mixed_raw_content)
    row = await repo.get_content(result.content_id)
    assert row is not None
    assert row.status == "completed"
    assert row.processed_at
    assert row.current_analysis_id == result.analysis_id
    assert row.error_type is None
    assert row.source == "manual"
    assert row.source_id.startswith("hash:")
    assert row.raw_text

    analyses = await repo.list_analyses(result.content_id)
    assert len(analyses) == 1
    assert analyses[0].prompt_version == "analysis_prompt_v1"
    assert analyses[0].chunk_count == 1
    assert analyses[0].model == "mock-llm-v1"


async def test_note_written_to_processed(pipeline, mixed_raw_content, vault_root: Path) -> None:
    result = await pipeline.process(mixed_raw_content)
    notes = processed(vault_root)
    assert len(notes) == 1
    assert notes[0].name == "人口下降之后，房子还会涨吗.md"
    # vault 内的**逻辑**路径永远用 ``/``（与操作系统无关），所以这里写字面量
    # 而不是 ``os.path.join`` —— 后者在 Windows 上给反斜杠，会让同一条笔记
    # 在两个平台上「不同名」。理由见 app/obsidian/layout.py:namespace_path。
    assert result.note_path == f"KnowledgeFlow/Processed/{notes[0].name}"
    body = notes[0].read_text(encoding="utf-8")
    assert "# 人口下降之后，房子还会涨吗" in body
    assert "[[中国]]" in body


async def test_frontmatter_reflects_pipeline(pipeline, mixed_raw_content, vault_root: Path) -> None:
    await pipeline.process(mixed_raw_content)
    front = frontmatter_of(processed(vault_root)[0])
    assert front["source"] == "manual"
    assert front["needs_verification"] is True
    assert front["needs_manual_review"] is True
    assert front["created_at"].endswith("Z")
    assert front["processed_at"].endswith("Z")
    assert front["model"] == "mock-llm-v1"
    assert front["prompt_version"] == "analysis_prompt_v1"


async def test_created_at_is_not_rewritten(pipeline, mixed_raw_content, repo, vault_root: Path) -> None:
    result = await pipeline.process(mixed_raw_content)
    row = await repo.get_content(result.content_id)
    assert row is not None
    assert frontmatter_of(processed(vault_root)[0])["created_at"] == row.created_at


async def test_entities_and_topics_linked(pipeline, entities, topics, mixed_raw_content) -> None:
    result = await pipeline.process(mixed_raw_content)
    linked_entities = await entities.list_content_entities(result.content_id)
    assert {item.canonical_name for item in linked_entities} == {
        "中国",
        "房地产",
        "人口",
        "一线城市",
    }
    linked_topics = await topics.list_content_topics(result.content_id)
    assert {item.name for item in linked_topics} == {"人口结构", "房地产", "宏观经济"}
    assert result.entity_count == 4
    assert result.topic_count == 3


async def test_content_id_can_be_injected(pipeline, mixed_raw_content, repo) -> None:
    result = await pipeline.process(mixed_raw_content, content_id="injected0001")
    assert result.content_id == "injected0001"
    assert await repo.get_content("injected0001") is not None


async def test_result_to_dict_is_serializable(pipeline, mixed_raw_content) -> None:
    result = await pipeline.process(mixed_raw_content)
    payload = json.loads(json.dumps(result.to_dict(), ensure_ascii=False))
    assert payload["outcome"] == "completed"
    assert len(payload["steps"]) == len(PIPELINE_STEPS)


# --------------------------------------------------------------------------- #
# 去重（Phase 4 只做同 (source, source_id)）
# --------------------------------------------------------------------------- #
async def test_second_identical_input_is_duplicate(
    pipeline, repo: ContentRepository, mixed_raw_content, vault_root: Path
) -> None:
    first = await pipeline.process(mixed_raw_content)
    second = await pipeline.process(mixed_raw_content)

    assert first.outcome == "completed"
    assert second.outcome == "duplicate"
    assert second.duplicated is True
    assert second.duplicate_of == first.content_id
    assert second.content_id == first.content_id
    assert second.analysis_id == first.analysis_id
    assert second.note_path is None
    assert await repo.count_contents() == 1
    assert await repo.count_analyses(first.content_id) == 1
    assert len(processed(vault_root)) == 1


async def test_duplicate_short_circuits_before_persist(pipeline, mixed_raw_content) -> None:
    await pipeline.process(mixed_raw_content)
    second = await pipeline.process(mixed_raw_content)
    assert second.step_names() == ("preflight", "normalize", "deduplicate")


async def test_completed_content_is_not_reprocessed_when_payload_changes(
    pipeline, repo, raw_content
) -> None:
    """已完成的内容改标题再喂一次 → 仍然是 duplicate，不偷偷重跑。

    真要重跑必须显式 ``reprocess()``（第十三节：reprocess 才产生新 analyses 行）。
    用 ``raw_content``（有真 aweme_id）而不是手贴内容，否则改标题会改掉 fallback 出来的
    ``source_id``，那就变成另一条内容了。
    """
    first = await pipeline.process(raw_content)
    changed = raw_content.model_copy(update={"title": "改过的标题"})
    second = await pipeline.process(changed)

    assert second.outcome == "duplicate"
    assert second.content_id == first.content_id
    row = await repo.get_content(first.content_id)
    assert row is not None and row.title == raw_content.title


async def test_failed_content_payload_is_refreshed_on_retry(
    pipeline_factory, repo, raw_content, invalid_analysis_text: str, valid_analysis_payload: dict
) -> None:
    failing = pipeline_factory(ScriptedProvider([invalid_analysis_text] * 3))
    first = await failing.process(raw_content)
    assert first.outcome == "failed"

    recovering = pipeline_factory(MockProvider(payload=valid_analysis_payload))
    changed = raw_content.model_copy(update={"title": "修好后的标题"})
    second = await recovering.process(changed)

    assert second.outcome == "completed"
    assert second.content_id == first.content_id
    assert await repo.count_contents() == 1
    row = await repo.get_content(first.content_id)
    assert row is not None
    assert row.title == "修好后的标题"  # update_payload 生效
    assert row.status == "completed"


# --------------------------------------------------------------------------- #
# reprocess
# --------------------------------------------------------------------------- #
async def test_reprocess_adds_new_analysis_row(pipeline, repo, mixed_raw_content) -> None:
    first = await pipeline.process(mixed_raw_content)
    second = await pipeline.reprocess(first.content_id)

    assert second.outcome == "completed"
    assert second.content_id == first.content_id
    assert second.analysis_id != first.analysis_id
    assert await repo.count_analyses(first.content_id) == 2

    row = await repo.get_content(first.content_id)
    assert row is not None and row.current_analysis_id == second.analysis_id
    history = await repo.list_analyses(first.content_id)
    assert {item.id for item in history} == {first.analysis_id, second.analysis_id}


async def test_reprocess_records_load_step(pipeline, mixed_raw_content) -> None:
    first = await pipeline.process(mixed_raw_content)
    second = await pipeline.reprocess(first.content_id)
    assert second.step_names()[0] == "load_content"
    assert second.step_names()[1:] == PIPELINE_STEPS


async def test_reprocess_rewrites_same_note(pipeline, mixed_raw_content, vault_root: Path) -> None:
    first = await pipeline.process(mixed_raw_content)
    await pipeline.reprocess(first.content_id)
    assert len(processed(vault_root)) == 1


async def test_reprocess_missing_content(pipeline) -> None:
    result = await pipeline.reprocess("does-not-exist")
    assert result.outcome == "failed"
    assert result.error_type == ErrorType.CONTENT_NOT_FOUND.value
    assert result.failed_step == "load_content"
    assert result.content_id == "does-not-exist"


async def test_load_raw_rebuilds_raw_content(pipeline, mixed_raw_content) -> None:
    """从数据库行重建的 RawContent 必须带原文，否则失败笔记会是空的。"""
    first = await pipeline.process(mixed_raw_content)
    raw = await pipeline._load_raw(first.content_id)  # noqa: SLF001 - 就是在测这个重建
    assert isinstance(raw, RawContent)
    assert raw.source == "manual"
    assert raw.raw_text == mixed_raw_content.raw_text
    assert raw.media_path is None


# --------------------------------------------------------------------------- #
# 失败路径
# --------------------------------------------------------------------------- #
async def test_analysis_failure_marks_content_failed(
    pipeline_factory, repo, mixed_raw_content, invalid_analysis_text: str
) -> None:
    pipeline = pipeline_factory(ScriptedProvider([invalid_analysis_text] * 3))
    result = await pipeline.process(mixed_raw_content)

    assert result.outcome == "failed"
    assert result.failed is True
    assert result.status == "failed"
    assert result.error_type == "LLM_INVALID_OUTPUT"
    assert result.error_message
    assert result.failed_step == "analyze"
    assert result.analysis_id is None
    assert result.extra["failed_step"] == "analyze"

    row = await repo.get_content(result.content_id)
    assert row is not None
    assert row.status == "failed"
    assert row.error_type == "LLM_INVALID_OUTPUT"
    assert row.error_message
    assert row.current_analysis_id is None
    assert await repo.count_analyses(result.content_id) == 0


async def test_failure_still_writes_failed_note(
    pipeline_factory, mixed_raw_content, invalid_analysis_text: str, vault_root: Path
) -> None:
    pipeline = pipeline_factory(ScriptedProvider([invalid_analysis_text] * 3))
    result = await pipeline.process(mixed_raw_content)

    notes = failed(vault_root)
    assert len(notes) == 1
    body = notes[0].read_text(encoding="utf-8")
    assert "## 处理失败" in body
    assert "LLM_INVALID_OUTPUT" in body
    assert "中国人口正在下降" in body  # 原文保住，方便人工重试
    assert result.note_path and result.note_path.startswith("KnowledgeFlow/Failed")
    assert processed(vault_root) == []


async def test_failed_note_marks_manual_review(
    pipeline_factory, mixed_raw_content, invalid_analysis_text: str, vault_root: Path
) -> None:
    pipeline = pipeline_factory(ScriptedProvider([invalid_analysis_text] * 3))
    await pipeline.process(mixed_raw_content)
    assert frontmatter_of(failed(vault_root)[0])["needs_manual_review"] is True


async def test_preflight_fails_without_vault(db, mixed_raw_content, valid_analysis_payload, repo) -> None:
    """没有 vault 配置时**先**失败：不白烧一轮 LLM、也不留垃圾行。"""
    pipeline = ProcessingPipeline.from_database(
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
        provider=MockProvider(payload=valid_analysis_payload),
        database=db,
        vault_root=None,
    )
    result = await pipeline.process(mixed_raw_content)

    assert result.outcome == "failed"
    assert result.error_type == ErrorType.CONFIG_OBSIDIAN_VAULT_MISSING.value
    assert result.failed_step == "preflight"
    assert result.content_id is None
    assert await repo.count_contents() == 0


async def test_preflight_fails_when_vault_missing(
    db, mixed_raw_content, valid_analysis_payload, tmp_path: Path
) -> None:
    pipeline = ProcessingPipeline.from_database(
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
        provider=MockProvider(payload=valid_analysis_payload),
        database=db,
        vault_root=tmp_path / "nowhere",
    )
    result = await pipeline.process(mixed_raw_content)
    assert result.error_type == ErrorType.CONFIG_OBSIDIAN_VAULT_NOT_FOUND.value
    assert result.failed_step == "preflight"


async def test_unexpected_exception_is_recorded_not_swallowed(
    db, settings_defaults, vault_root, mixed_raw_content, valid_analysis_payload
) -> None:
    stream = io.StringIO()
    logger = setup_logging(stream=stream, level="DEBUG", logger_name="knowledgeflow.pipeline")
    pipeline = ProcessingPipeline.from_database(
        settings=settings_defaults,
        provider=MockProvider(payload=valid_analysis_payload),
        database=db,
        vault_root=vault_root,
        writer=BrokenWriter(vault_root),
        logger=logger,
    )
    result = await pipeline.process(mixed_raw_content)

    assert result.outcome == "failed"
    assert result.error_type == ErrorType.PIPELINE_STEP_FAILED.value
    assert result.failed_step == "write_obsidian"
    logged = stream.getvalue()
    assert "Traceback" in logged  # 未预期异常必须打堆栈
    assert "RuntimeError" in logged


async def test_unexpected_exception_still_marks_content_failed(
    db, settings_defaults, vault_root, mixed_raw_content, valid_analysis_payload, repo
) -> None:
    pipeline = ProcessingPipeline.from_database(
        settings=settings_defaults,
        provider=MockProvider(payload=valid_analysis_payload),
        database=db,
        vault_root=vault_root,
        writer=BrokenWriter(vault_root),
    )
    result = await pipeline.process(mixed_raw_content)
    row = await repo.get_content(result.content_id)
    assert row is not None and row.status == "failed"
    assert row.error_type == ErrorType.PIPELINE_STEP_FAILED.value


async def test_failure_note_write_error_does_not_mask_original(
    db, settings_defaults, vault_root, mixed_raw_content, valid_analysis_payload
) -> None:
    """连失败笔记都写不出来时，仍然返回**原始**错误类型。"""
    stream = io.StringIO()
    logger = setup_logging(stream=stream, level="DEBUG", logger_name="knowledgeflow.pipeline")
    pipeline = ProcessingPipeline.from_database(
        settings=settings_defaults,
        provider=MockProvider(payload=valid_analysis_payload),
        database=db,
        vault_root=vault_root,
        writer=BrokenWriter(vault_root),
        logger=logger,
    )
    result = await pipeline.process(mixed_raw_content)

    assert result.error_type == ErrorType.PIPELINE_STEP_FAILED.value
    assert result.note_path is None
    assert "写失败笔记失败" in stream.getvalue()


async def test_llm_transport_failure_keeps_error_type(
    db, settings_defaults, vault_root, mixed_raw_content, repo
) -> None:
    timeout = LLMError("模拟超时", error_type=ErrorType.LLM_TIMEOUT)
    pipeline = ProcessingPipeline.from_database(
        settings=settings_defaults,
        provider=ScriptedProvider([timeout] * 3),
        database=db,
        vault_root=vault_root,
    )
    result = await pipeline.process(mixed_raw_content)

    assert result.outcome == "failed"
    assert result.error_type == ErrorType.LLM_TIMEOUT.value
    row = await repo.get_content(result.content_id)
    assert row is not None and row.error_type == ErrorType.LLM_TIMEOUT.value


# --------------------------------------------------------------------------- #
# 别名 / 实体归一化
# --------------------------------------------------------------------------- #
async def test_alias_index_refreshed_after_run(pipeline, mixed_raw_content) -> None:
    """本轮新建的实体必须在下一轮 Grounding 前可见 —— 否则会一直用陈旧快照。

    断言用的是 **pipeline 自己那个** loader：只有它被 ``normalize_aliases`` 刷新过。
    """
    loader = pipeline.alias_loader
    before = await loader.load_index()
    assert before.canonical_of("中国") is None

    await pipeline.process(mixed_raw_content)

    after = await loader.load_index()  # 走缓存
    assert after.canonical_of("中国") == "中国"
    assert after.canonical_of("房地产") == "房地产"


async def test_normalize_aliases_does_not_register_canonical(
    pipeline, entities, alias_loader, mixed_raw_content
) -> None:
    """canonical 自身不写进 alias 表（alias 表只放额外表面形式）。"""
    await pipeline.process(mixed_raw_content)
    assert await entities.all_aliases() == []


async def test_seeded_alias_makes_alias_rows_stable(
    pipeline_factory, entities, alias_loader, mixed_raw_content, valid_analysis_payload
) -> None:
    """已登记的别名不会被重复登记，也不会被改写归属。"""
    owner = await entities.resolve_or_create("人工智能", "concept", aliases=["AI"])
    alias_loader.invalidate()
    before = await entities.all_aliases()

    payload = dict(valid_analysis_payload)
    payload["entities"] = [{"name": "AI", "type": "concept"}, {"name": "中国", "type": "place"}]
    pipeline = pipeline_factory(MockProvider(payload=payload))
    result = await pipeline.process(mixed_raw_content)

    assert result.outcome == "completed"
    assert await entities.all_aliases() == before
    assert await entities.aliases_for(owner.entity_id) == ("AI",)
    linked = await entities.list_content_entities(result.content_id)
    assert {item.canonical_name for item in linked} == {"人工智能", "中国"}


# --------------------------------------------------------------------------- #
# 超长输入
# --------------------------------------------------------------------------- #
async def test_long_input_goes_through_chunking(
    pipeline_factory, long_raw_content, repo
) -> None:
    budget = TextBudget(12000, 6000, 24000)
    prepared = apply_budget(long_raw_content, budget)
    expected = len(chunk_text(prepared.text, max_chars=chunk_chars_for(24000, 4000)))
    assert expected > 1

    pipeline = pipeline_factory(
        ScriptedProvider([load_fixture_text(VALID_LLM_FIXTURE)] * (expected + 1)),
        budget=budget,
    )
    result = await pipeline.process(long_raw_content)

    assert result.outcome == "completed"
    assert result.chunk_count == expected
    assert result.prompt_version == "synthesis_prompt_v1"
    row = await repo.get_content(result.content_id)
    assert row is not None and row.transcript
    analyses = await repo.list_analyses(result.content_id)
    assert analyses[0].chunk_count == expected


# --------------------------------------------------------------------------- #
# 结果模型
# --------------------------------------------------------------------------- #
def test_pipeline_steps_cover_section_16_chain() -> None:
    assert PIPELINE_STEPS == (
        "preflight",
        "normalize",
        "deduplicate",
        "persist_content",
        "prepare_text",
        "analyze",
        "persist_analysis",
        "extract_topics",
        "extract_entities",
        "normalize_aliases",
        "render_markdown",
        "write_obsidian",
        "complete",
    )


def test_result_helpers() -> None:
    result = ProcessingResult(
        outcome="failed",
        content_id="c1",
        steps=(
            StepRecord(step="a", status="ok", elapsed_ms=1.0),
            StepRecord(step="b", status="failed", elapsed_ms=1.0, error_type="X"),
        ),
        error_type="X",
    )
    assert result.failed is True
    assert result.success is False
    assert result.duplicated is False
    assert result.failed_step == "b"


@pytest.mark.parametrize(
    ("outcome", "success", "failed_", "duplicated"),
    [
        ("completed", True, False, False),
        ("failed", False, True, False),
        ("duplicate", False, False, True),
    ],
)
def test_result_outcome_helpers(
    outcome: str, success: bool, failed_: bool, duplicated: bool
) -> None:
    result = ProcessingResult(outcome=outcome, content_id="c1")  # type: ignore[arg-type]
    assert result.success is success
    assert result.failed is failed_
    assert result.duplicated is duplicated
