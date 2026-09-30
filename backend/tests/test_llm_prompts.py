"""Prompt v1 与响应 JSON Schema：与 Pydantic 模型钉死（第八节）。"""

from __future__ import annotations

import json

import pytest

from app.llm.prompts import (
    ANALYSIS_PROMPT_VERSION,
    CLAIM_JSON_SCHEMA,
    ENTITY_JSON_SCHEMA,
    EVIDENCE_JSON_SCHEMA,
    FORBIDDEN_ANYWHERE_KEYS,
    FORBIDDEN_RESPONSE_KEYS,
    RESPONSE_JSON_SCHEMA,
    SYNTHESIS_PROMPT_VERSION,
    build_analysis_prompt,
    build_retry_hint,
    build_synthesis_prompt,
)
from app.schemas import ContentAnalysis
from app.schemas.enums import (
    ANALYSIS_TYPES,
    CLAIM_TYPES,
    ENTITY_TYPES,
    EVIDENCE_TYPES,
)


def walk_property_names(schema: object) -> set[str]:
    """递归收集所有 ``properties`` 的键名。"""
    names: set[str] = set()
    if isinstance(schema, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            names.update(properties)
            for value in properties.values():
                names.update(walk_property_names(value))
        for key in ("items",):
            if key in schema:
                names.update(walk_property_names(schema[key]))
        for key in ("anyOf", "oneOf", "allOf"):
            for branch in schema.get(key, []) or []:
                names.update(walk_property_names(branch))
    elif isinstance(schema, list):
        for item in schema:
            names.update(walk_property_names(item))
    return names


# --------------------------------------------------------------------------- #
# 版本常量
# --------------------------------------------------------------------------- #
def test_prompt_versions() -> None:
    assert ANALYSIS_PROMPT_VERSION == "analysis_prompt_v1"
    assert SYNTHESIS_PROMPT_VERSION == "synthesis_prompt_v1"
    assert ANALYSIS_PROMPT_VERSION != SYNTHESIS_PROMPT_VERSION


# --------------------------------------------------------------------------- #
# Schema 与模型对齐（防漂移）
# --------------------------------------------------------------------------- #
def test_top_level_properties_match_model() -> None:
    expected = set(ContentAnalysis.model_fields) - {"unverified_claims"}
    assert set(RESPONSE_JSON_SCHEMA["properties"]) == expected


def test_top_level_required_matches_expectation() -> None:
    assert set(RESPONSE_JSON_SCHEMA["required"]) == {
        "title",
        "summary",
        "analysis_type",
        "claims",
        "overall_confidence",
    }


def test_top_level_forbids_additional_properties() -> None:
    assert RESPONSE_JSON_SCHEMA["additionalProperties"] is False


@pytest.mark.parametrize("key", FORBIDDEN_RESPONSE_KEYS)
def test_forbidden_keys_absent_from_top_level(key: str) -> None:
    assert key not in RESPONSE_JSON_SCHEMA["properties"]


@pytest.mark.parametrize("key", FORBIDDEN_ANYWHERE_KEYS)
def test_forbidden_keys_absent_from_every_level(key: str) -> None:
    assert key not in walk_property_names(RESPONSE_JSON_SCHEMA)


def test_claim_level_needs_verification_is_allowed() -> None:
    """文档第八节 schema 里 claims[].needs_verification 是合法字段（顶层才禁止）。"""
    assert "needs_verification" in CLAIM_JSON_SCHEMA["properties"]
    assert "needs_verification" not in RESPONSE_JSON_SCHEMA["properties"]


def test_claim_schema_matches_claim_model() -> None:
    from app.schemas import Claim

    assert set(CLAIM_JSON_SCHEMA["properties"]) == set(Claim.model_fields)
    assert set(CLAIM_JSON_SCHEMA["required"]) == {"text", "type", "confidence"}
    assert CLAIM_JSON_SCHEMA["additionalProperties"] is False


def test_evidence_schema_matches_evidence_model() -> None:
    from app.schemas import Evidence

    assert set(EVIDENCE_JSON_SCHEMA["properties"]) == set(Evidence.model_fields)
    assert EVIDENCE_JSON_SCHEMA["additionalProperties"] is False


def test_entity_schema_matches_entity_model() -> None:
    from app.schemas import Entity

    assert set(ENTITY_JSON_SCHEMA["properties"]) == set(Entity.model_fields)


def test_enums_match_literal_values() -> None:
    assert RESPONSE_JSON_SCHEMA["properties"]["analysis_type"]["enum"] == list(ANALYSIS_TYPES)
    assert CLAIM_JSON_SCHEMA["properties"]["type"]["enum"] == list(CLAIM_TYPES)
    assert EVIDENCE_JSON_SCHEMA["properties"]["type"]["enum"] == list(EVIDENCE_TYPES)
    assert ENTITY_JSON_SCHEMA["properties"]["type"]["enum"] == list(ENTITY_TYPES)


def test_confidence_bounds_stated() -> None:
    assert CLAIM_JSON_SCHEMA["properties"]["confidence"]["minimum"] == 0
    assert CLAIM_JSON_SCHEMA["properties"]["confidence"]["maximum"] == 1


# --------------------------------------------------------------------------- #
# 分析 prompt
# --------------------------------------------------------------------------- #
def test_analysis_prompt_version_and_roles() -> None:
    prompt = build_analysis_prompt("一段内容。")
    assert prompt.version == ANALYSIS_PROMPT_VERSION
    assert prompt.system and prompt.user


def test_analysis_prompt_embeds_content_verbatim() -> None:
    text = "中国人口正在下降，所以未来房地产一定会大涨。"
    prompt = build_analysis_prompt(text)
    assert text in prompt.user
    assert "<content>" in prompt.user and "</content>" in prompt.user


def test_analysis_prompt_embeds_schema() -> None:
    prompt = build_analysis_prompt("x")
    assert '"claims"' in prompt.user
    assert '"overall_confidence"' in prompt.user
    assert json.dumps(RESPONSE_JSON_SCHEMA, ensure_ascii=False, indent=2)[:60] in prompt.user


@pytest.mark.parametrize("word", ["facts", "opinions", "inferences", "predictions"])
def test_analysis_prompt_forbids_legacy_arrays(word: str) -> None:
    prompt = build_analysis_prompt("x")
    assert word in prompt.system


def test_analysis_prompt_forbids_top_level_needs_verification() -> None:
    prompt = build_analysis_prompt("x")
    assert "禁止输出顶层 needs_verification" in prompt.system


def test_analysis_prompt_forbids_unverified_claims() -> None:
    prompt = build_analysis_prompt("x")
    assert "禁止输出 unverified_claims" in prompt.system


def test_analysis_prompt_states_no_markdown() -> None:
    prompt = build_analysis_prompt("x")
    assert "Markdown" in prompt.system


def test_analysis_prompt_documents_four_claim_types() -> None:
    prompt = build_analysis_prompt("x")
    for claim_type in CLAIM_TYPES:
        assert claim_type in prompt.system


def test_analysis_prompt_contains_section_nine_counterexample() -> None:
    prompt = build_analysis_prompt("x")
    assert "未来房地产一定大涨" in prompt.system
    assert "prediction" in prompt.system


def test_chunk_context_only_when_multiple_chunks() -> None:
    single = build_analysis_prompt("x", chunk_index=1, chunk_total=1)
    assert "第 1/1 个分块" not in single.user

    multi = build_analysis_prompt("x", chunk_index=2, chunk_total=5)
    assert "第 2/5 个分块" in multi.user


def test_retry_hint_is_appended() -> None:
    prompt = build_analysis_prompt("x", retry_hint="上一次缺少 analysis_type")
    assert "上一次缺少 analysis_type" in prompt.user
    assert prompt.version == ANALYSIS_PROMPT_VERSION


# --------------------------------------------------------------------------- #
# synthesis prompt
# --------------------------------------------------------------------------- #
def test_synthesis_prompt_version(valid_analysis_payload: dict) -> None:
    analysis = ContentAnalysis.model_validate(valid_analysis_payload)
    prompt = build_synthesis_prompt([analysis, analysis])
    assert prompt.version == SYNTHESIS_PROMPT_VERSION


def test_synthesis_prompt_embeds_chunk_analyses(valid_analysis_payload: dict) -> None:
    analysis = ContentAnalysis.model_validate(valid_analysis_payload)
    prompt = build_synthesis_prompt([analysis])
    assert analysis.title in prompt.user
    assert analysis.claims[0].text in prompt.user


def test_synthesis_prompt_mentions_merge_duties() -> None:
    prompt = build_synthesis_prompt([])
    for keyword in ("去重", "不丢失", "不新增", "合并"):
        assert keyword in prompt.system


def test_synthesis_prompt_omits_derived_field(valid_analysis_payload: dict) -> None:
    """分块结果里的 unverified_claims 是程序派生的，不能回灌给模型。"""
    analysis = ContentAnalysis.model_validate(valid_analysis_payload).with_unverified_claims(
        ["不该出现在 prompt 里"]
    )
    prompt = build_synthesis_prompt([analysis])
    body = prompt.user.split("<content>", 1)[1]
    assert "unverified_claims" not in body
    assert "不该出现在 prompt 里" not in body


def test_synthesis_prompt_retry_hint(valid_analysis_payload: dict) -> None:
    analysis = ContentAnalysis.model_validate(valid_analysis_payload)
    prompt = build_synthesis_prompt([analysis], retry_hint="claims 不是数组")
    assert "claims 不是数组" in prompt.user


# --------------------------------------------------------------------------- #
# retry hint
# --------------------------------------------------------------------------- #
def test_build_retry_hint_collapses_whitespace() -> None:
    assert build_retry_hint("缺少\n\n  字段\t名") == "缺少 字段 名"


def test_build_retry_hint_truncates() -> None:
    assert len(build_retry_hint("x" * 500, limit=50)) == 50


def test_build_retry_hint_handles_none() -> None:
    assert build_retry_hint(None) == ""  # type: ignore[arg-type]
