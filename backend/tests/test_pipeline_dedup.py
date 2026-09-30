"""Phase 5 · Deduplication：同 source + content_hash 去重。

口径（用户 2026-09-30 确认）：**同 source 内按 `content_hash` 去重，跨来源不合并。**
理由：定稿第七节的哈希白名单含 `source`，抖音与手贴的同一段文案本来就哈希不同。

判重顺序：先认精确身份 `(source, source_id)`，再认同 source 内的内容指纹 `content_hash`。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.db.repository import ContentRepository
from app.normalization import CONTENT_HASH_VERSION, normalize_content
from app.pipeline.service import DEDUP_BY_CONTENT_HASH, DEDUP_BY_SOURCE_ID
from app.providers import MockProvider
from app.testing import load_fixture_json, load_fixture_text
from app.testing.providers import ScriptedProvider

VALID_LLM_FIXTURE = "llm_analysis_valid.json"


def valid_provider() -> MockProvider:
    return MockProvider(payload=load_fixture_json(VALID_LLM_FIXTURE))


def notes_in(vault_root: Path, kind: str) -> list[Path]:
    directory = vault_root / "KnowledgeFlow" / kind
    return sorted(directory.glob("*.md")) if directory.is_dir() else []


def rebrand(raw, **overrides):
    """改「内容身份」字段 —— 会改变 content_hash。"""
    return raw.model_copy(update=overrides)


# --------------------------------------------------------------------------- #
# 仓库层：同 source 内按 content_hash 查
# --------------------------------------------------------------------------- #
async def test_find_by_source_and_content_hash(repo: ContentRepository, raw_content) -> None:
    normalized = normalize_content(raw_content)
    from tests.conftest import insert_raw

    content_id = await insert_raw(repo, raw_content, content_id="hash-lookup-1")
    hits = await repo.find_by_source_and_content_hash(
        normalized.source,
        normalized.content_hash,
        content_hash_version=CONTENT_HASH_VERSION,
    )
    assert [row.id for row in hits] == [content_id]


async def test_hash_lookup_is_scoped_to_source(repo: ContentRepository, raw_content) -> None:
    """同 hash 但 source 不同 → 不返回（跨来源不合并）。"""
    normalized = normalize_content(raw_content)
    from tests.conftest import insert_raw

    await insert_raw(repo, raw_content, content_id="hash-lookup-2")
    hits = await repo.find_by_source_and_content_hash(
        "manual", normalized.content_hash, content_hash_version=CONTENT_HASH_VERSION
    )
    assert hits == []


async def test_hash_lookup_respects_version(repo: ContentRepository, raw_content) -> None:
    normalized = normalize_content(raw_content)
    from tests.conftest import insert_raw

    await insert_raw(repo, raw_content, content_id="hash-lookup-3")
    hits = await repo.find_by_source_and_content_hash(
        normalized.source,
        normalized.content_hash,
        # 「未来版本」—— 写着 2 会在第七节再次递增版本号时变成假绿，所以取当前值 +1。
        content_hash_version=CONTENT_HASH_VERSION + 1,
    )
    assert hits == []


async def test_hash_lookup_without_version_filter(repo: ContentRepository, raw_content) -> None:
    normalized = normalize_content(raw_content)
    from tests.conftest import insert_raw

    await insert_raw(repo, raw_content, content_id="hash-lookup-4")
    hits = await repo.find_by_source_and_content_hash(normalized.source, normalized.content_hash)
    assert len(hits) == 1


# --------------------------------------------------------------------------- #
# 同 source + content_hash → duplicate
# --------------------------------------------------------------------------- #
async def test_same_source_different_id_same_content_is_duplicate(
    pipeline_factory, repo: ContentRepository, raw_content, vault_root: Path
) -> None:
    """同一条抖音视频的两个不同 aweme_id（标题/作者/简介一致）→ 第二次数出 duplicate。"""
    provider = ScriptedProvider([load_fixture_text(VALID_LLM_FIXTURE)])
    pipeline = pipeline_factory(provider)

    first = await pipeline.process(raw_content)
    second = await pipeline.process(raw_content.model_copy(update={"source_id": "9999999999999"}))

    assert first.outcome == "completed"
    assert second.outcome == "duplicate"
    assert second.deduplicated_by == DEDUP_BY_CONTENT_HASH
    assert second.duplicate_of == first.content_id
    assert second.analysis_id == first.analysis_id
    assert second.note_path is None

    # 只调了一次 LLM、只落了一行、只有一份笔记
    assert provider.call_count == 1
    assert await repo.count_contents() == 1
    assert len(notes_in(vault_root, "Processed")) == 1


async def test_exact_source_id_wins_over_content_hash(pipeline, raw_content) -> None:
    """精确身份优先：``deduplicated_by`` 应该是 ``source_id``。"""
    first = await pipeline.process(raw_content)
    second = await pipeline.process(raw_content)
    assert second.deduplicated_by == DEDUP_BY_SOURCE_ID
    assert second.duplicate_of == first.content_id


async def test_manual_paste_dedupes_by_source_id(pipeline, mixed_raw_content) -> None:
    """手贴内容的 source_id 是 hash: 兜底来的，所以判重依据是 source_id。"""
    await pipeline.process(mixed_raw_content)
    second = await pipeline.process(mixed_raw_content)
    assert second.outcome == "duplicate"
    assert second.deduplicated_by == DEDUP_BY_SOURCE_ID


async def test_hash_match_is_reported_in_to_dict(pipeline, raw_content) -> None:
    await pipeline.process(raw_content)
    second = await pipeline.process(raw_content.model_copy(update={"source_id": "888888"}))
    assert second.to_dict()["deduplicated_by"] == DEDUP_BY_CONTENT_HASH


async def test_no_duplicate_short_circuits_steps(pipeline, raw_content) -> None:
    await pipeline.process(raw_content)
    second = await pipeline.process(raw_content.model_copy(update={"source_id": "777777"}))
    assert second.step_names() == ("preflight", "normalize", "deduplicate")


# --------------------------------------------------------------------------- #
# 不判重的情形
# --------------------------------------------------------------------------- #
async def test_different_source_is_not_deduplicated(
    pipeline_factory, repo: ContentRepository, raw_content, mixed_raw_content
) -> None:
    """跨来源**不**合并（Q1 已定）：抖音 vs 手贴的同一段文案各自成条。"""
    pipeline = pipeline_factory(MockProvider(payload=load_fixture_json(VALID_LLM_FIXTURE)))
    first = await pipeline.process(raw_content)
    same_text_manual = raw_content.model_copy(update={"source": "manual", "source_id": ""})
    second = await pipeline.process(same_text_manual)

    assert first.outcome == "completed"
    assert second.outcome == "completed"
    assert second.content_id != first.content_id
    assert await repo.count_contents() == 2


async def test_different_title_is_not_deduplicated(
    pipeline_factory, repo: ContentRepository, raw_content
) -> None:
    """哈希白名单含 title：标题不同 → 内容指纹不同 → 不判重。"""
    pipeline = pipeline_factory(MockProvider(payload=load_fixture_json(VALID_LLM_FIXTURE)))
    first = await pipeline.process(raw_content)
    changed = rebrand(raw_content, title="换了个标题", source_id="666666")
    second = await pipeline.process(changed)

    assert second.outcome == "completed"
    assert second.content_id != first.content_id
    assert await repo.count_contents() == 2


async def test_different_description_is_not_deduplicated(
    pipeline_factory, repo: ContentRepository, raw_content
) -> None:
    pipeline = pipeline_factory(MockProvider(payload=load_fixture_json(VALID_LLM_FIXTURE)))
    first = await pipeline.process(raw_content)
    changed = rebrand(raw_content, description="完全不同的简介", source_id="555555")
    second = await pipeline.process(changed)
    assert second.content_id != first.content_id


async def test_different_author_is_not_deduplicated(
    pipeline_factory, repo: ContentRepository, raw_content
) -> None:
    pipeline = pipeline_factory(MockProvider(payload=load_fixture_json(VALID_LLM_FIXTURE)))
    first = await pipeline.process(raw_content)
    changed = rebrand(raw_content, author="另一个作者", source_id="444444")
    second = await pipeline.process(changed)
    assert second.content_id != first.content_id


async def test_same_content_but_other_hash_version_is_not_deduplicated(
    pipeline_factory, repo: ContentRepository, raw_content, db
) -> None:
    """哈希算法版本不同 → 不能混着比（定稿第七节：换算法递增版本号，旧数据不重算）。"""
    from sqlalchemy import update

    from app.db.models import Content

    provider = ScriptedProvider([load_fixture_text(VALID_LLM_FIXTURE)] * 2)
    pipeline = pipeline_factory(provider)
    first = await pipeline.process(raw_content)

    async with db.session_factory() as session:
        async with session.begin():
            await session.execute(
                update(Content).where(Content.id == first.content_id).values(
                    content_hash_version=CONTENT_HASH_VERSION + 1
                )
            )

    second = await pipeline.process(raw_content.model_copy(update={"source_id": "333333"}))
    assert second.outcome == "completed"
    assert second.content_id != first.content_id


# --------------------------------------------------------------------------- #
# 哈希命中但尚未完成 → 复用那一行继续处理（不新建、不重复出笔记）
# --------------------------------------------------------------------------- #
async def test_hash_match_on_failed_row_is_reused_and_retried(
    pipeline_factory, repo: ContentRepository, raw_content, invalid_analysis_text: str
) -> None:
    failing = pipeline_factory(ScriptedProvider([invalid_analysis_text] * 3))
    first = await failing.process(raw_content)
    assert first.outcome == "failed"

    recovering = pipeline_factory(
        MockProvider(payload=load_fixture_json(VALID_LLM_FIXTURE))
    )
    second = await recovering.process(raw_content.model_copy(update={"source_id": "222222"}))

    assert second.outcome == "completed"
    # 复用第一行（它的 source_id 是首次见到的那个），不新建
    assert second.content_id == first.content_id
    assert await repo.count_contents() == 1

    row = await repo.get_content(first.content_id)
    assert row is not None
    assert row.source_id == raw_content.source_id  # 身份没有被后来的 id 顶掉
    assert row.status == "completed"


async def test_failed_row_reuse_does_not_duplicate_notes(
    pipeline_factory, raw_content, invalid_analysis_text: str, vault_root: Path
) -> None:
    failing = pipeline_factory(ScriptedProvider([invalid_analysis_text] * 3))
    await failing.process(raw_content)
    assert len(notes_in(vault_root, "Failed")) == 1

    recovering = pipeline_factory(
        MockProvider(payload=load_fixture_json(VALID_LLM_FIXTURE))
    )
    await recovering.process(raw_content.model_copy(update={"source_id": "111111"}))

    assert len(notes_in(vault_root, "Processed")) == 1
    assert len(notes_in(vault_root, "Failed")) == 1


# --------------------------------------------------------------------------- #
# reprocess 不受去重影响
# --------------------------------------------------------------------------- #
async def test_force_reprocess_ignores_hash_dedup(pipeline, repo, raw_content) -> None:
    first = await pipeline.process(raw_content)
    second = await pipeline.reprocess(first.content_id)

    assert second.outcome == "completed"
    assert second.content_id == first.content_id
    assert await repo.count_analyses(first.content_id) == 2


async def test_reprocess_of_hash_matched_row(pipeline, raw_content, repo) -> None:
    first = await pipeline.process(raw_content)
    duplicate = await pipeline.process(raw_content.model_copy(update={"source_id": "121212"}))
    assert duplicate.outcome == "duplicate"

    # 显式重跑那一条
    assert duplicate.content_id == first.content_id
    again = await pipeline.reprocess(duplicate.content_id)  # type: ignore[arg-type]
    assert again.outcome == "completed"
    assert await repo.count_analyses(first.content_id) == 2


# --------------------------------------------------------------------------- #
# 已知限制：定稿的哈希口径只能做到这一步
# --------------------------------------------------------------------------- #
async def test_same_author_same_title_is_treated_as_same_content(
    pipeline_factory, repo: ContentRepository, raw_content
) -> None:
    """**已知限制**：哈希只吃元数据 + 正文，不认平台 id。

    同一作者发了两条标题 / 简介 / 正文完全相同的**不同**视频（只有 `source_id` 不同）
    → 会被判为同一条内容。这是定稿第七节口径的直接后果，不是实现 bug。
    """
    provider = ScriptedProvider([load_fixture_text(VALID_LLM_FIXTURE)])
    pipeline = pipeline_factory(provider)
    first = await pipeline.process(raw_content)
    other_video = raw_content.model_copy(update={"source_id": "1231231231231231231"})
    second = await pipeline.process(other_video)

    assert second.outcome == "duplicate"
    assert second.deduplicated_by == DEDUP_BY_CONTENT_HASH
    assert second.duplicate_of == first.content_id
    assert provider.call_count == 1
    assert await repo.count_contents() == 1


async def test_same_title_but_different_text_is_distinct_content(
    pipeline_factory, repo: ContentRepository, raw_content
) -> None:
    """**v2 的收益**：正文进了哈希白名单，标题相同但正文不同 → 两条内容。

    v1 只吃 title/author/description，所以「标题相同、正文不同」会被误判成同一条。
    手贴入口「不填标题只粘正文」是这条缺陷的重灾区。
    """
    provider = ScriptedProvider([load_fixture_text(VALID_LLM_FIXTURE)] * 2)
    pipeline = pipeline_factory(provider)
    first = await pipeline.process(raw_content)
    other_video = raw_content.model_copy(
        update={
            "source_id": "1231231231231231231",
            "raw_text": "其实是另一条视频的文案",
            "transcript": "另一条视频的逐字稿",
        }
    )
    second = await pipeline.process(other_video)

    assert first.outcome == "completed"
    assert second.outcome == "completed"
    assert second.content_id != first.content_id
    assert provider.call_count == 2
    assert await repo.count_contents() == 2


async def test_untitled_pastes_are_all_distinct(
    pipeline_factory, repo: ContentRepository
) -> None:
    """**手贴入口的回归**：不填标题、只粘正文 → 每段正文都是自己的一条内容。

    v1 下它们的哈希与 ``hash:`` fallback id 完全相同，第二条起全被静默吞掉。
    """
    from app.schemas import RawContent

    def paste(text: str) -> RawContent:
        return RawContent(
            source="manual",
            source_id="",
            source_url="",
            media_type="text",
            raw_text=text,
        )

    provider = ScriptedProvider([load_fixture_text(VALID_LLM_FIXTURE)] * 2)
    pipeline = pipeline_factory(provider)
    first = await pipeline.process(paste("今天聊人工智能算力，作者判断成本会下降三成。"))
    second = await pipeline.process(paste("完全不同的另一段正文：2026 年新能源汽车出口增长四成。"))

    assert first.outcome == "completed"
    assert second.outcome == "completed"
    assert second.content_id != first.content_id
    assert await repo.count_contents() == 2


def test_dedup_keys_are_named() -> None:
    assert DEDUP_BY_SOURCE_ID == "source_id"
    assert DEDUP_BY_CONTENT_HASH == "content_hash"


@pytest.mark.parametrize("field", ["content_id", "duplicate_of", "deduplicated_by"])
def test_result_carries_dedup_metadata(field: str) -> None:
    from app.pipeline.result import ProcessingResult

    payload = ProcessingResult(
        outcome="duplicate", content_id="c1", deduplicated_by=DEDUP_BY_CONTENT_HASH
    ).to_dict()
    assert field in payload
