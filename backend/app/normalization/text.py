"""文本归一化。

顺序固定：NFKC → 去零宽/控制字符 → 连续空白压缩 → trim → 空值转空串。
"""

from __future__ import annotations

import re
import unicodedata

_WHITESPACE_RE = re.compile(r"\s+")
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u2028\u2029\ufeff]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def normalize_text(value: object) -> str:
    """把任意输入归一化成稳定的字符串。

    ``None`` → ``""``；全角/半角统一（NFKC）；连续空白（含换行/全角空格）压成一个空格。
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = unicodedata.normalize("NFKC", text)
    text = _ZERO_WIDTH_RE.sub("", text)
    text = _CONTROL_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip()


def normalize_optional(value: object) -> str | None:
    """归一化后若为空串则返回 ``None``（用于可选字段）。"""
    text = normalize_text(value)
    return text or None


def is_blank(value: object) -> bool:
    return not normalize_text(value)


__all__ = ["normalize_text", "normalize_optional", "is_blank"]
