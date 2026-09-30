"""规则层 R1 / R2.1 / R2.2 / R2.3 / R3（第十节，强制）。"""

from __future__ import annotations

from app.grounding.aliases import StaticAliasResolver
from app.grounding.rules import (
    PATH_SEMANTIC,
    PATH_STRUCTURED,
    RULE_R1,
    RULE_R2_1,
    RULE_R2_2,
    RULE_R2_3,
    apply_grounding,
)
from app.schemas import ContentAnalysis

SOURCE = (
    "中国人口正在下降，所以未来房地产一定会大涨。"
    "过去十年，中国出生人口从每年 1600 万降到不足 1000 万。"
)


def evidence(type_: str = "source", description: str = "原文有此说法") -> dict:
    return {"type": type_, "description": description, "source_url": None, "timestamp": None}


def claim(
    text: str,
    *,
    type_: str = "fact",
    evidence_list: list[dict] | None = None,
    needs_verification: bool = False,
) -> dict:
    return {
        "text": text,
        "type": type_,
        "author_position": None,
        "time_horizon": None,
        "based_on_claims": [],
        "evidence": evidence_list if evidence_list is not None else [],
        "confidence": 0.8,
        "needs_verification": needs_verification,
    }


def analysis(*claims: dict, entities: list[dict] | None = None) -> ContentAnalysis:
    return ContentAnalysis.model_validate(
        {
            "title": "标题",
            "summary": "摘要",
            "analysis_type": "mixed",
            "claims": list(claims),
            "entities": entities or [],
            "overall_confidence": 0.7,
        }
    )


# --------------------------------------------------------------------------- #
# R1：无证据即需验证
# --------------------------------------------------------------------------- #
def test_r1_fires_without_any_evidence() -> None:
    updated, report = apply_grounding(analysis(claim("中国人口正在下降。")), SOURCE)
    assert report.verdicts[0].rules == (RULE_R1,)
    assert updated.claims[0].needs_verification is True


def test_r1_fires_when_all_evidence_is_none() -> None:
    payload = claim("中国人口正在下降。", evidence_list=[evidence("none", "无来源")])
    _, report = apply_grounding(analysis(payload), SOURCE)
    assert report.verdicts[0].rules == (RULE_R1,)


def test_r1_does_not_fire_when_any_evidence_is_real() -> None:
    payload = claim(
        "中国人口正在下降。",
        evidence_list=[evidence("none", "空"), evidence("source", "有来源")],
    )
    updated, report = apply_grounding(analysis(payload), SOURCE)
    assert report.verdicts[0].rules == ()
    assert updated.claims[0].needs_verification is False


# --------------------------------------------------------------------------- #
# R2.1：实体地面化
# --------------------------------------------------------------------------- #
def test_r2_1_fires_for_fabricated_entity() -> None:
    payload = claim("世界银行给出了不同结论。", evidence_list=[evidence("reference")])
    _, report = apply_grounding(
        analysis(payload, entities=[{"name": "世界银行", "type": "organization"}]), SOURCE
    )
    assert report.verdicts[0].rules == (RULE_R2_1,)
    assert report.verdicts[0].ungrounded_entities == ("世界银行",)


def test_r2_1_is_any_semantics() -> None:
    """claim 里有一个真实体 + 一个假实体 → any 语义下**不**判失败（规约原文如此）。"""
    payload = claim("国际货币基金组织与中国看法不同。", evidence_list=[evidence("reference")])
    _, report = apply_grounding(
        analysis(
            payload,
            entities=[
                {"name": "国际货币基金组织", "type": "organization"},
                {"name": "中国", "type": "place"},
            ],
        ),
        SOURCE,
    )
    assert RULE_R2_1 not in report.verdicts[0].rules
    # 但报告里仍然标出哪个实体找不到，供人工复核
    assert report.verdicts[0].ungrounded_entities == ("国际货币基金组织",)


def test_r2_1_uses_alias_resolver() -> None:
    resolver = StaticAliasResolver.from_pairs([("人工智能", ["AI"])])
    payload = claim("AI 正在重塑这个行业。", evidence_list=[evidence("source")])
    _, report = apply_grounding(
        analysis(payload, entities=[{"name": "人工智能", "type": "concept"}]),
        "这段原文提到了 AI 的落地难点。",
        resolver=resolver,
    )
    assert report.verdicts[0].rules == ()


def test_r2_1_skipped_without_entities() -> None:
    payload = claim("中国人口正在下降。", evidence_list=[evidence("source")])
    _, report = apply_grounding(analysis(payload), SOURCE)
    assert RULE_R2_1 not in report.verdicts[0].rules


# --------------------------------------------------------------------------- #
# R2.2：数字地面化
# --------------------------------------------------------------------------- #
def test_r2_2_fires_for_fabricated_number() -> None:
    payload = claim("预计 2035 年人口降到 9.2 亿。", evidence_list=[evidence("reference")])
    _, report = apply_grounding(analysis(payload), SOURCE)
    assert RULE_R2_2 in report.verdicts[0].rules
    assert "2035" in report.verdicts[0].ungrounded_numbers


def test_r2_2_passes_when_all_numbers_grounded() -> None:
    payload = claim("出生人口从每年 1600 万降到不足 1000 万。", evidence_list=[evidence("data")])
    updated, report = apply_grounding(analysis(payload), SOURCE)
    assert report.verdicts[0].rules == ()
    assert updated.claims[0].needs_verification is False


def test_r2_2_is_all_semantics() -> None:
    payload = claim("从 1600 万降到 2035 人。", evidence_list=[evidence("data")])
    _, report = apply_grounding(analysis(payload), SOURCE)
    assert RULE_R2_2 in report.verdicts[0].rules
    assert report.verdicts[0].ungrounded_numbers == ("2035",)


def test_r2_2_ignores_noise_numbers() -> None:
    """「第 1 步」「3.14」这类不是关键数字，不能拿来判失败。"""
    payload = claim("这是第 1 步，圆周率约 3.14。", evidence_list=[evidence("source")])
    _, report = apply_grounding(analysis(payload), SOURCE)
    assert RULE_R2_2 not in report.verdicts[0].rules


def test_r2_2_tolerates_chinese_notation() -> None:
    payload = claim("出生人口降到不足一千万。", evidence_list=[evidence("data")])
    _, report = apply_grounding(analysis(payload), SOURCE)
    assert RULE_R2_2 not in report.verdicts[0].rules


# --------------------------------------------------------------------------- #
# R2.3：语义/证据地面化
# --------------------------------------------------------------------------- #
def test_r2_3_passes_semantic_claim_with_support() -> None:
    """概括性断言**不要求**逐字命中原文。"""
    payload = claim(
        "作者对本轮变化的整体判断是谨慎乐观的。",
        type_="inference",
        evidence_list=[evidence("source", "原文末尾的判断句")],
    )
    updated, report = apply_grounding(analysis(payload), SOURCE)
    assert report.verdicts[0].path == PATH_SEMANTIC
    assert report.verdicts[0].rules == ()
    assert updated.claims[0].needs_verification is False


def test_r2_3_fires_when_support_is_empty() -> None:
    payload = claim(
        "作者对本轮变化的整体判断是谨慎乐观的。",
        type_="inference",
        evidence_list=[evidence("quote", "   ")],
    )
    _, report = apply_grounding(analysis(payload), SOURCE)
    assert report.verdicts[0].rules == (RULE_R2_3,)


def test_r2_3_not_applied_to_structured_claims() -> None:
    """有实体/数字的 claim 走结构化路径，R2.3 不参与。"""
    payload = claim(
        "中国人口正在下降。",
        evidence_list=[evidence("source", "")],
    )
    _, report = apply_grounding(
        analysis(payload, entities=[{"name": "中国", "type": "place"}]), SOURCE
    )
    assert report.verdicts[0].path == PATH_STRUCTURED
    assert report.verdicts[0].rules == ()


def test_r2_3_not_reported_when_r1_already_fired() -> None:
    payload = claim("作者对本轮变化的整体判断是谨慎乐观的。", type_="opinion")
    _, report = apply_grounding(analysis(payload), SOURCE)
    assert report.verdicts[0].rules == (RULE_R1,)


# --------------------------------------------------------------------------- #
# 不信模型自报
# --------------------------------------------------------------------------- #
def test_llm_true_is_overridden_to_false() -> None:
    payload = claim(
        "中国人口正在下降。", needs_verification=True, evidence_list=[evidence("source")]
    )
    updated, _ = apply_grounding(
        analysis(payload, entities=[{"name": "中国", "type": "place"}]), SOURCE
    )
    assert updated.claims[0].needs_verification is False


def test_llm_false_is_overridden_to_true() -> None:
    payload = claim("中国人口正在下降。", needs_verification=False)
    updated, _ = apply_grounding(analysis(payload), SOURCE)
    assert updated.claims[0].needs_verification is True


# --------------------------------------------------------------------------- #
# R3：派生 unverified_claims
# --------------------------------------------------------------------------- #
def test_r3_derives_from_flagged_claims_in_order() -> None:
    payloads = [
        claim("中国人口正在下降。", evidence_list=[evidence("source")]),
        claim("作者认为房地产未来会上涨。", type_="opinion", evidence_list=[evidence("none", "主观判断")]),
        claim("作者预测房地产未来上涨。", type_="prediction", evidence_list=[evidence("none", "预测")]),
    ]
    updated, report = apply_grounding(analysis(*payloads), SOURCE)
    assert updated.unverified_claims == [
        "作者认为房地产未来会上涨。",
        "作者预测房地产未来上涨。",
    ]
    assert report.needs_verification_count == 2
    assert report.verified_count == 1
    assert report.total_claims == 3


def test_r3_empty_when_everything_verified() -> None:
    payloads = [
        claim("中国人口正在下降。", evidence_list=[evidence("source")]),
        claim("出生人口从 1600 万降到 1000 万。", evidence_list=[evidence("data")]),
    ]
    updated, _ = apply_grounding(analysis(*payloads), SOURCE)
    assert updated.unverified_claims == []


def test_rules_fired_counts() -> None:
    payloads = [
        claim("没有任何证据的断言。"),
        claim("expected 2035 年 9.2 亿。", evidence_list=[evidence("reference")]),
    ]
    _, report = apply_grounding(analysis(*payloads), SOURCE)
    assert report.rules_fired == {RULE_R1: 1, RULE_R2_2: 1}


# --------------------------------------------------------------------------- #
# 不变性
# --------------------------------------------------------------------------- #
def test_input_analysis_is_not_mutated() -> None:
    original = analysis(claim("中国人口正在下降。", needs_verification=False))
    apply_grounding(original, SOURCE)
    assert original.claims[0].needs_verification is False
    assert original.unverified_claims == []


def test_grounding_is_idempotent() -> None:
    first, _ = apply_grounding(analysis(claim("作者预测房地产上涨。", type_="prediction")), SOURCE)
    second, _ = apply_grounding(first, SOURCE)
    assert first.unverified_claims == second.unverified_claims
    assert first.model_dump() == second.model_dump()


def test_other_fields_are_preserved() -> None:
    payloads = [claim("中国人口正在下降。", evidence_list=[evidence("source")])]
    original = analysis(*payloads, entities=[{"name": "中国", "type": "place"}])
    updated, _ = apply_grounding(original, SOURCE)
    assert updated.title == original.title
    assert updated.summary == original.summary
    assert updated.analysis_type == original.analysis_type
    assert updated.entities == original.entities
    assert len(updated.claims) == len(original.claims)


def test_report_source_length() -> None:
    _, report = apply_grounding(analysis(claim("无证据断言。")), SOURCE)
    assert report.source_length == len(SOURCE)


def test_verdict_detail_human_readable() -> None:
    payload = claim("世界银行给出了不同结论。", evidence_list=[evidence("reference")])
    _, report = apply_grounding(
        analysis(payload, entities=[{"name": "世界银行", "type": "organization"}]), SOURCE
    )
    detail = report.verdicts[0].detail
    assert "实体" in detail
    assert "世界银行" in detail


def test_verdict_detail_for_verified_claim() -> None:
    payload = claim("中国人口正在下降。", evidence_list=[evidence("source")])
    _, report = apply_grounding(
        analysis(payload, entities=[{"name": "中国", "type": "place"}]), SOURCE
    )
    assert report.verdicts[0].detail == "证据与来源可追溯"


def test_empty_claims_list() -> None:
    updated, report = apply_grounding(analysis(), SOURCE)
    assert updated.claims == []
    assert updated.unverified_claims == []
    assert report.rules_fired == {}
