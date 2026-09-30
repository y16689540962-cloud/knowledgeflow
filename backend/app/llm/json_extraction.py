"""JSON 抽取与 repair（第十二节，强制）。

只允许**修格式**：

* 去掉 ``` 围栏
* 去掉前后自然语言（抽取第一个平衡的 JSON 对象 / 数组）
* 去掉尾随逗号
* 把 ``True`` / ``False`` / ``None`` 这类 Python 字面量改成 JSON 字面量

**禁止**：改变语义、补字段、补内容。

设计要点：整个流程不依赖「猜」，每一步都是可验证的确定性变换；
修复动作全部记入 ``repairs``，便于日志与测试回溯。

错误消息里**只允许**出现结构与位置信息（行列号、字符数），绝不能回显原文片段
—— 原文属于第二十四节禁止入日志的内容。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from app.errors import LLMInvalidOutputError
from app.text_scan import find_balanced_json  # 共享的「找平衡 JSON 块」扫描器

#: 记录在 ``repairs`` 里的动作名。
REPAIR_STRIPPED_FENCE = "stripped_code_fence"
REPAIR_EXTRACTED_OBJECT = "extracted_json_object"
REPAIR_QUOTED_BARE_KEYS = "quoted_bare_keys"
REPAIR_INSERTED_COMMAS = "inserted_missing_commas"
REPAIR_TRAILING_COMMAS = "removed_trailing_commas"
REPAIR_PYTHON_LITERALS = "normalized_python_literals"
REPAIR_STRIPPED_BOM = "stripped_bom"

#: 错误消息里允许保留的最大长度。
ERROR_HINT_LIMIT = 120

_FENCE_RE = re.compile(
    r"^\s*```[A-Za-z0-9_+-]*\s*\n(?P<body>.*?)\n?\s*```\s*$",
    re.DOTALL,
)
_TRAILING_COMMA_RE = re.compile(r",\s*(?P<closer>[}\]])")
_PYTHON_LITERAL_RE = re.compile(r"(?<![\w\"'])(True|False|None)(?![\w\"'])")
#: 裸键：``{title: "x"}`` / ``,但: 1`` —— 只在 ``{`` 或 ``,`` 之后出现时才算键。
_BARE_KEY_RE = re.compile(
    r"(?P<prefix>[{,]\s*)(?P<key>[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\-\u4e00-\u9fff]*)(?P<suffix>\s*:)"
)
#: 缺逗号：值后面直接跟下一个 ``"key":``（只在字符串之外生效）。
#: 刻意**不**处理裸数组元素（``[1 2]``）—— 那有歧义，宁可让它失败。
_MISSING_COMMA_RE = re.compile(r"(?<=[}\]\"\d])\s*(?=\"[^\"\n]{0,120}\"\s*:)")


class JSONExtractionError(LLMInvalidOutputError):
    default_message = "无法从 LLM 输出中抽出 JSON"


@dataclass(frozen=True)
class ExtractionResult:
    payload: Any
    repairs: tuple[str, ...]
    raw_length: int


def strip_bom(text: str) -> tuple[str, bool]:
    if text.startswith("\ufeff"):
        return text[1:], True
    return text, False


def strip_code_fence(text: str) -> tuple[str, bool]:
    """去掉最外层 ``` 围栏（带或不带语言标记）。"""
    match = _FENCE_RE.match(text)
    if not match:
        return text, False
    return match.group("body").strip(), True


def remove_trailing_commas(text: str) -> tuple[str, bool]:
    """删掉 ``[1, 2,]`` / ``{"a": 1,}`` 里的尾随逗号（不影响字符串内部）。"""
    previous = None
    current = text
    while previous != current:
        previous = current
        current = _replace_outside_strings(
            _TRAILING_COMMA_RE, current, lambda match: match.group("closer")
        )
    return current, current != text


def normalize_python_literals(text: str) -> tuple[str, bool]:
    """``True`` → ``true``、``False`` → ``false``、``None`` → ``null``。"""
    mapping = {"True": "true", "False": "false", "None": "null"}
    result = _replace_outside_strings(
        _PYTHON_LITERAL_RE, text, lambda match: mapping[match.group(1)]
    )
    return result, result != text


def quote_bare_keys(text: str) -> tuple[str, bool]:
    """给裸键补引号：``{title: "x"}`` → ``{"title": "x"}``。

    只改分隔符，不动任何值；只在字符串之外生效。
    """
    result = _replace_outside_strings(
        _BARE_KEY_RE,
        text,
        lambda match: f'{match.group("prefix")}"{match.group("key")}"{match.group("suffix")}',
    )
    return result, result != text


def insert_missing_commas(text: str) -> tuple[str, bool]:
    """补上「值后面直接跟下一个键」时漏掉的逗号。

    只在字符串之外、且后面确实是 ``"key":`` 时插入 —— 尽力收敛到一种读法。
    """
    result = _replace_outside_strings(_MISSING_COMMA_RE, text, lambda match: ",")
    return result, result != text


def _replace_outside_strings(
    pattern: re.Pattern[str], text: str, replacement: Callable[[re.Match[str]], str]
) -> str:
    """只对字符串字面量**之外**的部分做替换。"""
    out: list[str] = []
    in_string = False
    escaped = False
    index = 0
    length = len(text)

    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue

        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue

        # 在字符串之外尝试匹配
        match = pattern.match(text, index)
        if match:
            out.append(replacement(match))
            index = match.end()
            continue

        out.append(char)
        index += 1

    return "".join(out)


def _json_error(reason: str, *, raw_length: int) -> JSONExtractionError:
    return JSONExtractionError(
        f"{reason}（输出长度 {raw_length} 字符）",
        context={"raw_length": raw_length},
    )


def _describe(exc: json.JSONDecodeError) -> str:
    """只输出行列号，绝不回显 doc 片段。"""
    return f"{exc.msg} @ line {exc.lineno} column {exc.colno}"


_MISSING: Any = object()


def _try_loads(candidate: str) -> Any:
    """解析成功返回对象（可能是 ``None``），失败返回哨兵。"""
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return _MISSING


def extract_json(raw: str) -> ExtractionResult:
    """从模型原始输出里抽出 JSON 对象。

    逐步尝试：原样 → 去 BOM → 去围栏 → 抽取平衡块 → 去尾随逗号 / 修字面量。
    任一步成功即返回，并把已执行的动作记入 ``repairs``。
    """
    if raw is None:
        raise _json_error("模型输出为空", raw_length=0)

    text = raw.strip()
    raw_length = len(raw)
    if not text:
        raise _json_error("模型输出为空", raw_length=raw_length)

    repairs: list[str] = []

    # 1) 原样就是合法 JSON
    parsed = _try_loads(text)
    if parsed is not _MISSING:
        return ExtractionResult(payload=parsed, repairs=(), raw_length=raw_length)

    # 2) 去 BOM
    text, stripped_bom = strip_bom(text)
    if stripped_bom:
        repairs.append(REPAIR_STRIPPED_BOM)
        parsed = _try_loads(text)
        if parsed is not _MISSING:
            return ExtractionResult(payload=parsed, repairs=tuple(repairs), raw_length=raw_length)

    # 3) 去 ``` 围栏
    text, stripped_fence = strip_code_fence(text)
    if stripped_fence:
        repairs.append(REPAIR_STRIPPED_FENCE)
        parsed = _try_loads(text)
        if parsed is not _MISSING:
            return ExtractionResult(payload=parsed, repairs=tuple(repairs), raw_length=raw_length)

    # 4) 抽取第一个平衡的 JSON 块（去掉前后自然语言寒暄）
    balanced = find_balanced_json(text)
    if balanced is not None and balanced != text:
        candidate = balanced
        repairs.append(REPAIR_EXTRACTED_OBJECT)
    else:
        candidate = text

    parsed = _try_loads(candidate)
    if parsed is not _MISSING:
        return ExtractionResult(payload=parsed, repairs=tuple(repairs), raw_length=raw_length)

    # 5) 修格式：裸键补引号 → 尾随逗号 → 补缺失逗号 → Python 字面量
    #    这是「一次 repair pass」：只做格式变换，每做完一步就试着解析。
    for repair_name, step in (
        (REPAIR_QUOTED_BARE_KEYS, quote_bare_keys),
        (REPAIR_TRAILING_COMMAS, remove_trailing_commas),
        (REPAIR_INSERTED_COMMAS, insert_missing_commas),
        (REPAIR_PYTHON_LITERALS, normalize_python_literals),
    ):
        candidate, changed = step(candidate)
        if not changed:
            continue
        repairs.append(repair_name)
        parsed = _try_loads(candidate)
        if parsed is not _MISSING:
            return ExtractionResult(payload=parsed, repairs=tuple(repairs), raw_length=raw_length)

    # 6) 全部手段用尽
    try:
        json.loads(candidate)
    except json.JSONDecodeError as exc:
        detail = _describe(exc)
    else:  # pragma: no cover - 理论上不可达
        detail = "未知解析错误"
    raise _json_error(f"JSON 修复后仍无法解析：{detail[:ERROR_HINT_LIMIT]}", raw_length=raw_length)


__all__ = [
    "JSONExtractionError",
    "ExtractionResult",
    "extract_json",
    "strip_bom",
    "strip_code_fence",
    "find_balanced_json",
    "remove_trailing_commas",
    "normalize_python_literals",
    "quote_bare_keys",
    "insert_missing_commas",
    "REPAIR_STRIPPED_FENCE",
    "REPAIR_EXTRACTED_OBJECT",
    "REPAIR_QUOTED_BARE_KEYS",
    "REPAIR_INSERTED_COMMAS",
    "REPAIR_TRAILING_COMMAS",
    "REPAIR_PYTHON_LITERALS",
    "REPAIR_STRIPPED_BOM",
    "ERROR_HINT_LIMIT",
]
