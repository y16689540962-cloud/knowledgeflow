"""文本扫描小工具：从混杂文本里找出第一个**平衡**的 JSON 块。

LLM 输出（``llm/json_extraction``）与抖音页面内嵌 JSON（``ingestion/douyin``）
都需要这件事，所以抽成独立模块 —— 否则 ``ingestion`` 得反向依赖 ``llm``。
"""

from __future__ import annotations


def find_balanced_json(text: str) -> str | None:
    """扫描出第一个**平衡**的 JSON 对象或数组。

    正确跳过字符串内部的括号与转义字符 —— 不会在 ``"a}b"`` 处提前收尾。

    字符串状态**只在找到起始括号之后**才开始跟踪：否则前文寒暄里的
    引号（尤其是奇数个）会把状态机带偏，导致漏掉真正的 JSON 块。
    """
    start: int | None = None
    opener = ""
    closer = ""
    depth = 0
    in_string = False
    escaped = False

    for index, char in enumerate(text):
        if start is None:
            if char in "{[":
                start = index
                opener = char
                closer = "}" if char == "{" else "]"
                depth = 1
            continue

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue

        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]

    return None


__all__ = ["find_balanced_json"]
