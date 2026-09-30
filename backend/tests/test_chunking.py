"""语义边界切分与分块（第十一节，强制）。"""

from __future__ import annotations

import pytest

from app.chunking.chunker import DEFAULT_OVERLAP_SENTENCES, Chunk, chunk_count, chunk_text
from app.chunking.sentences import normalized_sentences, split_sentences
from tests.conftest import sentence_series

# --------------------------------------------------------------------------- #
# split_sentences
# --------------------------------------------------------------------------- #
def test_split_empty_text() -> None:
    assert split_sentences("") == []


def test_split_is_lossless() -> None:
    text = "第一句。第二句！第三句？\n第四句\nEnglish sentence. Another one! 结束"
    assert "".join(split_sentences(text)) == text


def test_split_is_lossless_on_real_fixture(mixed_payload: dict) -> None:
    text = mixed_payload["raw_text"]
    assert "".join(split_sentences(text)) == text


def test_split_on_chinese_period() -> None:
    assert normalized_sentences("甲。乙。丙。") == ["甲。", "乙。", "丙。"]


def test_split_on_newline() -> None:
    assert normalized_sentences("第一行\n第二行") == ["第一行", "第二行"]


def test_split_on_ascii_sentence_end() -> None:
    assert normalized_sentences("Hello! How are you? Fine.") == ["Hello!", "How are you?", "Fine."]


def test_decimal_is_not_split() -> None:
    """数字里的小数点绝不能当句末标点。"""
    assert normalized_sentences("圆周率是 3.14 左右。") == ["圆周率是 3.14 左右。"]


def test_grouped_punctuation_stays_together() -> None:
    assert normalized_sentences("真的吗？！我不信！！") == ["真的吗？！", "我不信！！"]


def test_blank_lines_are_dropped_by_normalizer() -> None:
    assert normalized_sentences("甲。\n\n\n乙。") == ["甲。", "乙。"]


def test_semicolon_is_not_a_boundary() -> None:
    """文档只把 。.!?！？\\n 当边界；句内分号不切。"""
    assert normalized_sentences("甲；乙。") == ["甲；乙。"]


# --------------------------------------------------------------------------- #
# chunk_text：基础
# --------------------------------------------------------------------------- #
def test_empty_text_produces_no_chunks() -> None:
    assert chunk_text("", max_chars=100) == []
    assert chunk_text("   \n  ", max_chars=100) == []


def test_short_text_produces_single_chunk() -> None:
    chunks = chunk_text("甲。乙。", max_chars=100)
    assert len(chunks) == 1
    assert chunks[0].index == 0
    assert chunks[0].text == "甲。乙。"
    assert chunks[0].char_count == 4


def test_invalid_max_chars_raises() -> None:
    with pytest.raises(ValueError):
        chunk_text("甲。", max_chars=0)
    with pytest.raises(ValueError):
        chunk_text("甲。", max_chars=-5)


def test_negative_overlap_raises() -> None:
    with pytest.raises(ValueError):
        chunk_text("甲。", max_chars=10, overlap_sentences=-1)


# --------------------------------------------------------------------------- #
# chunk_text：不变量
# --------------------------------------------------------------------------- #
def test_chunks_are_never_empty() -> None:
    chunks = chunk_text(sentence_series(20), max_chars=40)
    assert all(chunk.text.strip() for chunk in chunks)


def test_every_chunk_within_limit() -> None:
    chunks = chunk_text(sentence_series(20), max_chars=40)
    assert all(chunk.char_count <= 40 for chunk in chunks)


def test_coverage_every_sentence_appears_somewhere() -> None:
    text = sentence_series(17)
    chunks = chunk_text(text, max_chars=40)
    for sentence in normalized_sentences(text):
        assert any(sentence in chunk.text for chunk in chunks), sentence


def test_indices_are_sequential() -> None:
    chunks = chunk_text(sentence_series(20), max_chars=40)
    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))


def test_deterministic() -> None:
    text = sentence_series(20)
    assert [c.text for c in chunk_text(text, max_chars=40)] == [
        c.text for c in chunk_text(text, max_chars=40)
    ]


def test_hard_split_fallback_for_overlong_sentence() -> None:
    """单句超长时只能硬切，但仍然不许超过上限。"""
    text = "字" * 250 + "。"
    chunks = chunk_text(text, max_chars=100)
    assert len(chunks) == 3
    assert all(chunk.char_count <= 100 for chunk in chunks)
    assert "".join(chunk.text for chunk in chunks) == text


def test_hard_split_keeps_coverage() -> None:
    text = "字" * 250 + "。"
    chunks = chunk_text(text, max_chars=100)
    assert "".join(chunk.text for chunk in chunks).startswith("字" * 250)


# --------------------------------------------------------------------------- #
# chunk_text：overlap
# --------------------------------------------------------------------------- #
def test_default_overlap_is_one_sentence() -> None:
    assert DEFAULT_OVERLAP_SENTENCES == 1


def test_overlap_repeats_previous_last_sentence() -> None:
    text = sentence_series(9)
    chunks = chunk_text(text, max_chars=40, overlap_sentences=1)
    assert len(chunks) > 1
    for previous, current in zip(chunks, chunks[1:]):
        last_sentence = normalized_sentences(previous.text)[-1]
        assert current.text.startswith(last_sentence), (previous.index, current.index)


def test_no_overlap_when_disabled() -> None:
    text = sentence_series(9)
    chunks = chunk_text(text, max_chars=40, overlap_sentences=0)
    assert len(chunks) > 1
    for previous, current in zip(chunks, chunks[1:]):
        last_sentence = normalized_sentences(previous.text)[-1]
        assert not current.text.startswith(last_sentence)


def test_overlap_can_be_two_sentences() -> None:
    text = sentence_series(12)
    chunks = chunk_text(text, max_chars=48, overlap_sentences=2)
    first_two = normalized_sentences(chunks[0].text)[-2:]
    assert chunks[1].text.startswith("".join(first_two))


def test_chunk_count_matches_length() -> None:
    text = sentence_series(20)
    assert chunk_count(text, max_chars=40) == len(chunk_text(text, max_chars=40))


def test_chunk_is_frozen_dataclass() -> None:
    chunk = Chunk(index=0, text="甲。")
    with pytest.raises(Exception):
        chunk.text = "乙。"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# 真实超长 fixture
# --------------------------------------------------------------------------- #
def is_sentence_concatenation(chunk_text: str, sentences: list[str]) -> bool:
    """判断 ``chunk_text`` 是否由**连续的完整句子**拼成（证明没有切在句子中间）。"""
    consumed = 0
    for sentence in sentences:
        if consumed >= len(chunk_text):
            break
        if chunk_text.startswith(sentence, consumed):
            consumed += len(sentence)
    return consumed == len(chunk_text)


def test_long_fixture_is_split_by_semantics(long_payload: dict) -> None:
    text = (long_payload["raw_text"] or "") + "\n" + (long_payload["transcript"] or "")
    chunks = chunk_text(text, max_chars=2000)
    sentences = normalized_sentences(text)

    assert len(chunks) > 10
    assert all(chunk.char_count <= 2000 for chunk in chunks)
    # 每个 chunk 都是完整句子的拼接 —— 没有任何一个 chunk 切在句子中间
    assert all(is_sentence_concatenation(chunk.text, sentences) for chunk in chunks)
