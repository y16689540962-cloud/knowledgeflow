"""JSON 抽取与 repair：只修格式，绝不改语义（第十二节）。"""

from __future__ import annotations

import json

import pytest

from app.llm.json_extraction import (
    REPAIR_EXTRACTED_OBJECT,
    REPAIR_INSERTED_COMMAS,
    REPAIR_PYTHON_LITERALS,
    REPAIR_QUOTED_BARE_KEYS,
    REPAIR_STRIPPED_BOM,
    REPAIR_STRIPPED_FENCE,
    REPAIR_TRAILING_COMMAS,
    JSONExtractionError,
    extract_json,
    find_balanced_json,
    insert_missing_commas,
    normalize_python_literals,
    quote_bare_keys,
    remove_trailing_commas,
    strip_code_fence,
)

PAYLOAD = {"title": "人口下降之后，房子还会涨吗", "claims": [{"text": "中国人口正在下降。", "type": "fact"}]}
CLEAN = json.dumps(PAYLOAD, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 干净输入
# --------------------------------------------------------------------------- #
def test_clean_json_needs_no_repair() -> None:
    result = extract_json(CLEAN)
    assert result.payload == PAYLOAD
    assert result.repairs == ()
    assert result.raw_length == len(CLEAN)


def test_whitespace_padded_json_is_clean() -> None:
    result = extract_json(f"\n\n   {CLEAN}  \n")
    assert result.payload == PAYLOAD
    assert result.repairs == ()


def test_raw_length_reports_original_length() -> None:
    raw = "\n" + CLEAN + "\n"
    assert extract_json(raw).raw_length == len(raw)


# --------------------------------------------------------------------------- #
# 单步修复
# --------------------------------------------------------------------------- #
def test_bom_is_stripped() -> None:
    result = extract_json("\ufeff" + CLEAN)
    assert result.payload == PAYLOAD
    assert result.repairs == (REPAIR_STRIPPED_BOM,)


def test_fenced_with_language_tag() -> None:
    result = extract_json(f"```json\n{CLEAN}\n```")
    assert result.payload == PAYLOAD
    assert result.repairs == (REPAIR_STRIPPED_FENCE,)


def test_fenced_without_language_tag() -> None:
    result = extract_json(f"```\n{CLEAN}\n```")
    assert result.payload == PAYLOAD
    assert REPAIR_STRIPPED_FENCE in result.repairs


def test_trailing_comma_in_object() -> None:
    result = extract_json('{"a": 1,}')
    assert result.payload == {"a": 1}
    assert result.repairs == (REPAIR_TRAILING_COMMAS,)


def test_trailing_comma_inside_array() -> None:
    result = extract_json('{"a": [1, 2, ],}')
    assert result.payload == {"a": [1, 2]}
    assert REPAIR_TRAILING_COMMAS in result.repairs


def test_python_literals_are_normalized() -> None:
    result = extract_json('{"a": True, "b": False, "c": None}')
    assert result.payload == {"a": True, "b": False, "c": None}
    assert result.repairs == (REPAIR_PYTHON_LITERALS,)


def test_bare_keys_are_quoted() -> None:
    """补引号：``{title: "x"}`` 是格式问题，不是语义问题。"""
    result = extract_json('{title: "人口下降", claims: []}')
    assert result.payload == {"title": "人口下降", "claims": []}
    assert result.repairs == (REPAIR_QUOTED_BARE_KEYS,)


def test_bare_keys_after_comma_are_quoted() -> None:
    result = extract_json('{"a": 1, bcd: 2}')
    assert result.payload == {"a": 1, "bcd": 2}


def test_quoted_keys_are_not_touched() -> None:
    result = extract_json('{"a": 1}')
    assert result.repairs == ()


def test_missing_comma_between_members_is_inserted() -> None:
    """补逗号：``{"a": 1 "b": 2}``。"""
    result = extract_json('{"a": 1 "b": 2}')
    assert result.payload == {"a": 1, "b": 2}
    assert REPAIR_INSERTED_COMMAS in result.repairs


def test_missing_comma_after_nested_object() -> None:
    result = extract_json('{"a": {"x": 1} "b": 2}')
    assert result.payload == {"a": {"x": 1}, "b": 2}


def test_missing_comma_after_string_value() -> None:
    result = extract_json('{"a": "x" "b": 2}')
    assert result.payload == {"a": "x", "b": 2}


def test_missing_comma_inside_strings_is_not_inserted() -> None:
    """字符串里的 ``"`` 后面跟 ``"key":`` 的形态不能被误插逗号。"""
    result = extract_json('{"a": "他说 \\"b\\": 好", "c": 2}')
    assert result.payload["c"] == 2
    assert result.payload["a"] == '他说 "b": 好'


def test_bare_array_elements_are_not_guessed() -> None:
    """``[1 2]`` 有歧义 —— 宁可失败，也不猜。"""
    with pytest.raises(JSONExtractionError):
        extract_json("[1 2]")


def test_combined_repairs_are_all_recorded() -> None:
    raw = '```json\n{title: "t", claims: [], "b": 1 "c": 2,}\n```'
    result = extract_json(raw)
    assert result.payload == {"title": "t", "claims": [], "b": 1, "c": 2}
    assert set(result.repairs) == {
        REPAIR_STRIPPED_FENCE,
        REPAIR_QUOTED_BARE_KEYS,
        REPAIR_TRAILING_COMMAS,
        REPAIR_INSERTED_COMMAS,
    }


def test_python_literals_inside_strings_untouched() -> None:
    result = extract_json('{"a": "True None False"}')
    assert result.payload == {"a": "True None False"}
    assert result.repairs == ()


def test_fence_then_trailing_comma() -> None:
    result = extract_json('```json\n{"a": 1,}\n```')
    assert result.payload == {"a": 1}
    assert result.repairs == (REPAIR_STRIPPED_FENCE, REPAIR_TRAILING_COMMAS)


# --------------------------------------------------------------------------- #
# 去除前后自然语言
# --------------------------------------------------------------------------- #
def test_preamble_and_postamble() -> None:
    raw = f"好的，我已经仔细阅读了。结果如下：\n\n{CLEAN}\n\n以上就是全部内容。"
    result = extract_json(raw)
    assert result.payload == PAYLOAD
    assert result.repairs == (REPAIR_EXTRACTED_OBJECT,)


def test_preamble_with_quotes_does_not_break_scanning() -> None:
    raw = f'他问："你确定吗？" 我回答：确实。\n{CLEAN}\n结束'
    assert extract_json(raw).payload == PAYLOAD


def test_preamble_inside_fence() -> None:
    raw = f"```json\n这是结果：\n{CLEAN}\n```"
    result = extract_json(raw)
    assert result.payload == PAYLOAD
    assert REPAIR_STRIPPED_FENCE in result.repairs
    assert REPAIR_EXTRACTED_OBJECT in result.repairs


# --------------------------------------------------------------------------- #
# 括号平衡
# --------------------------------------------------------------------------- #
def test_braces_inside_string_are_ignored() -> None:
    raw = '{"a": "这里有个 } 花括号", "b": {"c": 1}}'
    assert extract_json(raw).payload == {"a": "这里有个 } 花括号", "b": {"c": 1}}


def test_escaped_quote_inside_string_is_ignored() -> None:
    raw = '{"a": "他说 \\"结束了\\"", "b": 2}'
    assert extract_json(raw).payload["b"] == 2


def test_nested_objects() -> None:
    raw = '{"a": {"b": {"c": [1, {"d": 2}]}}}'
    assert extract_json(raw).payload["a"]["b"]["c"][1]["d"] == 2


def test_top_level_array_is_accepted() -> None:
    assert extract_json('[{"a": 1}]').payload == [{"a": 1}]


def test_find_balanced_json_returns_none_without_structure() -> None:
    assert find_balanced_json("完全不是 JSON") is None


def test_find_balanced_json_stops_at_first_balanced_block() -> None:
    assert find_balanced_json('前言 {"a": 1} 中间 {"b": 2} 结尾') == '{"a": 1}'


# --------------------------------------------------------------------------- #
# 失败情形
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw", ["", "   ", "\n\n"])
def test_empty_output_raises(raw: str) -> None:
    with pytest.raises(JSONExtractionError):
        extract_json(raw)


def test_plain_text_raises() -> None:
    with pytest.raises(JSONExtractionError):
        extract_json("我认为这个内容很有意思，但不打算给出结构化结果。")


def test_truncated_json_raises() -> None:
    with pytest.raises(JSONExtractionError):
        extract_json('{"a": 1, "b":')


def test_unbalanced_json_raises() -> None:
    with pytest.raises(JSONExtractionError):
        extract_json('{"a": 1}'[:-1])


def test_error_type_is_llm_invalid_output() -> None:
    with pytest.raises(JSONExtractionError) as excinfo:
        extract_json("不是 JSON")
    assert excinfo.value.error_type_value == "LLM_INVALID_OUTPUT"


def test_error_message_never_leaks_content() -> None:
    """错误消息只能带结构信息 —— 不能回显模型原文。"""
    secret = "绝密字符串XYZ987"
    with pytest.raises(JSONExtractionError) as excinfo:
        extract_json(f"{{'a': '{secret}',")
    assert secret not in excinfo.value.message
    assert secret not in str(excinfo.value.to_dict())


def test_error_context_has_length_only() -> None:
    raw = "随便一段文字" * 10
    with pytest.raises(JSONExtractionError) as excinfo:
        extract_json(raw)
    assert excinfo.value.context == {"raw_length": len(raw)}


# --------------------------------------------------------------------------- #
# 修复不得改变语义
# --------------------------------------------------------------------------- #
def test_repair_preserves_all_keys() -> None:
    raw = f"```json\n{json.dumps({'a': 1, 'b': [1, 2], 'c': {'d': 'e'}}, ensure_ascii=False)},\n```"
    # 上面刻意造了一个非法 JSON：对象后跟逗号
    result = extract_json(raw)
    assert set(result.payload) == {"a", "b", "c"}


def test_repair_preserves_values_exactly() -> None:
    body = {"标题": "带，逗号与}括号", "n": [1, 2, 3], "nested": {"x": None}}
    raw = "开场白\n" + json.dumps(body, ensure_ascii=False) + "\n结束语"
    result = extract_json(raw)
    assert result.payload == body


def test_repair_never_adds_fields() -> None:
    raw = f"寒暄\n{json.dumps({'title': 't'}, ensure_ascii=False)}\n寒暄"
    assert extract_json(raw).payload == {"title": "t"}


def test_repair_never_invents_claims() -> None:
    body = {"title": "t", "claims": [{"text": "只有这一条"}]}
    raw = "```json\n" + json.dumps(body, ensure_ascii=False) + "\n```"
    assert extract_json(raw).payload["claims"] == [{"text": "只有这一条"}]


# --------------------------------------------------------------------------- #
# 低层工具
# --------------------------------------------------------------------------- #
def test_strip_code_fence_without_fence() -> None:
    assert strip_code_fence('{"a": 1}') == ('{"a": 1}', False)


def test_remove_trailing_commas_noop_when_clean() -> None:
    assert remove_trailing_commas('{"a": 1}') == ('{"a": 1}', False)


def test_normalize_python_literals_outside_strings_only() -> None:
    text = '{"a": True, "b": "None"}'
    assert normalize_python_literals(text) == ('{"a": true, "b": "None"}', True)


def test_quote_bare_keys_returns_changed_flag() -> None:
    assert quote_bare_keys('{a: 1}') == ('{"a": 1}', True)
    assert quote_bare_keys('{"a": 1}') == ('{"a": 1}', False)


def test_insert_missing_commas_returns_changed_flag() -> None:
    assert insert_missing_commas('{"a": 1 "b": 2}') == ('{"a": 1,"b": 2}', True)
    assert insert_missing_commas('{"a": 1}') == ('{"a": 1}', False)


def test_quote_bare_keys_preserves_values() -> None:
    text = '{标题: "含，逗号与}括号", n: 2}'
    result, changed = quote_bare_keys(text)
    assert changed is True
    assert json.loads(result) == {"标题": "含，逗号与}括号", "n": 2}


# --------------------------------------------------------------------------- #
# 真实 fixture（Phase 2 验收口径）
# --------------------------------------------------------------------------- #
def test_fenced_fixture_is_repairable(fenced_analysis_text: str) -> None:
    result = extract_json(fenced_analysis_text)
    assert result.repairs == (REPAIR_STRIPPED_FENCE,)
    assert "claims" in result.payload


def test_preamble_fixture_is_repairable(preamble_analysis_text: str) -> None:
    result = extract_json(preamble_analysis_text)
    assert result.repairs == (REPAIR_EXTRACTED_OBJECT,)
    assert "claims" in result.payload


def test_invalid_fixture_parses_cleanly(invalid_analysis_text: str) -> None:
    """invalid fixture 是**合法 JSON** —— 它要被 Pydantic 拒，而不是被抽取层拒。"""
    result = extract_json(invalid_analysis_text)
    assert result.repairs == ()
    assert "claims" in result.payload


def test_valid_fixture_parses_cleanly(valid_analysis_text: str) -> None:
    result = extract_json(valid_analysis_text)
    assert result.repairs == ()
    assert isinstance(result.payload, dict)
