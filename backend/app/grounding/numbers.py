"""关键数字的抽取与等价性判定（R2.2 的基础设施）。

「明确等价的规范化表示」包含：

* 全角数字 / 全角百分号（NFKC 折叠）
* ``1600 万`` 与 ``16000000`` 与 ``一千六百万``  → 同一个值
* ``90%`` 与 ``百分之九十``                    → 同一个值
* ``一千六百多万`` → 模糊值，允许 15% 相对误差

「关键数字」的判定（避免把「第 1 步」「3.14」这类噪声当成证据要求）：

* 带量级（百/千/万/亿）→ 关键
* 出现 ``%`` / ``百分之`` → 关键
* 纯数字：整数部分 ≥ 2 位（即 ≥ 10）→ 关键
* 中文数字：**至少 2 个字**（``十``、``百`` 这类单字极易是「十分」「百姓」的噪声）→ 关键
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from app.grounding.folding import fold_for_matching

PLAIN: Final[str] = "plain"
PERCENT: Final[str] = "percent"

#: 模糊量词：出现即允许相对误差。
FUZZY_MARKERS: Final[str] = "多余左右"

_CN_DIGITS: Final[dict[str, int]] = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "壹": 1,
    "二": 2,
    "两": 2,
    "贰": 2,
    "三": 3,
    "叁": 3,
    "四": 4,
    "肆": 4,
    "五": 5,
    "伍": 5,
    "六": 6,
    "陆": 6,
    "七": 7,
    "柒": 7,
    "八": 8,
    "捌": 8,
    "九": 9,
    "玖": 9,
}
_CN_SMALL_UNITS: Final[dict[str, int]] = {
    "十": 10,
    "拾": 10,
    "百": 100,
    "佰": 100,
    "千": 1000,
    "仟": 1000,
}
_CN_BIG_UNITS: Final[dict[str, int]] = {"万": 10**4, "亿": 10**8}

_CN_CHAR_CLASS: Final[str] = "".join(
    sorted(set(_CN_DIGITS) | set(_CN_SMALL_UNITS) | set(_CN_BIG_UNITS))
)

_ARABIC_SCALE: Final[dict[str, int]] = {"百": 100, "千": 1000, "万": 10**4, "亿": 10**8}

_PERCENT_CN_RE = re.compile(
    r"百分之\s*(?P<arabic>\d+(?:\.\d+)?|(?P<chinese>[" + _CN_CHAR_CLASS + r"]{1,12}))"
)
_PERCENT_ARABIC_RE = re.compile(r"(?P<arabic>\d+(?:\.\d+)?)\s*%")

_NUMBER_RE = re.compile(
    r"(?P<arabic>\d+(?:\.\d+)?)(?:\s*(?P<arabic_scale>[百千万亿]))?"
    r"|(?P<chinese>[" + _CN_CHAR_CLASS + r"]{1,12})"
    r"(?P<chinese_fuzzy>[" + FUZZY_MARKERS + r"]{1,2})?"
    r"(?P<chinese_tail>[万亿])?"
)


@dataclass(frozen=True)
class NumberToken:
    raw: str
    value: Decimal
    kind: str = PLAIN
    scale: int = 1
    fuzzy: bool = False

    @property
    def is_key(self) -> bool:
        if self.kind == PERCENT:
            return True
        if self.scale > 1:
            return True
        return abs(self.value) >= 10


def parse_chinese_number(text: str) -> int | None:
    """把中文数字转成整数。``一千六百万`` → 16000000。无法解析返回 ``None``。"""
    if not text:
        return None
    total = 0
    section = 0
    number = 0
    seen = False

    for char in text:
        if char in _CN_DIGITS:
            number = _CN_DIGITS[char]
            seen = True
        elif char in _CN_SMALL_UNITS:
            unit = _CN_SMALL_UNITS[char]
            if number == 0:
                number = 1  # 「十五」的十
            section += number * unit
            number = 0
            seen = True
        elif char in _CN_BIG_UNITS:
            unit = _CN_BIG_UNITS[char]
            section += number
            total += (section or 1) * unit
            section = 0
            number = 0
            seen = True
        else:
            return None

    if not seen:
        return None
    return total + section + number


def scale_of(text: str) -> int:
    """文本里最大的**量级单位**（只认 万 / 亿）。

    ``百`` / ``千`` 是数字内部单位，其量级已经体现在 ``value`` 里；
    只有 ``万`` / ``亿`` 代表「另一个数量级」。
    """
    if "亿" in text:
        return 10**8
    if "万" in text:
        return 10**4
    return 1


def _mask_percentages(text: str) -> tuple[str, list[NumberToken]]:
    """把百分比写法抠出来换成等长空格，避免与普通数字重复计数。"""
    tokens: list[NumberToken] = []
    working = text

    def replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        arabic = match.group("arabic")
        chinese = match.groupdict().get("chinese")
        if chinese:
            parsed = parse_chinese_number(chinese)
            if parsed is None:
                return raw
            value = Decimal(parsed)
        else:
            value = Decimal(arabic)
        tokens.append(NumberToken(raw=raw, value=value, kind=PERCENT, scale=1))
        return " " * len(raw)

    working = _PERCENT_CN_RE.sub(replace, working)
    working = _PERCENT_ARABIC_RE.sub(replace, working)
    return working, tokens


def extract_numbers(text: str) -> tuple[NumberToken, ...]:
    """抽出全部数字 token（不做「关键」过滤）。"""
    folded = fold_for_matching(text)
    if not folded:
        return ()

    masked, tokens = _mask_percentages(folded)

    for match in _NUMBER_RE.finditer(masked):
        arabic = match.group("arabic")
        if arabic:
            scale_char = match.group("arabic_scale")
            scale = _ARABIC_SCALE.get(scale_char, 1) if scale_char else 1
            tokens.append(
                NumberToken(
                    raw=match.group(0).strip(),
                    value=Decimal(arabic) * scale,
                    kind=PLAIN,
                    scale=scale,
                )
            )
            continue

        chinese = match.group("chinese") or ""
        if len(chinese) < 2:
            # 单字中文数字噪声太多（十分 / 百姓 / 一 */
            continue
        base = parse_chinese_number(chinese)
        if base is None:
            continue
        tail = match.group("chinese_tail") or ""
        tail_scale = _CN_BIG_UNITS.get(tail, 1)
        fuzzy = bool(match.group("chinese_fuzzy"))
        tokens.append(
            NumberToken(
                raw=match.group(0).strip(),
                value=Decimal(base * tail_scale),
                kind=PLAIN,
                scale=scale_of(chinese) * tail_scale,
                fuzzy=fuzzy,
            )
        )

    return tuple(tokens)


def extract_key_numbers(text: str) -> tuple[NumberToken, ...]:
    """只保留「关键数字」（见模块 docstring 的判定规则）。"""
    return tuple(token for token in extract_numbers(text) if token.is_key)


def _matches(claim: NumberToken, source: NumberToken) -> bool:
    if claim.kind != source.kind:
        return False
    if claim.value == source.value:
        return True
    if not (claim.fuzzy or source.fuzzy):
        return False
    if claim.value == 0:
        return source.value == 0
    relative = abs(claim.value - source.value) / abs(claim.value)
    return relative <= Decimal("0.15")


def ungrounded_numbers(
    claim_numbers: tuple[NumberToken, ...],
    source_numbers: tuple[NumberToken, ...],
) -> tuple[NumberToken, ...]:
    """返回 claim 里**在源文本中找不到对应值**的那些数字。"""
    return tuple(
        claim
        for claim in claim_numbers
        if not any(_matches(claim, source) for source in source_numbers)
    )


def numbers_grounded(
    claim_numbers: tuple[NumberToken, ...],
    source_numbers: tuple[NumberToken, ...],
) -> bool:
    """claim 里的每个关键数字都必须能在源文本里找到对应值。"""
    return not ungrounded_numbers(claim_numbers, source_numbers)


def describe_numbers(tokens: tuple[NumberToken, ...]) -> str:
    return ", ".join(token.raw for token in tokens if token.raw)


__all__ = [
    "NumberToken",
    "PLAIN",
    "PERCENT",
    "FUZZY_MARKERS",
    "parse_chinese_number",
    "scale_of",
    "extract_numbers",
    "extract_key_numbers",
    "ungrounded_numbers",
    "numbers_grounded",
    "describe_numbers",
]
