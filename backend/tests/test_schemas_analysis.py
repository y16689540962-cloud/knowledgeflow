"""AI 结构化 Schema：claims 单一真源（第八节 / 第九节）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas import (
    ANALYSIS_TYPES,
    CLAIM_TYPES,
    EVIDENCE_TYPES,
    Claim,
    ContentAnalysis,
    Entity,
    Evidence,
    LEGACY_CLAIM_ARRAYS,
)


def minimal_claim(**overrides: object) -> dict:
    payload = {
        "text": "中国人口正在下降。",
        "type": "fact",
        "confidence": 0.8,
    }
    payload.update(overrides)
    return payload


def minimal_analysis(**overrides: object) -> dict:
    payload = {
        "title": "标题",
        "summary": "摘要",
        "analysis_type": "mixed",
        "claims": [minimal_claim()],
        "overall_confidence": 0.7,
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- #
# Evidence / Claim
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("evidence_type", EVIDENCE_TYPES)
def test_all_evidence_types_accepted(evidence_type: str) -> None:
    evidence = Evidence(type=evidence_type, description="d")  # type: ignore[arg-type]
    assert evidence.type == evidence_type
    assert evidence.source_url is None


def test_evidence_rejects_unknown_type() -> None:
    with pytest.raises(ValidationError):
        Evidence(type="rumour", description="d")  # type: ignore[arg-type]


def test_evidence_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        Evidence(type="source", description="d", url="http://x")  # type: ignore[call-arg]


@pytest.mark.parametrize("claim_type", CLAIM_TYPES)
def test_all_claim_types_accepted(claim_type: str) -> None:
    assert Claim(**minimal_claim(type=claim_type)).type == claim_type


def test_claim_defaults() -> None:
    claim = Claim(**minimal_claim())
    assert claim.author_position is None
    assert claim.time_horizon is None
    assert claim.based_on_claims == []
    assert claim.evidence == []
    assert claim.needs_verification is False


def test_claim_requires_confidence() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Claim(text="x", type="fact")  # type: ignore[call-arg]
    assert any(err["loc"][0] == "confidence" for err in excinfo.value.errors())


def test_claim_requires_text_and_type() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Claim(confidence=0.5)  # type: ignore[call-arg]
    missing = {err["loc"][0] for err in excinfo.value.errors() if err["type"] == "missing"}
    assert missing == {"text", "type"}


def test_claim_text_cannot_be_blank() -> None:
    with pytest.raises(ValidationError):
        Claim(**minimal_claim(text="   "))


@pytest.mark.parametrize("confidence", [-0.1, 1.1, 2])
def test_claim_confidence_range(confidence: float) -> None:
    with pytest.raises(ValidationError):
        Claim(**minimal_claim(confidence=confidence))


def test_claim_confidence_must_be_number() -> None:
    with pytest.raises(ValidationError):
        Claim(**minimal_claim(confidence="high"))


def test_claim_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        Claim(**minimal_claim(source="微博"))  # type: ignore[call-arg]


def test_claim_has_evidence_helper() -> None:
    assert Claim(**minimal_claim()).has_evidence() is False
    none_only = Claim(**minimal_claim(evidence=[{"type": "none", "description": "无"}]))
    assert none_only.has_evidence() is False
    sourced = Claim(**minimal_claim(evidence=[{"type": "source", "description": "有"}]))
    assert sourced.has_evidence() is True


# --------------------------------------------------------------------------- #
# Entity
# --------------------------------------------------------------------------- #
def test_entity_valid() -> None:
    entity = Entity(name="人工智能", type="concept")
    assert entity.name == "人工智能"


def test_entity_type_rejected_when_unknown() -> None:
    with pytest.raises(ValidationError):
        Entity(name="x", type="animal")  # type: ignore[arg-type]


def test_entity_name_blank_rejected() -> None:
    with pytest.raises(ValidationError):
        Entity(name="  ", type="concept")


# --------------------------------------------------------------------------- #
# ContentAnalysis —— 单一真源强制条款
# --------------------------------------------------------------------------- #
def test_no_legacy_claim_arrays_in_model() -> None:
    """facts / opinions / inferences / predictions 四个数组必须已删除。"""
    for legacy in LEGACY_CLAIM_ARRAYS:
        assert legacy not in ContentAnalysis.model_fields
        assert not hasattr(ContentAnalysis(**minimal_analysis()), legacy)


def test_legacy_arrays_are_rejected_loudly() -> None:
    """模型给出旧数组 → 直接拒绝（说明 prompt 没被遵守，不能静默丢数据）。"""
    with pytest.raises(ValidationError) as excinfo:
        ContentAnalysis.model_validate(minimal_analysis(facts=[{"text": "x"}], opinions=[]))
    assert "claims[]" in str(excinfo.value)


@pytest.mark.parametrize("legacy", LEGACY_CLAIM_ARRAYS)
def test_each_legacy_array_rejected(legacy: str) -> None:
    with pytest.raises(ValidationError):
        ContentAnalysis.model_validate(minimal_analysis(**{legacy: []}))


def test_top_level_needs_verification_rejected_as_bool() -> None:
    with pytest.raises(ValidationError):
        ContentAnalysis.model_validate(minimal_analysis(needs_verification=True))


def test_top_level_needs_verification_rejected_as_array() -> None:
    """历史上它曾是数组，造成同名不同型 —— 现在一律拒绝。"""
    with pytest.raises(ValidationError):
        ContentAnalysis.model_validate(minimal_analysis(needs_verification=["x"]))


def test_llm_provided_unverified_claims_is_ignored() -> None:
    analysis = ContentAnalysis.model_validate(
        minimal_analysis(unverified_claims=["LLM 自己写的", "不应该被采纳"])
    )
    assert analysis.unverified_claims == []


def test_with_unverified_claims_overwrites() -> None:
    analysis = ContentAnalysis(**minimal_analysis()).with_unverified_claims(["由规则层派生"])
    assert analysis.unverified_claims == ["由规则层派生"]


def test_extra_unknown_top_level_key_ignored() -> None:
    analysis = ContentAnalysis.model_validate(minimal_analysis(llm_chatter="无关字段"))
    assert analysis.title == "标题"


def test_remaining_required_fields() -> None:
    with pytest.raises(ValidationError) as excinfo:
        ContentAnalysis.model_validate({"title": "t"})
    missing = {err["loc"][0] for err in excinfo.value.errors() if err["type"] == "missing"}
    assert missing == {"summary", "analysis_type", "claims", "overall_confidence"}


@pytest.mark.parametrize("analysis_type", ANALYSIS_TYPES)
def test_all_analysis_types_accepted(analysis_type: str) -> None:
    analysis = ContentAnalysis.model_validate(minimal_analysis(analysis_type=analysis_type))
    assert analysis.analysis_type == analysis_type


def test_overall_confidence_range() -> None:
    with pytest.raises(ValidationError):
        ContentAnalysis.model_validate(minimal_analysis(overall_confidence=1.5))


def test_optional_lists_default_empty() -> None:
    analysis = ContentAnalysis(**minimal_analysis())
    assert analysis.questions == []
    assert analysis.topics == []
    assert analysis.entities == []
    assert analysis.keywords == []


# --------------------------------------------------------------------------- #
# 事实 / 观点 / 推论 / 预测 分离（第九节）
# --------------------------------------------------------------------------- #
def test_claims_of_type_groups_by_type(valid_analysis_payload: dict) -> None:
    analysis = ContentAnalysis.model_validate(valid_analysis_payload)
    assert {claim.type for claim in analysis.claims} == {"fact", "opinion", "inference", "prediction"}

    facts = [c.text for c in analysis.claims_of_type("fact")]
    opinions = [c.text for c in analysis.claims_of_type("opinion")]
    inferences = [c.text for c in analysis.claims_of_type("inference")]
    predictions = [c.text for c in analysis.claims_of_type("prediction")]

    assert "中国人口正在下降。" in facts
    assert "作者认为房地产未来会上涨。" in opinions
    assert "作者将人口变化与房地产价格联系起来。" in inferences
    assert "作者预测房地产未来上涨。" in predictions


def test_future_price_claim_is_never_filed_as_fact(valid_analysis_payload: dict) -> None:
    """第九节禁止事项：不得把「未来房地产一定上涨」写入 facts。"""
    analysis = ContentAnalysis.model_validate(valid_analysis_payload)
    fact_texts = [claim.text for claim in analysis.claims_of_type("fact")]
    assert all("上涨" not in text for text in fact_texts)


def test_valid_fixture_passes_validation(valid_analysis_payload: dict) -> None:
    analysis = ContentAnalysis.from_llm_payload(valid_analysis_payload)
    assert analysis.title
    assert len(analysis.claims) == 6
    assert analysis.overall_confidence == pytest.approx(0.72)


def test_grounding_fail_fixture_passes_pydantic(valid_analysis_payload: dict) -> None:
    """Phase 1 只验证它能通过 Pydantic —— 拦截它的是 Phase 2 的 Grounding Check。"""
    from app.testing.fixture_loader import load_fixture_json

    payload = load_fixture_json("llm_analysis_grounding_fail.json")
    analysis = ContentAnalysis.from_llm_payload(payload)
    assert any(entity.name == "国际货币基金组织" for entity in analysis.entities)


def test_invalid_fixture_is_rejected() -> None:
    """缺字段 + 类型错误，必须被拒绝。"""
    from app.testing.fixture_loader import load_fixture_json

    payload = load_fixture_json("llm_analysis_invalid.json")
    with pytest.raises(ValidationError):
        ContentAnalysis.model_validate(payload)
