"""Phase 3 验收：Markdown → 安全文件名 → Obsidian Vault（第三十一 / 三十二节）。

链路：

```text
fixture → (Mock LLM) → Pydantic → Grounding Check → SQLite
        → 实体 / 主题归一化（alias 表）→ Markdown → 安全文件名 → Vault
```

**全程离线**，只用临时 vault 目录，不碰用户的真实 Obsidian。
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from app.db.repository import ContentRepository
from app.errors import PathTraversalError
from app.grounding.rules import apply_grounding
from app.knowledge.aliases import DatabaseAliasLoader
from app.knowledge.entities import EntityRegistry
from app.knowledge.topics import TopicRegistry
from app.obsidian.frontmatter import FRONTMATTER_KEY_ORDER, frontmatter_fields_in_order
from app.obsidian.layout import SUBDIRS, VAULT_NAMESPACE
from app.obsidian.renderer import SECTION_ORDER, NoteContext, render_note
from app.obsidian.writer import ObsidianWriter
from app.providers import MockProvider
from app.schemas import ContentAnalysis
from app.testing import load_fixture_json
from tests.conftest import insert_raw, make_service


def headings(markdown: str) -> list[str]:
    body = markdown.split("---\n", 2)[-1]
    in_fence = False
    found: list[str] = []
    for line in body.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and line.startswith("## "):
            found.append(line[3:])
    return found


async def build_note(
    *,
    repo: ContentRepository,
    entities: EntityRegistry,
    topics: TopicRegistry,
    analysis: ContentAnalysis,
    raw,
    content_id: str,
) -> tuple[NoteContext, str]:
    """落库 → 归一化实体/主题 → 组装 NoteContext。"""
    row = await repo.get_content(content_id)
    assert row is not None

    grounded, _report = apply_grounding(analysis, raw.source_text())

    resolved_entities = await entities.resolve_many(
        [(entity.name, entity.type) for entity in grounded.entities]
    )
    resolved_topics = await topics.resolve_many(list(grounded.topics))

    await entities.link_content(
        content_id, [(item.entity_id, None) for item in resolved_entities]
    )
    await topics.link_content(content_id, [(item.topic_id, None) for item in resolved_topics])

    context = NoteContext(
        content_id=content_id,
        source=row.source,
        source_id=row.source_id,
        analysis=grounded,
        source_url=row.source_url,
        title=row.title,
        author=row.author,
        media_type=row.media_type or "text",
        created_at=row.created_at,
        processed_at=row.processed_at,
        needs_manual_review=bool(row.needs_manual_review),
        content_hash_version=int(row.content_hash_version),
        prompt_version="analysis_prompt_v1",
        model="mock-llm-v1",
        entities=tuple(item.as_linked() for item in resolved_entities),
        topics=tuple(item.name for item in resolved_topics),
        raw_text=row.raw_text,
        transcript=row.transcript,
        ocr_text=row.ocr_text,
    )
    return context, row.source_id


# --------------------------------------------------------------------------- #
# A 线端到端
# --------------------------------------------------------------------------- #
async def test_a_line_from_fixture_to_vault(
    db,
    repo: ContentRepository,
    entities: EntityRegistry,
    topics: TopicRegistry,
    obsidian_writer: ObsidianWriter,
    vault_root: Path,
    mixed_raw_content,
    valid_analysis_payload: dict,
) -> None:
    # 1) Phase 2：Mock LLM → Pydantic → Grounding Check
    outcome = await make_service(MockProvider(payload=valid_analysis_payload)).analyze_raw(
        mixed_raw_content
    )
    assert outcome.success is True
    assert outcome.analysis is not None
    assert outcome.grounding is not None and outcome.grounding.needs_verification_count == 3

    # 2) Phase 1：落库
    content_id = await insert_raw(repo, mixed_raw_content, content_id="acceptance0001")

    # 3) Phase 3：归一化 + 渲染 + 写入
    context, source_id = await build_note(
        repo=repo,
        entities=entities,
        topics=topics,
        analysis=outcome.analysis,
        raw=mixed_raw_content,
        content_id=content_id,
    )
    note = render_note(context)
    written = obsidian_writer.write_rendered(note, identity=(context.source, source_id))

    # 4) 断言：路径与目录
    assert written.status == "created"
    assert written.relative_path == os.path.join(
        VAULT_NAMESPACE, "Processed", "人口下降之后，房子还会涨吗.md"
    )
    for subdir in SUBDIRS:
        assert (vault_root / VAULT_NAMESPACE / subdir).is_dir()

    markdown = written.absolute_path.read_text(encoding="utf-8")

    # 5) 断言：frontmatter
    assert frontmatter_fields_in_order(markdown) == list(FRONTMATTER_KEY_ORDER)
    front = yaml.safe_load(markdown.split("---\n")[1])
    assert front["source"] == "manual"
    assert front["source_id"].startswith("hash:")
    assert front["analysis_type"] == "mixed"
    assert front["prompt_version"] == "analysis_prompt_v1"
    assert front["model"] == "mock-llm-v1"
    assert front["needs_verification"] is True
    assert front["needs_manual_review"] is True
    assert front["content_hash_version"] == 2
    assert front["confidence"] == pytest.approx(0.72)
    assert front["tags"] == ["mixed", *front["topics"]]

    # 6) 断言：章节顺序固定 + 四类分离
    assert headings(markdown) == list(SECTION_ORDER)
    facts_start = markdown.index("## 事实")
    facts_block = markdown[facts_start : markdown.index("## 作者观点")]
    assert "中国人口正在下降。" in facts_block
    assert "上涨" not in facts_block  # 第九节禁令

    # 7) 断言：实体 / 主题走 alias 表并输出 wikilink
    entities_block = markdown[markdown.index("## 相关实体") : markdown.index("## 相关知识")]
    for canonical in ("中国", "房地产", "人口", "一线城市"):
        assert f"[[{canonical}]]" in entities_block
    topics_block = markdown[markdown.index("## 相关知识") : markdown.index("## 原始内容")]
    for topic in front["topics"]:
        assert f"[[{topic}]]" in topics_block

    # 8) 断言：只写内容笔记，实体/主题**不**生成空白笔记
    all_files = sorted(
        path.name for path in (vault_root / VAULT_NAMESPACE).rglob("*") if path.is_file()
    )
    assert all_files == ["人口下降之后，房子还会涨吗.md"]

    # 9) 断言：DB 里的关联与笔记一致
    linked = await entities.list_content_entities(content_id)
    assert {item.canonical_name for item in linked} == {"中国", "房地产", "人口", "一线城市"}
    linked_topics = await topics.list_content_topics(content_id)
    assert {item.name for item in linked_topics} == set(front["topics"])


async def test_alias_table_feeds_grounding_and_links(
    db,
    repo: ContentRepository,
    entities: EntityRegistry,
    topics: TopicRegistry,
    alias_loader: DatabaseAliasLoader,
    obsidian_writer: ObsidianWriter,
    vault_root: Path,
) -> None:
    """别名入库后，渲染出的链接指向 canonical，而不是表面形式。"""
    from app.schemas import RawContent

    await entities.resolve_or_create("人工智能", "concept", aliases=["AI"])
    alias_loader.invalidate()

    raw = RawContent.from_fixture(
        {
            "source": "manual",
            "source_id": "",
            "source_url": "",
            "title": "AI 与内容行业",
            "media_type": "text",
            "raw_text": "人工智能 正在改变内容行业。",
        }
    )
    content_id = await insert_raw(repo, raw, content_id="aliasfeed0001")

    analysis = ContentAnalysis.model_validate(
        {
            "title": "AI 与内容行业",
            "summary": "围绕人工智能的讨论。",
            "analysis_type": "opinion",
            "claims": [
                {
                    "text": "AI 正在改变内容行业。",
                    "type": "fact",
                    "confidence": 0.9,
                    "evidence": [{"type": "source", "description": "原文如此"}],
                }
            ],
            "topics": ["人工智能"],
            "entities": [{"name": "AI", "type": "concept"}],
            "overall_confidence": 0.6,
        }
    )

    context, source_id = await build_note(
        repo=repo,
        entities=entities,
        topics=topics,
        analysis=analysis,
        raw=raw,
        content_id=content_id,
    )
    # 库里只有一条实体，链接指向 canonical「人工智能」
    assert [entity.canonical_name for entity in context.entities] == ["人工智能"]

    written = obsidian_writer.write_rendered(
        render_note(context), identity=(context.source, source_id)
    )
    markdown = written.absolute_path.read_text(encoding="utf-8")
    assert "- [[人工智能]]（AI）" in markdown
    assert "[[AI]]" not in markdown


# --------------------------------------------------------------------------- #
# 幂等与消歧
# --------------------------------------------------------------------------- #
async def test_reprocess_does_not_create_second_note(
    repo: ContentRepository,
    entities: EntityRegistry,
    topics: TopicRegistry,
    obsidian_writer: ObsidianWriter,
    vault_root: Path,
    mixed_raw_content,
    valid_analysis_payload: dict,
) -> None:
    content_id = await insert_raw(repo, mixed_raw_content, content_id="reprocess001")
    analysis = ContentAnalysis.model_validate(valid_analysis_payload)

    context, source_id = await build_note(
        repo=repo,
        entities=entities,
        topics=topics,
        analysis=analysis,
        raw=mixed_raw_content,
        content_id=content_id,
    )
    identity = (context.source, source_id)
    first = obsidian_writer.write_rendered(render_note(context), identity=identity)

    # reprocess：AI 标题变了，内容也变了
    reprocessed = analysis.model_copy(update={"title": "第二版标题"})
    second_context = replace(
        context,
        analysis=reprocessed,
        prompt_version="synthesis_prompt_v1",
    )
    second = obsidian_writer.write_rendered(render_note(second_context), identity=identity)

    assert first.relative_path == second.relative_path
    assert second.status == "updated"
    files = sorted(
        path.name for path in (vault_root / VAULT_NAMESPACE / "Processed").glob("*.md")
    )
    assert len(files) == 1
    assert "# 第二版标题" in second.absolute_path.read_text(encoding="utf-8")


async def test_two_contents_with_same_title_do_not_collide(
    repo: ContentRepository,
    entities: EntityRegistry,
    topics: TopicRegistry,
    obsidian_writer: ObsidianWriter,
    vault_root: Path,
    mixed_raw_content,
    valid_analysis_payload: dict,
) -> None:
    from app.schemas import RawContent

    analysis = ContentAnalysis.model_validate(valid_analysis_payload)

    async def write_for(raw: RawContent, content_id: str):
        cid = await insert_raw(repo, raw, content_id=content_id)
        context, source_id = await build_note(
            repo=repo,
            entities=entities,
            topics=topics,
            analysis=analysis,
            raw=raw,
            content_id=cid,
        )
        return obsidian_writer.write_rendered(
            render_note(context), identity=(context.source, source_id)
        )

    first = await write_for(mixed_raw_content, "same-title-0001")

    # 另一条内容，标题完全相同，但 source_id 不同
    other = mixed_raw_content.model_copy(update={"source_id": "9999999999999999999"})
    second = await write_for(other, "same-title-0002")

    assert first.relative_path != second.relative_path
    assert second.disambiguated is True
    assert first.absolute_path.is_file() and second.absolute_path.is_file()


# --------------------------------------------------------------------------- #
# 安全边界
# --------------------------------------------------------------------------- #
async def test_writer_rejects_traversal(obsidian_writer: ObsidianWriter) -> None:
    for bad in ("../evil.md", "KnowledgeFlow/../../evil.md", "/tmp/evil.md"):
        with pytest.raises(PathTraversalError):
            obsidian_writer.write_markdown(bad, "内容")


async def test_note_filename_is_sanitized(vault_root: Path, note_context) -> None:
    from app.security import MAX_FILENAME_CODE_POINTS

    evil = replace(
        note_context,
        title='标题/含:非法*字符?"<>| 与\n换行' + "很长的标题" * 20,
    )
    result = ObsidianWriter(vault_root).write_rendered(
        render_note(evil), identity=("manual", "hash:x")
    )
    name = result.absolute_path.name
    assert "/" not in name and ":" not in name and "\n" not in name
    assert len(name.removesuffix(".md")) <= MAX_FILENAME_CODE_POINTS
    assert result.absolute_path.is_file()


async def test_vault_outside_writes_are_impossible(
    obsidian_writer: ObsidianWriter, vault_root: Path, note_context
) -> None:
    obsidian_writer.write_rendered(render_note(note_context), identity=("manual", "hash:x"))
    written_files = [path for path in vault_root.rglob("*.md")]
    assert len(written_files) == 1
    assert written_files[0].is_relative_to(vault_root.resolve())


# --------------------------------------------------------------------------- #
# 过程产物
# --------------------------------------------------------------------------- #
def test_phase3_modules_do_not_import_network() -> None:
    from app.knowledge import entities as entities_module
    from app.obsidian import writer as writer_module

    for module in (entities_module, writer_module):
        source = open(module.__file__, encoding="utf-8").read()
        for banned in ("httpx", "requests", "urllib.request", "aiohttp", "socket"):
            assert banned not in source, f"{module.__name__} 引用了 {banned}"


def test_phase3_does_not_write_to_real_vault() -> None:
    """Phase 3 的所有写入都必须经过显式传入的 vault_root，没有默认值。"""
    import inspect

    from app.obsidian.writer import ObsidianWriter as Writer

    signature = inspect.signature(Writer.__init__)
    assert signature.parameters["vault_root"].default is inspect.Parameter.empty
