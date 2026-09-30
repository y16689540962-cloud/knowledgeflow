"""Markdown 渲染器（第二十节 / 第九节）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.obsidian.frontmatter import parse_frontmatter
from app.obsidian.renderer import (
    DEFAULT_TOP_CLAIMS,
    EMPTY_MARKER,
    SECTION_ORDER,
    LinkedEntity,
    NoteContext,
    build_frontmatter,
    build_tags,
    core_claim_lines,
    fenced_block,
    note_filename,
    note_title,
    one_line,
    render_failure_note,
    render_note,
)
from app.schemas import ContentAnalysis


def headings(markdown: str) -> list[str]:
    """提取真标题（跳过代码围栏内部 —— 围栏里的 ``##`` 只是正文）。"""
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


def h1(markdown: str) -> str:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:]
    raise AssertionError("笔记里没有 H1")


def section_text(markdown: str, name: str) -> str:
    marker = f"## {name}"
    lines = markdown.splitlines()
    try:
        start = lines.index(marker)
    except ValueError:  # pragma: no cover - 测试自身出错才会走到
        raise AssertionError(f"没有找到章节 {name}")
    collected: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        collected.append(line)
    return "\n".join(collected).strip()


# --------------------------------------------------------------------------- #
# 结构
# --------------------------------------------------------------------------- #
def test_section_order_matches_spec() -> None:
    assert SECTION_ORDER == (
        "摘要",
        "核心观点",
        "事实",
        "作者观点",
        "推论",
        "预测",
        "未验证信息",
        "值得进一步研究",
        "相关实体",
        "相关知识",
        "原始内容",
        "Transcript",
        "OCR",
    )


def test_all_sections_present_in_order(note_context: NoteContext) -> None:
    note = render_note(note_context)
    assert headings(note.markdown) == list(SECTION_ORDER)


def test_h1_uses_analysis_title(note_context: NoteContext) -> None:
    note = render_note(note_context)
    assert h1(note.markdown) == "人口下降之后，房子还会涨吗"


def test_h1_falls_back_to_content_title() -> None:
    context = NoteContext(
        content_id="c1",
        source="manual",
        source_id="hash:x",
        analysis=ContentAnalysis(
            title="", summary="s", analysis_type="mixed", claims=[], overall_confidence=0.1
        ),
        title="原始标题",
    )
    assert note_title(context) == "原始标题"


def test_h1_falls_back_to_untitled() -> None:
    context = NoteContext(
        content_id="c1",
        source="manual",
        source_id="hash:x",
        analysis=ContentAnalysis(
            title="", summary="s", analysis_type="mixed", claims=[], overall_confidence=0.1
        ),
    )
    assert note_title(context) == "未命名"


def test_frontmatter_embedded_matches_builder(note_context: NoteContext) -> None:
    note = render_note(note_context)
    assert parse_frontmatter(note.markdown) == build_frontmatter(note_context)
    assert note.frontmatter == build_frontmatter(note_context)


def test_note_renders_all_sections_even_when_empty() -> None:
    context = NoteContext(
        content_id="c1",
        source="manual",
        source_id="hash:x",
        analysis=ContentAnalysis(
            title="标题",
            summary="",
            analysis_type="mixed",
            claims=[],
            overall_confidence=0.0,
        ),
    )
    note = render_note(context)
    assert headings(note.markdown) == list(SECTION_ORDER)
    for name in SECTION_ORDER:
        if name != "摘要":
            assert EMPTY_MARKER in section_text(note.markdown, name)


# --------------------------------------------------------------------------- #
# 事实 / 观点 / 推论 / 预测
# --------------------------------------------------------------------------- #
def test_four_way_separation(note_context: NoteContext) -> None:
    note = render_note(note_context)
    assert "中国人口正在下降。" in section_text(note.markdown, "事实")
    assert "作者认为房地产未来会上涨。" in section_text(note.markdown, "作者观点")
    assert "作者将人口变化与房地产价格联系起来。" in section_text(note.markdown, "推论")
    assert "作者预测房地产未来上涨。" in section_text(note.markdown, "预测")


def test_prediction_never_in_facts_section(note_context: NoteContext) -> None:
    """第九节禁令：预测句绝不能出现在「事实」章节。"""
    note = render_note(note_context)
    assert "上涨" not in section_text(note.markdown, "事实")


def test_fact_never_in_prediction_section(note_context: NoteContext) -> None:
    note = render_note(note_context)
    assert "人口正在下降" not in section_text(note.markdown, "预测")


def test_claim_renders_evidence_and_confidence(note_context: NoteContext) -> None:
    facts = section_text(render_note(note_context).markdown, "事实")
    assert "证据（source）" in facts
    assert "证据（data）" in facts
    assert "置信度：0.90" in facts


def test_claim_evidence_none_is_skipped(note_context: NoteContext) -> None:
    predictions = section_text(render_note(note_context).markdown, "预测")
    assert "证据（none）" not in predictions


def test_needs_verification_status_marked(note_context: NoteContext) -> None:
    note = render_note(note_context)
    assert "状态：需验证" in section_text(note.markdown, "作者观点")
    assert "状态：已验证" in section_text(note.markdown, "事实")


def test_author_position_and_time_horizon(note_context: NoteContext) -> None:
    note = render_note(note_context)
    opinions = section_text(note.markdown, "作者观点")
    assert "作者立场：主张房价上涨" in opinions
    assert "时间范围：未来" in opinions


def test_based_on_claims_rendered(note_context: NoteContext) -> None:
    note = render_note(note_context)
    assert "基于：中国人口正在下降。" in section_text(note.markdown, "作者观点")


# --------------------------------------------------------------------------- #
# 其余章节
# --------------------------------------------------------------------------- #
def test_unverified_section_lists_claims(note_context: NoteContext) -> None:
    body = section_text(render_note(note_context).markdown, "未验证信息")
    assert "作者认为房地产未来会上涨。" in body
    assert "作者预测房地产未来上涨。" in body
    assert "中国人口正在下降。" not in body


def test_questions_section(note_context: NoteContext) -> None:
    body = section_text(render_note(note_context).markdown, "值得进一步研究")
    assert "出生人口下降是否必然导致核心城市房价下跌？" in body


def test_entities_section_uses_wikilinks(note_context: NoteContext) -> None:
    body = section_text(render_note(note_context).markdown, "相关实体")
    assert "- [[中国]]" in body
    assert "[[人工智能]]（AI、Artificial Intelligence）" in body


def test_entity_without_aliases_has_no_parentheses() -> None:
    context = NoteContext(
        content_id="c1",
        source="manual",
        source_id="hash:x",
        analysis=ContentAnalysis(
            title="t", summary="s", analysis_type="mixed", claims=[], overall_confidence=0.1
        ),
        entities=(LinkedEntity(canonical_name="中国"),),
    )
    body = section_text(render_note(context).markdown, "相关实体")
    assert body == "- [[中国]]"


def test_topics_section_uses_wikilinks(note_context: NoteContext) -> None:
    body = section_text(render_note(note_context).markdown, "相关知识")
    assert "- [[人口结构]]" in body
    assert "- [[房地产]]" in body


def test_core_claims_sorted_by_confidence(note_context: NoteContext) -> None:
    body = section_text(render_note(note_context).markdown, "核心观点")
    lines = [line for line in body.splitlines() if line.startswith("- ")]
    assert len(lines) == min(DEFAULT_TOP_CLAIMS, len(note_context.analysis.claims))
    # 置信度最高的是第一条 fact（0.90）
    assert "中国人口正在下降。" in lines[0]
    assert lines[0].endswith("（fact）")


def test_core_claims_limit_is_honoured(note_context: NoteContext) -> None:
    body = section_text(render_note(note_context, top_claims=2).markdown, "核心观点")
    assert len([line for line in body.splitlines() if line.startswith("- ")]) == 2


def test_core_claims_zero_limit(note_context: NoteContext) -> None:
    assert core_claim_lines(note_context.analysis, limit=0) == [EMPTY_MARKER]


# --------------------------------------------------------------------------- #
# 原文围栏
# --------------------------------------------------------------------------- #
def test_raw_text_is_fenced(note_context: NoteContext) -> None:
    body = section_text(render_note(note_context).markdown, "原始内容")
    assert body.startswith("```")
    assert body.endswith("```")
    assert "中国人口正在下降" in body


def test_missing_transcript_and_ocr_are_empty_markers(note_context: NoteContext) -> None:
    note = render_note(note_context)
    assert section_text(note.markdown, "Transcript") == EMPTY_MARKER
    assert section_text(note.markdown, "OCR") == EMPTY_MARKER


def test_raw_content_with_headings_does_not_break_structure(note_context: NoteContext) -> None:
    """正文里带 ``##`` 的内容被围在代码块里，不能变成真标题。"""
    context = NoteContext(
        content_id="c1",
        source="manual",
        source_id="hash:x",
        analysis=note_context.analysis,
        title="t",
        raw_text="正常一句。\n## 假标题\n# 更大的假标题\n",
    )
    note = render_note(context)
    assert headings(note.markdown) == list(SECTION_ORDER)


def test_fence_is_longer_than_any_run_in_content() -> None:
    text = "内容 ``` 里面有三个反引号"
    block = fenced_block(text)
    assert block[0] == "````text"
    assert block[-1] == "````"


def test_fence_defaults_to_three_backticks() -> None:
    assert fenced_block("普通内容")[0] == "```text"


def test_fenced_block_strips_trailing_whitespace() -> None:
    assert fenced_block("内容\n\n  ")[1] == "内容"


# --------------------------------------------------------------------------- #
# 文件名稳定性（关键：reprocess 不能产生第二个文件）
# --------------------------------------------------------------------------- #
def test_filename_from_content_title(note_context: NoteContext) -> None:
    assert note_filename(note_context) == "人口下降之后，房子还会涨吗"


def test_filename_ignores_analysis_title(note_context: NoteContext) -> None:
    changed = note_context.analysis.model_copy(update={"title": "完全不同的 AI 标题"})
    from dataclasses import replace

    other = replace(note_context, analysis=changed)
    assert note_filename(other) == note_filename(note_context)


def test_filename_falls_back_to_content_id() -> None:
    context = NoteContext(
        content_id="0f1e2d3c4b5a6978",
        source="manual",
        source_id="hash:x",
        analysis=ContentAnalysis(
            title="AI 标题", summary="s", analysis_type="mixed", claims=[], overall_confidence=0.1
        ),
        title=None,
    )
    assert note_filename(context) == "0f1e2d3c"


# --------------------------------------------------------------------------- #
# tags / 小工具
# --------------------------------------------------------------------------- #
def test_tags_are_analysis_type_plus_topics(note_context: NoteContext) -> None:
    assert build_tags(note_context) == ["mixed", "人口结构", "房地产"]


def test_topics_fall_back_to_analysis_output() -> None:
    context = NoteContext(
        content_id="c1",
        source="manual",
        source_id="hash:x",
        analysis=ContentAnalysis(
            title="t",
            summary="s",
            analysis_type="mixed",
            claims=[],
            topics=["甲", "乙"],
            overall_confidence=0.1,
        ),
    )
    assert build_frontmatter(context)["topics"] == ["甲", "乙"]
    assert build_tags(context) == ["mixed", "甲", "乙"]


def test_frontmatter_topics_match_related_section(note_context: NoteContext) -> None:
    """frontmatter 的 topics、「相关知识」的链接、tags 三处必须一致。"""
    note = render_note(note_context)
    topics = note.frontmatter["topics"]
    assert isinstance(topics, list)
    body = section_text(note.markdown, "相关知识")
    assert [line for line in body.splitlines() if line.startswith("- ")] == [
        f"- [[{topic}]]" for topic in topics
    ]
    assert note.frontmatter["tags"] == ["mixed", *topics]


def test_tags_dedupe() -> None:
    context = NoteContext(
        content_id="c1",
        source="manual",
        source_id="hash:x",
        analysis=ContentAnalysis(
            title="t",
            summary="s",
            analysis_type="mixed",
            claims=[],
            topics=["mixed", "topic"],
            overall_confidence=0.1,
        ),
    )
    assert build_tags(context) == ["mixed", "topic"]


def test_one_line_collapses_whitespace() -> None:
    assert one_line("a\n b\t\tc ") == "a b c"
    assert one_line(None) == ""


def test_claim_text_newlines_do_not_break_list(note_context: NoteContext) -> None:
    """claim 文本里的换行不能把 Markdown 列表结构搞坏。"""
    analysis = note_context.analysis.model_copy(
        update={
            "claims": [
                note_context.analysis.claims[0].model_copy(update={"text": "第一行\n第二行"})
            ]
        }
    )
    from dataclasses import replace

    note = render_note(replace(note_context, analysis=analysis))
    facts = section_text(note.markdown, "事实")
    assert "- 第一行 第二行" in facts
    assert "\n第二行" not in facts


# --------------------------------------------------------------------------- #
# 失败笔记
# --------------------------------------------------------------------------- #
def test_failure_note_has_error_section(note_context: NoteContext) -> None:
    note = render_failure_note(
        note_context, error_type="LLM_INVALID_OUTPUT", error_message="三次都解析失败"
    )
    assert "## 处理失败" in note.markdown
    assert "`LLM_INVALID_OUTPUT`" in note.markdown
    assert "三次都解析失败" in note.markdown


def test_failure_note_keeps_raw_content(note_context: NoteContext) -> None:
    note = render_failure_note(note_context, error_type="X", error_message="y")
    assert "中国人口正在下降" in section_text(note.markdown, "原始内容")


def test_failure_note_marks_manual_review(note_context: NoteContext) -> None:
    note = render_failure_note(note_context, error_type="X", error_message="y")
    assert note.frontmatter["needs_manual_review"] is True
    assert parse_frontmatter(note.markdown)["needs_manual_review"] is True


def test_failure_note_keeps_filename_and_sections(note_context: NoteContext) -> None:
    note = render_failure_note(note_context, error_type="X", error_message="y")
    assert note.filename == note_filename(note_context)
    assert note.sections == ("处理失败", *SECTION_ORDER)


def test_failure_note_heading_order(note_context: NoteContext) -> None:
    note = render_failure_note(note_context, error_type="X", error_message="y")
    assert headings(note.markdown) == ["处理失败", *SECTION_ORDER]


# --------------------------------------------------------------------------- #
# 与 schema 的边界
# --------------------------------------------------------------------------- #
def test_analysis_from_invalid_payload_still_rejected() -> None:
    with pytest.raises(ValidationError):
        ContentAnalysis.model_validate({"title": "t"})
