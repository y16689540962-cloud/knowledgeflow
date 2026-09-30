"""源文本清理（第十一节的「清理」环节）。

只做**格式级**清理，绝不改写内容：

* CRLF / CR → LF
* 去掉零宽字符与 BOM
* 折叠 3 行以上连续空行 → 1 行空行
* 去掉**紧邻重复**的行（ASR 常见的抖动噪声）

刻意**不做**内容级去重（那属于 Phase 5 的 Deduplication，按第五节以
``source_id`` / ``content_hash`` 判定，不在这里做）。
非相邻的重复内容一律保留 —— 重复出现常常是有意义的强调。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

ZERO_WIDTH_CHARS: Final[str] = "\u200b\u200c\u200d\u200e\u200f\u2028\u2029\ufeff"

#: 连续多少个以上空行才折叠。
MAX_BLANK_LINES: Final[int] = 2


@dataclass(frozen=True)
class CleanedText:
    text: str
    newline_normalized: bool
    zero_width_removed: bool
    duplicate_lines_removed: int
    blank_lines_collapsed: int

    @property
    def changed(self) -> bool:
        return bool(
            self.newline_normalized
            or self.zero_width_removed
            or self.duplicate_lines_removed
            or self.blank_lines_collapsed
        )


def clean_source_text(text: str) -> CleanedText:
    if not text:
        return CleanedText(
            text="",
            newline_normalized=False,
            zero_width_removed=False,
            duplicate_lines_removed=0,
            blank_lines_collapsed=0,
        )

    newline_normalized = False
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        newline_normalized = True

    zero_width_removed = any(char in text for char in ZERO_WIDTH_CHARS)
    if zero_width_removed:
        for char in ZERO_WIDTH_CHARS:
            text = text.replace(char, "")

    lines = text.split("\n")
    kept: list[str] = []
    duplicate_lines_removed = 0
    blank_lines_collapsed = 0
    previous_content: str | None = None
    blank_run = 0

    for line in lines:
        stripped = line.strip()
        if not stripped:
            blank_run += 1
            # 空行断开「相邻」关系：空行隔开的重复内容多半是刻意的强调。
            previous_content = None
            if blank_run > MAX_BLANK_LINES:
                blank_lines_collapsed += 1
                continue
            kept.append("")
            continue

        blank_run = 0
        if previous_content is not None and stripped == previous_content:
            duplicate_lines_removed += 1
            continue
        previous_content = stripped
        kept.append(line)

    return CleanedText(
        text="\n".join(kept).strip("\n"),
        newline_normalized=newline_normalized,
        zero_width_removed=zero_width_removed,
        duplicate_lines_removed=duplicate_lines_removed,
        blank_lines_collapsed=blank_lines_collapsed,
    )


__all__ = ["CleanedText", "clean_source_text", "ZERO_WIDTH_CHARS", "MAX_BLANK_LINES"]
