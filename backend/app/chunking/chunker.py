"""语义边界分块 + 句级 overlap（第十一节，强制）。

规则：

* 按语义边界切分（段落 → 句末标点），**禁止固定字符数硬切**
* 单句超长才退化为硬切（最后手段）
* chunk 之间保留句级 overlap，避免边界丢信息
* ``chunk_count`` 由 :func:`chunk_text` 的结果长度给出
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.chunking.sentences import normalized_sentences

#: 默认句级 overlap。
DEFAULT_OVERLAP_SENTENCES: Final[int] = 1


@dataclass(frozen=True)
class Chunk:
    index: int
    text: str

    @property
    def char_count(self) -> int:
        return len(self.text)


def _hard_split(text: str, max_chars: int) -> list[str]:
    """超长单句的最后手段：按字符数切，但每个切片都不超过 ``max_chars``。"""
    return [text[i : i + max_chars] for i in range(0, len(text), max_chars)]


def _pieces(text: str, max_chars: int) -> list[str]:
    pieces: list[str] = []
    for sentence in normalized_sentences(text):
        if len(sentence) <= max_chars:
            pieces.append(sentence)
        else:
            pieces.extend(_hard_split(sentence, max_chars))
    return pieces


def chunk_text(
    text: str,
    *,
    max_chars: int,
    overlap_sentences: int = DEFAULT_OVERLAP_SENTENCES,
) -> list[Chunk]:
    """把文本分成若干块。

    保证：

    * 每个 chunk 非空
    * 除硬切产生的切片外，单个 chunk 长度 ≤ ``max_chars``
      （overlap 前缀会先被裁剪到留出空间）
    * 覆盖率：源文本的每一句至少出现在一个 chunk 里
    * 确定性：同样的输入必然得到同样的输出
    """
    if max_chars <= 0:
        raise ValueError("max_chars 必须为正整数")
    if overlap_sentences < 0:
        raise ValueError("overlap_sentences 不能为负")

    pieces = _pieces(text, max_chars)
    if not pieces:
        return []

    grouped: list[list[str]] = []
    current: list[str] = []
    current_len = 0

    for piece in pieces:
        if current and current_len + len(piece) > max_chars:
            grouped.append(current)
            prefix = current[-overlap_sentences:] if overlap_sentences else []
            # 前缀不能吃掉全部预算，否则新句永远放不进去
            while prefix and sum(len(item) for item in prefix) + len(piece) > max_chars:
                prefix = prefix[1:]
            current = list(prefix)
            current_len = sum(len(item) for item in current)

        current.append(piece)
        current_len += len(piece)

    if current:
        grouped.append(current)

    return [
        Chunk(index=index, text="".join(group))
        for index, group in enumerate(grouped)
    ]


def chunk_count(text: str, *, max_chars: int, overlap_sentences: int = DEFAULT_OVERLAP_SENTENCES) -> int:
    return len(chunk_text(text, max_chars=max_chars, overlap_sentences=overlap_sentences))


__all__ = ["Chunk", "chunk_text", "chunk_count", "DEFAULT_OVERLAP_SENTENCES"]
