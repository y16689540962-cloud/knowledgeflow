"""Markdown 渲染器（定稿文档第二十节）。

**Markdown 只是 JSON 的渲染结果，不是真源**（第二节第 5 条）：

* frontmatter 字段与顺序按文档
* 正文章节顺序**固定**，14 个章节永远都在（空的写「（无）」），
  这样笔记结构可预测、可断言
* 事实 / 作者观点 / 推论 / 预测 四个章节由 ``claims[]`` 按 ``type`` 分组渲染
* 原始内容 / Transcript / OCR 一律包在代码围栏里 —— 正文里若含 ``##`` 之类的
  Markdown 结构，不围起来会被 Obsidian 解析成假标题
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final

from app.obsidian.frontmatter import render_frontmatter
from app.schemas.analysis import Claim, ContentAnalysis
from app.security.filenames import sanitize_filename

EMPTY_MARKER: Final[str] = "（无）"
UNTITLED: Final[str] = "未命名"

#: 正文章节顺序（文档第二十节，一字不改）。
SECTION_ORDER: Final[tuple[str, ...]] = (
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

#: 「核心观点」取多少条 claim。
DEFAULT_TOP_CLAIMS: Final[int] = 5

_FENCE_RUN_RE = re.compile(r"`+")


@dataclass(frozen=True)
class LinkedEntity:
    """已归一化的实体（canonical 名 + 别名），渲染成 ``[[canonical]]``。"""

    canonical_name: str
    aliases: tuple[str, ...] = ()
    entity_type: str = "other"


@dataclass(frozen=True)
class NoteContext:
    content_id: str
    source: str
    source_id: str
    analysis: ContentAnalysis
    source_url: str | None = None
    title: str | None = None
    author: str | None = None
    media_type: str = "text"
    created_at: str = ""
    processed_at: str | None = None
    needs_manual_review: bool = False
    content_hash_version: int = 1
    prompt_version: str = ""
    model: str | None = None
    entities: tuple[LinkedEntity, ...] = ()
    topics: tuple[str, ...] = ()
    raw_text: str | None = None
    transcript: str | None = None
    ocr_text: str | None = None


@dataclass(frozen=True)
class RenderedNote:
    content_id: str
    filename: str
    markdown: str
    frontmatter: dict[str, object]
    sections: tuple[str, ...] = field(default=())


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def one_line(text: str | None) -> str:
    """把任意文本压成一行 —— 防止正文内容在列表项里破坏 Markdown 结构。"""
    return " ".join((text or "").split())


def fenced_block(text: str, *, info: str = "text") -> list[str]:
    """用**足够长**的围栏包住原文（原文里有 ``` 也不会提前结束）。"""
    longest = max((len(run) for run in _FENCE_RUN_RE.findall(text)), default=0)
    fence = "`" * max(3, longest + 1)
    return [f"{fence}{info}", text.rstrip(), fence]


def note_title(context: NoteContext) -> str:
    """H1 标题：优先用 AI 给的标题，其次原文标题。"""
    return (
        one_line(context.analysis.title)
        or one_line(context.title)
        or UNTITLED
    )


def note_filename(context: NoteContext) -> str:
    """文件名**只依赖内容原始标题**。

    刻意不用 AI 标题：reprocess 后 AI 标题可能变化，那会生成第二个文件，
    直接违反「重复输入 → 不产生重复内容」。原始标题为空时退化为
    ``content_id[:8]``，也比会漂移的 AI 标题稳定。
    """
    return sanitize_filename(context.title, context.content_id)


def resolved_topics(context: NoteContext) -> list[str]:
    """最终生效的主题列表（去重保序）。

    优先用**归一化后**的主题（``context.topics``，来自 ``topics`` 表），
    它为空时才退回 AI 原始输出。frontmatter 的 ``topics`` / ``tags``
    与正文「相关知识」三处必须一致。
    """
    source = context.topics if context.topics else tuple(context.analysis.topics)
    topics: list[str] = []
    for topic in source:
        cleaned = one_line(topic)
        if cleaned and cleaned not in topics:
            topics.append(cleaned)
    return topics


def build_tags(context: NoteContext) -> list[str]:
    """标签 = analysis_type + topics（去重保序）。"""
    tags: list[str] = []
    for tag in (context.analysis.analysis_type, *resolved_topics(context)):
        cleaned = one_line(tag)
        if cleaned and cleaned not in tags:
            tags.append(cleaned)
    return tags


def build_frontmatter(context: NoteContext) -> dict[str, object]:
    analysis = context.analysis
    return {
        "title": note_title(context),
        "source": context.source,
        "source_id": context.source_id,
        "source_url": context.source_url or "",
        "author": context.author or "",
        "media_type": context.media_type,
        "analysis_type": analysis.analysis_type,
        "topics": resolved_topics(context),
        "tags": build_tags(context),
        "created_at": context.created_at,
        "processed_at": context.processed_at or "",
        "model": context.model or "",
        "prompt_version": context.prompt_version,
        "confidence": round(float(analysis.overall_confidence), 4),
        "needs_verification": bool(analysis.unverified_claims),
        "needs_manual_review": bool(context.needs_manual_review),
        "content_hash_version": int(context.content_hash_version),
    }


# --------------------------------------------------------------------------- #
# claim 渲染
# --------------------------------------------------------------------------- #
def render_claim(claim: Claim) -> list[str]:
    lines = [f"- {one_line(claim.text)}"]
    if claim.author_position:
        lines.append(f"  - 作者立场：{one_line(claim.author_position)}")
    if claim.time_horizon:
        lines.append(f"  - 时间范围：{one_line(claim.time_horizon)}")
    if claim.based_on_claims:
        joined = "；".join(one_line(item) for item in claim.based_on_claims if one_line(item))
        if joined:
            lines.append(f"  - 基于：{joined}")
    for evidence in claim.evidence:
        if evidence.type == "none":
            continue
        detail = f"  - 证据（{evidence.type}）：{one_line(evidence.description)}"
        if evidence.source_url:
            detail += f" — {evidence.source_url}"
        lines.append(detail)
    lines.append(f"  - 置信度：{claim.confidence:.2f}")
    lines.append(f"  - 状态：{'需验证' if claim.needs_verification else '已验证'}")
    return lines


def render_claims(claims: list[Claim]) -> list[str]:
    if not claims:
        return [EMPTY_MARKER]
    lines: list[str] = []
    for claim in claims:
        if lines:
            lines.append("")
        lines.extend(render_claim(claim))
    return lines


def render_entity(entity: LinkedEntity) -> str:
    link = f"[[{entity.canonical_name}]]"
    extras = [one_line(alias) for alias in entity.aliases if one_line(alias)]
    if extras:
        return f"- {link}（{'、'.join(extras)}）"
    return f"- {link}"


def core_claim_lines(analysis: ContentAnalysis, *, limit: int) -> list[str]:
    """「核心观点」= 按 confidence 取前 N 条 claim（跨类型），标注类型。"""
    if not analysis.claims or limit <= 0:
        return [EMPTY_MARKER]
    ranked = sorted(
        enumerate(analysis.claims), key=lambda pair: (-pair[1].confidence, pair[0])
    )
    return [f"- {one_line(claim.text)}（{claim.type}）" for _, claim in ranked[:limit]]


# --------------------------------------------------------------------------- #
# 主渲染
# --------------------------------------------------------------------------- #
def section_bodies(context: NoteContext, *, top_claims: int = DEFAULT_TOP_CLAIMS) -> dict[str, list[str]]:
    analysis = context.analysis
    return {
        "摘要": [one_line(analysis.summary)] if one_line(analysis.summary) else [EMPTY_MARKER],
        "核心观点": core_claim_lines(analysis, limit=top_claims),
        "事实": render_claims(analysis.claims_of_type("fact")),
        "作者观点": render_claims(analysis.claims_of_type("opinion")),
        "推论": render_claims(analysis.claims_of_type("inference")),
        "预测": render_claims(analysis.claims_of_type("prediction")),
        "未验证信息": (
            [f"- {one_line(text)}" for text in analysis.unverified_claims if one_line(text)]
            or [EMPTY_MARKER]
        ),
        "值得进一步研究": (
            [f"- {one_line(text)}" for text in analysis.questions if one_line(text)]
            or [EMPTY_MARKER]
        ),
        "相关实体": [render_entity(entity) for entity in context.entities] or [EMPTY_MARKER],
        "相关知识": [f"- [[{topic}]]" for topic in resolved_topics(context)] or [EMPTY_MARKER],
        "原始内容": fenced_block(context.raw_text) if context.raw_text else [EMPTY_MARKER],
        "Transcript": fenced_block(context.transcript) if context.transcript else [EMPTY_MARKER],
        "OCR": fenced_block(context.ocr_text) if context.ocr_text else [EMPTY_MARKER],
    }


def render_note(context: NoteContext, *, top_claims: int = DEFAULT_TOP_CLAIMS) -> RenderedNote:
    """渲染成完整的 Obsidian 笔记。"""
    bodies = section_bodies(context, top_claims=top_claims)

    lines: list[str] = [f"# {note_title(context)}", ""]
    for section in SECTION_ORDER:
        lines.append(f"## {section}")
        lines.append("")
        lines.extend(bodies[section])
        lines.append("")

    body = "\n".join(lines).rstrip("\n") + "\n"
    frontmatter = build_frontmatter(context)
    markdown = render_frontmatter(frontmatter) + "\n" + body

    return RenderedNote(
        content_id=context.content_id,
        filename=note_filename(context),
        markdown=markdown,
        frontmatter=frontmatter,
        sections=SECTION_ORDER,
    )


def render_failure_note(
    context: NoteContext,
    *,
    error_type: str,
    error_message: str,
) -> RenderedNote:
    """失败笔记（写进 ``Failed/``）：保住原文，方便人工重试。

    失败原因单独放在「处理失败」一节，所以「摘要」不再重复一遍长错误文本。
    """
    bodies = section_bodies(context)
    bodies["摘要"] = ["（本次未产出结构化摘要，原因见上方「处理失败」）"]

    note = render_note(context)
    lines: list[str] = [f"# {note_title(context)}", "", "## 处理失败", ""]
    lines.append(f"- error_type：`{error_type}`")
    lines.append(f"- error_message：{one_line(error_message)}")
    lines.append("")

    for section in SECTION_ORDER:
        lines.append(f"## {section}")
        lines.append("")
        lines.extend(bodies[section])
        lines.append("")

    body = "\n".join(lines).rstrip("\n") + "\n"
    frontmatter = dict(note.frontmatter)
    frontmatter["needs_manual_review"] = True
    markdown = render_frontmatter(frontmatter) + "\n" + body

    return RenderedNote(
        content_id=context.content_id,
        filename=note.filename,
        markdown=markdown,
        frontmatter=frontmatter,
        sections=("处理失败", *SECTION_ORDER),
    )


__all__ = [
    "SECTION_ORDER",
    "EMPTY_MARKER",
    "UNTITLED",
    "DEFAULT_TOP_CLAIMS",
    "LinkedEntity",
    "NoteContext",
    "RenderedNote",
    "one_line",
    "fenced_block",
    "note_title",
    "note_filename",
    "build_tags",
    "resolved_topics",
    "build_frontmatter",
    "render_claim",
    "render_claims",
    "render_entity",
    "core_claim_lines",
    "section_bodies",
    "render_note",
    "render_failure_note",
]
