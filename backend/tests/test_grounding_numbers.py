"""关键数字抽取与等价性判定（R2.2 的基础）。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.grounding.numbers import (
    NumberToken,
    describe_numbers,
    extract_key_numbers,
    extract_numbers,
    numbers_grounded,
    parse_chinese_number,
    scale_of,
    ungrounded_numbers,
)


def values(text: str) -> list[Decimal]:
    return [token.value for token in extract_key_numbers(text)]


def raws(text: str) -> list[str]:
    return [token.raw for token in extract_key_numbers(text)]


# --------------------------------------------------------------------------- #
# 中文数字解析
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("一", 1),
        ("十", 10),
        ("十五", 15),
        ("二十五", 25),
        ("一百六十", 160),
        ("一千六百", 1600),
        ("一千六百万", 16_000_000),
        ("一千万", 10_000_000),
        ("九亿二千万", 920_000_000),
        ("两", 2),
    ],
)
def test_parse_chinese_number(text: str, expected: int) -> None:
    assert parse_chinese_number(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "甲", "一二三abc"])
def test_parse_chinese_number_rejects_garbage(text: str) -> None:
    assert parse_chinese_number(text) is None


def test_scale_of() -> None:
    """只认万/亿；百/千的量级已经体现在 value 里。"""
    assert scale_of("一千六百") == 1
    assert scale_of("一百") == 1
    assert scale_of("1600 万") == 10**4
    assert scale_of("一千万") == 10**4
    assert scale_of("一亿") == 10**8
    assert scale_of("常态") == 1


# --------------------------------------------------------------------------- #
# 阿拉伯数字
# --------------------------------------------------------------------------- #
def test_arabic_with_scale() -> None:
    tokens = extract_numbers("从每年 1600 万降到不足 1000 万")
    assert [(t.value, t.scale) for t in tokens] == [
        (Decimal(16_000_000), 10**4),
        (Decimal(10_000_000), 10**4),
    ]


def test_decimal_with_scale() -> None:
    tokens = extract_key_numbers("预测降到 9.2 亿")
    assert tokens[0].value == Decimal("920000000")
    assert tokens[0].scale == 10**8


def test_fullwidth_digits_are_folded() -> None:
    assert values("一共 １６００万") == values("一共 1600万")


def test_percent_arabic() -> None:
    tokens = extract_numbers("成功率大概 90%")
    assert tokens[0].kind == "percent"
    assert tokens[0].value == Decimal(90)


def test_percent_chinese() -> None:
    tokens = extract_numbers("成功率大概百分之九十")
    assert tokens[0].kind == "percent"
    assert tokens[0].value == Decimal(90)


def test_percent_is_not_double_counted() -> None:
    assert len(extract_numbers("恰好 90%")) == 1


def test_fullwidth_percent_sign() -> None:
    tokens = extract_numbers("增长率 30％")
    assert tokens[0].kind == "percent"


# --------------------------------------------------------------------------- #
# 「关键数字」过滤
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text", ["第 1 步", "第 3 次尝试", "圆周率是 3.14", "一共 5 个"])
def test_noise_numbers_are_not_key(text: str) -> None:
    assert extract_key_numbers(text) == ()


def test_two_digit_number_is_key() -> None:
    assert values("一共 25 个") == [Decimal(25)]


def test_four_digit_year_is_key() -> None:
    assert values("2035 年的预测") == [Decimal(2035)]


@pytest.mark.parametrize("text", ["十分重要", "百姓的生活", "一时之间"])
def test_single_char_chinese_numbers_are_not_key(text: str) -> None:
    """「十分」「百姓」这类词里的单字数字不能当成证据要求。"""
    assert extract_key_numbers(text) == ()


def test_scaled_chinese_number_is_key() -> None:
    assert values("一千万") == [Decimal(10_000_000)]


def test_multi_char_chinese_number_is_key() -> None:
    assert values("一百六十人") == [Decimal(160)]


# --------------------------------------------------------------------------- #
# 模糊量词
# --------------------------------------------------------------------------- #
def test_fuzzy_marker_is_recorded() -> None:
    tokens = extract_key_numbers("一千六百多万")
    assert tokens[0].fuzzy is True
    assert tokens[0].value == Decimal(16_000_000)
    assert tokens[0].scale == 10**4


def test_exact_number_is_not_fuzzy() -> None:
    assert extract_key_numbers("一千六百万")[0].fuzzy is False


# --------------------------------------------------------------------------- #
# 等价性判定
# --------------------------------------------------------------------------- #
def test_identical_numbers_are_grounded() -> None:
    claim = extract_key_numbers("从 1600 万降到 1000 万")
    source = extract_key_numbers("从每年 1600 万降到不足 1000 万")
    assert numbers_grounded(claim, source) is True


def test_missing_number_is_not_grounded() -> None:
    claim = extract_key_numbers("国际货币基金组织预计 2035 年降到 9.2 亿")
    source = extract_key_numbers("从每年 1600 万降到不足 1000 万")
    assert numbers_grounded(claim, source) is False


def test_ungrounded_numbers_lists_only_the_missing() -> None:
    claim = extract_key_numbers("从 1600 万降到 2035")
    source = extract_key_numbers("从 1600 万")
    missing = ungrounded_numbers(claim, source)
    assert [token.raw for token in missing] == ["2035"]


def test_chinese_and_arabic_are_equivalent() -> None:
    claim = extract_key_numbers("一千万")
    source = extract_key_numbers("1000 万")
    assert numbers_grounded(claim, source) is True


def test_chinese_and_arabic_reverse_direction() -> None:
    assert numbers_grounded(extract_key_numbers("1600 万"), extract_key_numbers("一千六百万"))


def test_percent_chinese_matches_arabic() -> None:
    assert numbers_grounded(extract_key_numbers("百分之九十"), extract_key_numbers("90%"))


def test_percent_does_not_match_plain() -> None:
    """90% 与「90 家店」不是一回事。"""
    assert not numbers_grounded(extract_key_numbers("90%"), extract_key_numbers("90 家店"))


def test_fuzzy_number_tolerates_small_drift() -> None:
    assert numbers_grounded(extract_key_numbers("一千六百多万"), extract_key_numbers("1500 万"))


def test_fuzzy_number_rejects_large_drift() -> None:
    assert not numbers_grounded(extract_key_numbers("一千六百多万"), extract_key_numbers("900 万"))


def test_exact_number_rejects_small_drift() -> None:
    """没有模糊量词就必须严格相等。"""
    assert not numbers_grounded(extract_key_numbers("1600 万"), extract_key_numbers("1500 万"))


def test_partial_grounding_is_not_grounded() -> None:
    """all 语义：一个对不上就算不通过。"""
    claim = extract_key_numbers("从 1600 万降到 1000 万")
    source = extract_key_numbers("只有 1600 万")
    assert numbers_grounded(claim, source) is False


def test_empty_claim_numbers_are_trivially_grounded() -> None:
    assert numbers_grounded((), extract_key_numbers("随便 1000 万"))


def test_numbers_without_source_cannot_ground() -> None:
    assert not numbers_grounded(extract_key_numbers("增长 200%"), ())


# --------------------------------------------------------------------------- #
# 描述
# --------------------------------------------------------------------------- #
def test_describe_numbers() -> None:
    assert describe_numbers(extract_key_numbers("从 1600 万降到 1000 万")) == "1600 万, 1000 万"


def test_describe_numbers_empty() -> None:
    assert describe_numbers(()) == ""


# --------------------------------------------------------------------------- #
# token 结构
# --------------------------------------------------------------------------- #
def test_token_is_key_predicate() -> None:
    assert NumberToken(raw="100", value=Decimal(100)).is_key is True
    assert NumberToken(raw="1", value=Decimal(1)).is_key is False
    assert NumberToken(raw="90%", value=Decimal(90), kind="percent").is_key is True
    assert NumberToken(raw="1600万", value=Decimal(16_000_000), scale=10**4).is_key is True
