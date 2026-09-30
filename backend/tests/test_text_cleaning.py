"""源文本清理：只做格式级清理（第十一节「清理」环节）。"""

from __future__ import annotations

from app.chunking.budget import TextBudget, apply_budget
from app.chunking.cleaning import MAX_BLANK_LINES, clean_source_text
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


# --------------------------------------------------------------------------- #
# 无变化
# --------------------------------------------------------------------------- #
def test_clean_text_is_unchanged() -> None:
    cleaned = clean_source_text("甲。\n乙。")
    assert cleaned.text == "甲。\n乙。"
    assert cleaned.changed is False


def test_empty_text() -> None:
    cleaned = clean_source_text("")
    assert cleaned.text == ""
    assert cleaned.changed is False


# --------------------------------------------------------------------------- #
# 换行与零宽
# --------------------------------------------------------------------------- #
def test_crlf_is_normalized() -> None:
    cleaned = clean_source_text("甲。\r\n乙。")
    assert cleaned.text == "甲。\n乙。"
    assert cleaned.newline_normalized is True


def test_lone_cr_is_normalized() -> None:
    cleaned = clean_source_text("甲。\r乙。")
    assert cleaned.text == "甲。\n乙。"
    assert cleaned.newline_normalized is True


def test_zero_width_chars_removed() -> None:
    cleaned = clean_source_text("甲\u200b乙\u200d丙\ufeff")
    assert cleaned.text == "甲乙丙"
    assert cleaned.zero_width_removed is True


# --------------------------------------------------------------------------- #
# 空白行
# --------------------------------------------------------------------------- #
def test_many_blank_lines_collapsed() -> None:
    cleaned = clean_source_text("甲。\n\n\n\n\n乙。")
    assert cleaned.blank_lines_collapsed == 2
    # 保留 MAX_BLANK_LINES 个空行 → 3 个换行符
    assert cleaned.text == "甲。" + "\n" * (MAX_BLANK_LINES + 1) + "乙。"


def test_up_to_max_blank_lines_kept() -> None:
    text = "甲。\n" + "\n" * MAX_BLANK_LINES + "乙。"
    cleaned = clean_source_text(text)
    assert cleaned.blank_lines_collapsed == 0
    assert cleaned.text.count("\n") == MAX_BLANK_LINES + 1


# --------------------------------------------------------------------------- #
# 紧邻重复行
# --------------------------------------------------------------------------- #
def test_consecutive_duplicate_lines_removed() -> None:
    cleaned = clean_source_text("同一句话。\n同一句话。\n同一句话。\n下一句。")
    assert cleaned.text == "同一句话。\n下一句。"
    assert cleaned.duplicate_lines_removed == 2


def test_non_consecutive_repetition_is_kept() -> None:
    """重复出现常常是刻意的强调 —— 只要不相邻就保留。"""
    text = "甲。\n乙。\n甲。"
    cleaned = clean_source_text(text)
    assert cleaned.text == text
    assert cleaned.duplicate_lines_removed == 0


def test_duplicate_detection_ignores_surrounding_whitespace() -> None:
    cleaned = clean_source_text("甲。\n  甲。  \n乙。")
    assert cleaned.text == "甲。\n乙。"


def test_blank_lines_break_adjacency() -> None:
    text = "甲。\n\n甲。"
    cleaned = clean_source_text(text)
    assert cleaned.text == text


# --------------------------------------------------------------------------- #
# apply_budget 集成
# --------------------------------------------------------------------------- #
def test_cleaning_runs_inside_apply_budget() -> None:
    prepared = apply_budget(raw(raw_text="甲。\r\n甲。\r\n乙。"), TextBudget(100, 100, 1000))
    assert prepared.text == "甲。\n乙。"
    assert prepared.cleaned is True
    assert prepared.cleaning is not None
    assert prepared.cleaning.duplicate_lines_removed == 1


def test_cleaning_happens_before_total_truncation() -> None:
    """先清理再截断 —— 否则噪声会白白吃掉预算。"""
    body = "有效内容。" * 6
    text = body + "\n" + body
    prepared = apply_budget(raw(raw_text=text), TextBudget(10000, 10000, 10000))
    assert prepared.text == body


def test_cleaning_does_not_change_clean_input(mixed_raw_content) -> None:
    prepared = apply_budget(mixed_raw_content, TextBudget(12000, 6000, 24000))
    assert prepared.text == mixed_raw_content.raw_text
    assert prepared.cleaned is False


def test_cleaned_flag_false_when_nothing_changed() -> None:
    prepared = apply_budget(raw(raw_text="甲。\n乙。"), TextBudget(100, 100, 1000))
    assert prepared.cleaned is False
