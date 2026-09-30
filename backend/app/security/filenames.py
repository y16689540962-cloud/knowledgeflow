"""Obsidian 文件名安全（定稿文档第十八节，强制）。

* 剔除 ``\\ / : * ? " < > |`` 及控制字符
* 压缩连续空白
* 最大 **80 Unicode code points**
* **不得切断**中文字符、emoji、surrogate pair（按码点截断）
* 标题为空 → 回退 ``content_id[:8]``
* Windows 保留设备名（``CON`` ``PRN`` ``AUX`` ``NUL`` ``COM1-9`` ``LPT1-9``）→ 加前缀 ``_``
"""

from __future__ import annotations

import re
from typing import Final

MAX_FILENAME_CODE_POINTS: Final[int] = 80
FALLBACK_TITLE: Final[str] = "untitled"

_ILLEGAL_CHARS: Final[frozenset[str]] = frozenset('\\/:*?"<>|')
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE_RE = re.compile(r"\s+")
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u2028\u2029\ufeff]")

WINDOWS_RESERVED_NAMES: Final[frozenset[str]] = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def _strip_illegal(text: str) -> str:
    kept = [ch for ch in text if ch not in _ILLEGAL_CHARS and not _CONTROL_RE.match(ch)]
    return "".join(kept)


def _truncate_code_points(text: str, limit: int) -> str:
    """按 Unicode 码点截断。

    Python 的 ``str`` 本身以码点为单位（不存在半个 surrogate pair），
    因此切片永远不会把中文、emoji 或 surrogate pair 从中间切断。
    """
    if len(text) <= limit:
        return text
    return text[:limit]


def _is_windows_reserved(name: str) -> bool:
    """``CON`` / ``con.txt`` / ``LPT1`` 都算保留名。"""
    stem = name.split(".", 1)[0]
    return stem.upper() in WINDOWS_RESERVED_NAMES


def sanitize_filename(title: str | None, fallback_id: str) -> str:
    """把任意标题变成安全的文件名（不含扩展名）。

    **刻意不做 NFKC 归一化**：NFKC 会把中文全角标点折成 ASCII
    （``，`` → ``,``、``：`` → ``:``），而 ``:`` 恰好在非法字符表里，
    结果是「标题：副标题」被吃成「标题副标题」—— 中文标题被无声改写。
    文件名不参与去重，因此保持原字符是对的。
    """
    raw = "" if title is None else str(title)

    text = _ZERO_WIDTH_RE.sub("", raw)
    text = _strip_illegal(text)
    text = _WHITESPACE_RE.sub(" ", text)  # 全角空格 U+3000 也命中 \s，会被压成一个半角空格

    # 前后空白、点号一律去掉：避免隐藏文件与 Windows 尾随点/空格。
    text = text.strip().strip(".").strip()
    text = _truncate_code_points(text, MAX_FILENAME_CODE_POINTS)
    text = text.strip().strip(".").strip()

    if not text:
        text = normalize_fallback(fallback_id)

    if _is_windows_reserved(text):
        text = "_" + text

    return text


def normalize_fallback(fallback_id: str | None) -> str:
    """``content_id[:8]``；连 id 都没有时用 ``untitled``。"""
    candidate = _truncate_code_points(_strip_illegal(str(fallback_id or "")).strip(), 8)
    candidate = _truncate_code_points(candidate, 8).strip().strip(".")
    return candidate or FALLBACK_TITLE


__all__ = [
    "MAX_FILENAME_CODE_POINTS",
    "FALLBACK_TITLE",
    "WINDOWS_RESERVED_NAMES",
    "sanitize_filename",
    "normalize_fallback",
]
