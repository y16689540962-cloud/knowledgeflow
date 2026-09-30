"""文本预算：MAX_TRANSCRIPT_CHARS / MAX_OCR_CHARS / MAX_TOTAL_INPUT_CHARS（第十一节）。"""

from __future__ import annotations

import pytest

from app.chunking.budget import (
    DEFAULT_PROMPT_RESERVE_CHARS,
    TextBudget,
    apply_budget,
    chunk_chars_for,
)
from app.schemas import RawContent


def raw(**overrides: object) -> RawContent:
    payload = {
        "source": "manual",
        "source_id": "",
        "source_url": "",
        "media_type": "text",
        "raw_text": None,
        "transcript": None,
        "ocr_text": None,
    }
    payload.update(overrides)
    return RawContent.from_fixture(payload)


BUDGET = TextBudget(max_transcript_chars=100, max_ocr_chars=50, max_total_input_chars=200)


# --------------------------------------------------------------------------- #
# 无截断
# --------------------------------------------------------------------------- #
def test_within_budget_no_truncation() -> None:
    prepared = apply_budget(raw(raw_text="原文", transcript="逐字稿", ocr_text="识别文字"), BUDGET)
    assert prepared.text == "原文\n逐字稿\n识别文字"
    assert prepared.truncated is False
    assert prepared.transcript_truncated is False
    assert prepared.ocr_truncated is False
    assert prepared.total_truncated is False


def test_empty_raw_content() -> None:
    prepared = apply_budget(raw(), BUDGET)
    assert prepared.text == ""
    assert prepared.truncated is False
    assert prepared.raw_text_chars == 0
    assert prepared.transcript_chars == 0
    assert prepared.ocr_chars == 0


def test_blank_fields_are_skipped() -> None:
    prepared = apply_budget(raw(raw_text="", transcript="逐字稿", ocr_text=""), BUDGET)
    assert prepared.text == "逐字稿"


# --------------------------------------------------------------------------- #
# 逐字段截断
# --------------------------------------------------------------------------- #
def test_transcript_truncated_to_limit() -> None:
    prepared = apply_budget(raw(transcript="字" * 300), BUDGET)
    assert prepared.transcript_truncated is True
    assert len(prepared.text) == 100
    assert prepared.text == "字" * 100


def test_transcript_original_length_reported() -> None:
    prepared = apply_budget(raw(transcript="字" * 300), BUDGET)
    assert prepared.transcript_chars == 300


def test_ocr_truncated_to_limit() -> None:
    prepared = apply_budget(raw(ocr_text="图" * 120), BUDGET)
    assert prepared.ocr_truncated is True
    assert len(prepared.text) == 50
    assert prepared.ocr_chars == 120


def test_exact_limit_is_not_truncation() -> None:
    prepared = apply_budget(raw(transcript="字" * 100), BUDGET)
    assert prepared.transcript_truncated is False
    assert len(prepared.text) == 100


def test_truncation_keeps_the_head() -> None:
    prepared = apply_budget(raw(transcript="A" * 50 + "B" * 50 + "C" * 50), BUDGET)
    assert prepared.text == "A" * 50 + "B" * 50
    assert "C" not in prepared.text


# --------------------------------------------------------------------------- #
# 总预算
# --------------------------------------------------------------------------- #
def test_total_budget_truncates() -> None:
    prepared = apply_budget(
        raw(raw_text="原" * 90, transcript="逐" * 90, ocr_text="图" * 40),
        BUDGET,
    )
    assert prepared.total_truncated is True
    assert len(prepared.text) == 200
    assert prepared.transcript_truncated is False


def test_field_order_is_preserved() -> None:
    prepared = apply_budget(
        raw(raw_text="AAA", transcript="BBB", ocr_text="CCC"),
        TextBudget(100, 100, 100),
    )
    assert prepared.text == "AAA\nBBB\nCCC"


def test_field_budget_applies_before_total() -> None:
    prepared = apply_budget(raw(transcript="逐" * 500), BUDGET)
    assert prepared.transcript_truncated is True
    assert prepared.text == "逐" * 100


def test_truncated_flag_is_aggregate() -> None:
    assert apply_budget(raw(transcript="字" * 300), BUDGET).truncated is True
    assert apply_budget(raw(ocr_text="图" * 300), BUDGET).truncated is True
    assert apply_budget(raw(raw_text="原" * 300), BUDGET).truncated is True
    assert apply_budget(raw(raw_text="短"), BUDGET).truncated is False


# --------------------------------------------------------------------------- #
# chunk_chars
# --------------------------------------------------------------------------- #
def test_default_reserve_constant() -> None:
    assert DEFAULT_PROMPT_RESERVE_CHARS == 4000


def test_chunk_chars_leaves_room_for_prompt() -> None:
    assert chunk_chars_for(24000) == 20000


def test_chunk_chars_shrinks_reserve_for_small_budget() -> None:
    """预留量最多吃掉一半 —— 否则小预算会把 chunk 退化成逐字符切分。"""
    assert chunk_chars_for(2000) == 1000
    assert chunk_chars_for(100) == 50


def test_chunk_chars_never_below_one() -> None:
    assert chunk_chars_for(1) == 1
    assert chunk_chars_for(0) == 1


def test_chunk_chars_custom_reserve() -> None:
    assert chunk_chars_for(10000, 1000) == 9000


def test_budget_chunk_chars_property(default_budget: TextBudget) -> None:
    assert default_budget.max_total_input_chars == 24000
    assert default_budget.chunk_chars == 20000


def test_budget_from_settings(settings_defaults) -> None:
    budget = TextBudget.from_settings(settings_defaults)
    assert budget.max_transcript_chars == 12000
    assert budget.max_ocr_chars == 6000
    assert budget.max_total_input_chars == 24000


# --------------------------------------------------------------------------- #
# 真实 fixture
# --------------------------------------------------------------------------- #
def test_long_fixture_is_truncated_with_default_settings(long_raw_content, default_budget) -> None:
    prepared = apply_budget(long_raw_content, default_budget)
    assert prepared.transcript_truncated is True
    assert prepared.total_truncated is True
    assert len(prepared.text) == 24000
    assert prepared.transcript_chars > 12000
    assert prepared.raw_text_chars > 12000


def test_short_fixture_is_not_truncated(mixed_raw_content, default_budget) -> None:
    prepared = apply_budget(mixed_raw_content, default_budget)
    assert prepared.truncated is False
    assert prepared.text == mixed_raw_content.raw_text
