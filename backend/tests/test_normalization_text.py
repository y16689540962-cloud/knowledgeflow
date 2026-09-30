"""文本归一化。"""

from __future__ import annotations

import pytest

from app.normalization import is_blank, normalize_optional, normalize_text


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        ("", ""),
        ("   ", ""),
        ("\n\t ", ""),
        ("  abc  ", "abc"),
        ("a   b", "a b"),
        ("a\nb", "a b"),
        ("a\t\tb", "a b"),
        ("ａｂｃ", "abc"),  # 全角 → 半角（NFKC）
        ("ＡＩ", "AI"),
        ("１２００", "1200"),
        ("中文　空格", "中文 空格"),  # 全角空格
        ("a\u200bb", "ab"),  # 零宽空格
        ("\ufeffbom", "bom"),
        ("a\x00b", "ab"),  # 控制字符
        (123, "123"),
    ],
)
def test_normalize_text(value: object, expected: str) -> None:
    assert normalize_text(value) == expected


def test_normalize_text_is_idempotent() -> None:
    once = normalize_text("  ＡＩ   Ａｇｅｎｔ \n")
    assert normalize_text(once) == once


def test_normalize_optional_returns_none_for_blank() -> None:
    assert normalize_optional("   ") is None
    assert normalize_optional(None) is None
    assert normalize_optional(" x ") == "x"


def test_is_blank() -> None:
    assert is_blank(None)
    assert is_blank("　")
    assert not is_blank("x")
