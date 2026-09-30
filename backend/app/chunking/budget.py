"""文本预算（第十一节）。

三个预算项来自配置（``MAX_TRANSCRIPT_CHARS`` / ``MAX_OCR_CHARS`` /
``MAX_TOTAL_INPUT_CHARS``）：

1. ``transcript`` 截到 ``MAX_TRANSCRIPT_CHARS``
2. ``ocr_text`` 截到 ``MAX_OCR_CHARS``
3. 拼好的 source text 再整体截到 ``MAX_TOTAL_INPUT_CHARS``

截断只保留**头部**（时间轴上更早、通常也是更核心的内容），并把截断事实记下来，
不允许静默丢弃。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.chunking.cleaning import CleanedText, clean_source_text
from app.config import Settings
from app.schemas.content import RawContent

#: 给 prompt 指令 + JSON schema 预留的字符预算（约 2000 tokens）。
#: 单次 LLM 调用的文本上限 = ``MAX_TOTAL_INPUT_CHARS - 预留量``，
#: 否则 chunk + 指令会一起超出模型上下文。
DEFAULT_PROMPT_RESERVE_CHARS = 4000


def chunk_chars_for(
    max_total_input_chars: int,
    reserve: int = DEFAULT_PROMPT_RESERVE_CHARS,
) -> int:
    """算单次 LLM 调用的源文本上限。

    预留量最多吃掉一半预算 —— 否则把 ``MAX_TOTAL_INPUT_CHARS`` 调得很小时
    （例如测试里设成 2000），预留量会大于预算，chunk 退化成逐字符切分。
    """
    total = max(1, int(max_total_input_chars))
    effective_reserve = min(max(0, int(reserve)), total // 2)
    return max(1, total - effective_reserve)


@dataclass(frozen=True)
class TextBudget:
    max_transcript_chars: int
    max_ocr_chars: int
    max_total_input_chars: int

    @classmethod
    def from_settings(cls, settings: Settings) -> "TextBudget":
        return cls(
            max_transcript_chars=settings.max_transcript_chars,
            max_ocr_chars=settings.max_ocr_chars,
            max_total_input_chars=settings.max_total_input_chars,
        )

    @property
    def chunk_chars(self) -> int:
        """单次 LLM 调用里允许的源文本长度（用默认预留量）。"""
        return chunk_chars_for(self.max_total_input_chars)


@dataclass(frozen=True)
class PreparedText:
    text: str
    raw_text_chars: int
    transcript_chars: int
    ocr_chars: int
    transcript_truncated: bool
    ocr_truncated: bool
    total_truncated: bool
    cleaning: CleanedText | None = None

    @property
    def truncated(self) -> bool:
        return self.transcript_truncated or self.ocr_truncated or self.total_truncated

    @property
    def cleaned(self) -> bool:
        return bool(self.cleaning and self.cleaning.changed)


def _cut(value: str | None, limit: int) -> tuple[str, bool, int]:
    text = value or ""
    if len(text) <= limit:
        return text, False, len(text)
    return text[:limit], True, len(text)


def apply_budget(raw: RawContent, budget: TextBudget) -> PreparedText:
    """按预算裁剪、清理并拼接 ``raw_text + transcript + ocr_text``。

    顺序：逐字段截断 → 拼接 → **清理**（CRLF / 零宽 / 紧邻重复行 / 空行）→ 总预算截断。
    """
    raw_text = raw.raw_text or ""
    transcript, transcript_truncated, transcript_len = _cut(
        raw.transcript, budget.max_transcript_chars
    )
    ocr_text, ocr_truncated, ocr_len = _cut(raw.ocr_text, budget.max_ocr_chars)

    parts = [part for part in (raw_text, transcript, ocr_text) if part]
    text = "\n".join(parts)

    cleaning = clean_source_text(text)
    text = cleaning.text

    total_truncated = False
    if len(text) > budget.max_total_input_chars:
        text = text[: budget.max_total_input_chars]
        total_truncated = True

    return PreparedText(
        text=text,
        raw_text_chars=len(raw_text),
        transcript_chars=transcript_len,
        ocr_chars=ocr_len,
        transcript_truncated=transcript_truncated,
        ocr_truncated=ocr_truncated,
        total_truncated=total_truncated,
        cleaning=cleaning,
    )


__all__ = [
    "TextBudget",
    "PreparedText",
    "apply_budget",
    "chunk_chars_for",
    "DEFAULT_PROMPT_RESERVE_CHARS",
    "CleanedText",
    "clean_source_text",
]
