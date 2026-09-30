"""``sanitize_filename``（第十八节，强制）。"""

from __future__ import annotations

import pytest

from app.security import (
    MAX_FILENAME_CODE_POINTS,
    WINDOWS_RESERVED_NAMES,
    sanitize_filename,
)


def test_plain_title_kept() -> None:
    assert sanitize_filename("人口下降之后，房子还会涨吗", "abcdef12") == "人口下降之后，房子还会涨吗"


@pytest.mark.parametrize("char", list('\\/:*?"<>|'))
def test_illegal_characters_removed(char: str) -> None:
    assert sanitize_filename(f"a{char}b", "abcdef12") == "ab"


def test_slash_cannot_survive() -> None:
    """文件名里绝不能留下路径分隔符。"""
    result = sanitize_filename("../../etc/passwd", "abcdef12")
    assert "/" not in result
    assert "\\" not in result


def test_control_characters_removed() -> None:
    assert sanitize_filename("a\x00b\x1fc\x7f", "abcdef12") == "abc"


def test_whitespace_collapsed_and_trimmed() -> None:
    assert sanitize_filename("  a   b \n c  ", "abcdef12") == "a b c"


def test_fullwidth_space_collapsed() -> None:
    assert sanitize_filename("ＡＩ　Ａｇｅｎｔ", "abcdef12") == "ＡＩ Ａｇｅｎｔ"


def test_chinese_fullwidth_punctuation_preserved() -> None:
    """不做 NFKC：中文全角标点必须原样保留，否则「标题：副标题」会被吃掉冒号。"""
    assert sanitize_filename("标题：副标题，很关键？", "abcdef12") == "标题：副标题，很关键？"


def test_length_limited_by_code_points() -> None:
    title = "汉" * 200
    result = sanitize_filename(title, "abcdef12")
    assert len(result) == MAX_FILENAME_CODE_POINTS
    assert result == "汉" * MAX_FILENAME_CODE_POINTS


def test_chinese_never_cut_in_half() -> None:
    title = "知识" * 100
    result = sanitize_filename(title, "abcdef12")
    assert len(result) == 80
    assert len(result.encode("utf-8")) == 80 * 3


def test_emoji_never_cut_in_half() -> None:
    """按码点截断 → emoji 不会被截成半个 surrogate pair。"""
    title = "🎬" * 120
    result = sanitize_filename(title, "abcdef12")
    assert len(result) == MAX_FILENAME_CODE_POINTS
    assert result == "🎬" * MAX_FILENAME_CODE_POINTS
    assert result.encode("utf-8").decode("utf-8") == result


def test_combining_zwj_sequence_survives_roundtrip() -> None:
    title = "👨‍👩‍👧‍👦" * 30
    result = sanitize_filename(title, "abcdef12")
    # 截断可能落在 ZWJ 序列内部，但字符本身必须是合法码点序列
    assert result.encode("utf-8").decode("utf-8") == result
    assert len(result) <= MAX_FILENAME_CODE_POINTS


@pytest.mark.parametrize("title", [None, "", "   ", "..."])
def test_empty_title_falls_back_to_content_id(title: str | None) -> None:
    assert sanitize_filename(title, "3f2a1b0c9d8e7f6a") == "3f2a1b0c"


def test_fallback_uses_first_eight_chars() -> None:
    assert sanitize_filename("", "0123456789abcdef") == "01234567"


def test_fallback_when_id_too_short() -> None:
    assert sanitize_filename("", "ab") == "ab"


def test_fallback_when_nothing_available() -> None:
    assert sanitize_filename("", "") == "untitled"


def test_title_made_only_of_illegal_chars_falls_back() -> None:
    assert sanitize_filename('///:::***', "abcdef12") == "abcdef12"


def test_leading_and_trailing_dots_stripped() -> None:
    result = sanitize_filename("...hidden.md...", "abcdef12")
    assert not result.startswith(".")
    assert not result.endswith(".")


@pytest.mark.parametrize("name", sorted(WINDOWS_RESERVED_NAMES))
def test_windows_reserved_names_prefixed(name: str) -> None:
    assert sanitize_filename(name, "abcdef12") == "_" + name


def test_windows_reserved_names_case_insensitive() -> None:
    assert sanitize_filename("con", "abcdef12") == "_con"
    assert sanitize_filename("Nul", "abcdef12") == "_Nul"


def test_windows_reserved_with_extension() -> None:
    assert sanitize_filename("CON.txt", "abcdef12") == "_CON.txt"
    assert sanitize_filename("lpt9.md", "abcdef12") == "_lpt9.md"


def test_reserved_lookalike_is_not_prefixed() -> None:
    assert sanitize_filename("CONSOLE", "abcdef12") == "CONSOLE"
    assert sanitize_filename("COM10", "abcdef12") == "COM10"


def test_zero_width_removed() -> None:
    assert sanitize_filename("a\u200bb", "abcdef12") == "ab"


def test_result_is_deterministic() -> None:
    assert sanitize_filename("同一标题", "x") == sanitize_filename("同一标题", "x")
