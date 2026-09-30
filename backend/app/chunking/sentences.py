"""按语义边界切分句子（第十一节，强制）。

禁止按固定字符数硬切（会断在句子中间）。这里只做「按标点切分」这一件事，
超长单句的退化处理在 ``chunker`` 里，且是**最后手段**。

边界字符严格按定稿文档：

* 第一优先：段落（换行）
* 其次：``。`` ``.`` ``!`` ``?`` ``！`` ``？``
"""

from __future__ import annotations

from typing import Final

#: 句末标点（严格按文档列举，刻意不含 ``；`` / ``，`` 这类句内标点）。
SENTENCE_END_CHARS: Final[str] = "。！？!?"
PARAGRAPH_BREAK: Final[str] = "\n"

_BOUNDARY_CHARS = set(SENTENCE_END_CHARS)


def split_sentences(text: str) -> list[str]:
    """切分为句子，**不丢字符**。

    不变式：``"".join(split_sentences(text)) == text``。

    细节：
    * 换行符一律作为边界（段落优先）
    * ``.`` 作为边界，但**后面紧跟数字时不切**（避免把 ``3.14`` 切开）
    * 连写的句末标点（``！？``）合并进同一句
    """
    if not text:
        return []

    sentences: list[str] = []
    buffer: list[str] = []
    index = 0
    length = len(text)

    while index < length:
        char = text[index]
        buffer.append(char)

        if char == PARAGRAPH_BREAK:
            sentences.append("".join(buffer))
            buffer = []
        elif char in _BOUNDARY_CHARS:
            # 吃掉连写的句末标点
            while index + 1 < length and text[index + 1] in _BOUNDARY_CHARS:
                index += 1
                buffer.append(text[index])
            sentences.append("".join(buffer))
            buffer = []
        elif char == ".":
            following = text[index + 1] if index + 1 < length else ""
            if not following.isdigit():
                sentences.append("".join(buffer))
                buffer = []

        index += 1

    if buffer:
        sentences.append("".join(buffer))
    return sentences


def normalized_sentences(text: str) -> list[str]:
    """切分 + 去空白，丢掉空句。用于分块（不保证能拼回原文）。"""
    return [stripped for stripped in (s.strip() for s in split_sentences(text)) if stripped]


__all__ = [
    "SENTENCE_END_CHARS",
    "PARAGRAPH_BREAK",
    "split_sentences",
    "normalized_sentences",
]
