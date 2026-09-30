"""Phase 2 验收：脏输出修复、三次失败状态机、Grounding Check（第三十一节）。

第三十一节的 Phase 2 验收口径，逐条对应：

* ``fenced`` / ``preamble`` → JSON extraction / repair
* ``invalid`` → 三次失败状态机 → ``LLM_INVALID_OUTPUT``
* ``grounding_fail`` → Grounding Check 拦截

**全部离线**：只用 Mock / Scripted Provider，不碰网络、不碰真实 LLM。
"""

from __future__ import annotations

import json

import pytest

from app.chunking.budget import TextBudget, apply_budget, chunk_chars_for
from app.chunking.chunker import chunk_text
from app.grounding.rules import RULE_R1, RULE_R2_1, RULE_R2_2
from app.llm.prompts import ANALYSIS_PROMPT_VERSION, SYNTHESIS_PROMPT_VERSION
from app.llm.service import LLMAnalysisService
from app.providers import MockProvider
from app.schemas import ContentAnalysis
from app.testing import ScriptedProvider, load_fixture_json, load_fixture_text
from tests.conftest import make_service


def valid_text() -> str:
    return load_fixture_text("llm_analysis_valid.json")


def invalid_text() -> str:
    return load_fixture_text("llm_analysis_invalid.json")


# --------------------------------------------------------------------------- #
# A 线依然完全离线
# --------------------------------------------------------------------------- #
def test_phase2_uses_no_network_modules() -> None:
    """Core Pipeline 的分析段不得引入任何网络客户端（httpx 只在 Adapter 里）。"""
    from app.llm import service
    from app.grounding import rules

    for module in (service, rules):
        source = open(module.__file__, encoding="utf-8").read()
        for banned in ("httpx", "requests", "aiohttp", "urllib.request", "socket"):
            assert banned not in source, f"{module.__name__} 引用了 {banned}"


# --------------------------------------------------------------------------- #
# fenced / preamble：抽取 + 修复
# --------------------------------------------------------------------------- #
async def test_fenced_fixture_end_to_end(mixed_raw_content) -> None:
    provider = MockProvider(raw_text=load_fixture_text("llm_analysis_fenced.json"))
    outcome = await make_service(provider).analyze_raw(mixed_raw_content)

    assert outcome.success is True
    assert outcome.analysis is not None
    assert outcome.analysis.analysis_type == "mixed"
    assert len(outcome.analysis.claims) == 6
    assert outcome.attempts[0].repairs == ("stripped_code_fence",)
    assert outcome.prompt_version == ANALYSIS_PROMPT_VERSION


async def test_preamble_fixture_end_to_end(mixed_raw_content) -> None:
    provider = MockProvider(raw_text=load_fixture_text("llm_analysis_preamble.json"))
    outcome = await make_service(provider).analyze_raw(mixed_raw_content)

    assert outcome.success is True
    assert outcome.analysis is not None
    assert outcome.attempts[0].repairs == ("extracted_json_object",)


async def test_repaired_output_equals_clean_output(mixed_raw_content) -> None:
    """修复只动格式：fenced 版与干净版的最终分析结果必须一致。"""
    clean = await make_service(
        MockProvider(raw_text=load_fixture_text("llm_analysis_valid.json"))
    ).analyze_raw(mixed_raw_content)
    fenced = await make_service(
        MockProvider(raw_text=load_fixture_text("llm_analysis_fenced.json"))
    ).analyze_raw(mixed_raw_content)
    preambled = await make_service(
        MockProvider(raw_text=load_fixture_text("llm_analysis_preamble.json"))
    ).analyze_raw(mixed_raw_content)

    assert clean.analysis is not None and fenced.analysis is not None
    assert clean.analysis.model_dump() == fenced.analysis.model_dump()
    assert clean.analysis.model_dump() == preambled.analysis.model_dump()  # type: ignore[union-attr]


# --------------------------------------------------------------------------- #
# invalid：三次失败状态机
# --------------------------------------------------------------------------- #
async def test_invalid_fixture_fails_after_three_attempts(mixed_raw_content) -> None:
    provider = ScriptedProvider([invalid_text()] * 3)
    outcome = await make_service(provider).analyze_raw(mixed_raw_content)

    assert outcome.success is False
    assert outcome.analysis is None
    assert outcome.error_type == "LLM_INVALID_OUTPUT"
    assert provider.call_count == 3
    assert [record.attempt for record in outcome.attempts] == [1, 2, 3]
    assert all(record.failed_step == "validation" for record in outcome.attempts)
    assert all(record.elapsed_ms >= 0 for record in outcome.attempts)


async def test_invalid_output_never_reaches_analysis(mixed_raw_content) -> None:
    provider = ScriptedProvider([invalid_text()] * 3)
    outcome = await make_service(provider).analyze_raw(mixed_raw_content)
    assert outcome.analysis is None
    assert outcome.grounding is None


async def test_invalid_then_repair_recovers(mixed_raw_content) -> None:
    """第 1 次脏、第 2 次干净 —— 状态机必须能恢复。"""
    provider = ScriptedProvider([invalid_text(), valid_text()])
    outcome = await make_service(provider).analyze_raw(mixed_raw_content)

    assert outcome.success is True
    assert len(outcome.attempts) == 2
    assert outcome.attempts[0].failed_step == "validation"
    assert outcome.attempts[1].succeeded is True


# --------------------------------------------------------------------------- #
# grounding_fail：Grounding Check 拦截
# --------------------------------------------------------------------------- #
async def test_grounding_fail_fixture_passes_schema_then_gets_flagged(
    mixed_raw_content, grounding_fail_payload: dict
) -> None:
    """它必须**先通过** Pydantic（否则测的就不是 Grounding Check 了）。"""
    ContentAnalysis.model_validate(grounding_fail_payload)

    provider = MockProvider(payload=grounding_fail_payload)
    outcome = await make_service(provider).analyze_raw(mixed_raw_content)

    assert outcome.success is True
    assert outcome.grounding is not None
    report = outcome.grounding

    # claim 0：数字 2035 / 9.2 亿 不存在于原文
    assert RULE_R2_2 in report.verdicts[0].rules
    assert "2035" in report.verdicts[0].ungrounded_numbers
    # claim 3：实体「世界银行」在原文中完全找不到
    assert RULE_R2_1 in report.verdicts[3].rules
    assert report.verdicts[3].ungrounded_entities == ("世界银行",)
    # claim 2：预测句没有非 none 证据
    assert RULE_R1 in report.verdicts[2].rules

    assert report.needs_verification_count == 3
    assert report.rules_fired == {RULE_R1: 1, RULE_R2_1: 1, RULE_R2_2: 2}


async def test_grounding_fail_unverified_claims_are_derived(
    mixed_raw_content, grounding_fail_payload: dict
) -> None:
    provider = MockProvider(payload=grounding_fail_payload)
    outcome = await make_service(provider).analyze_raw(mixed_raw_content)
    assert outcome.analysis is not None

    expected = [
        claim.text for claim in outcome.analysis.claims if claim.needs_verification
    ]
    assert outcome.analysis.unverified_claims == expected
    assert "国际货币基金组织在 2035 年预测中国人口将降至 9.2 亿。" in expected
    assert "世界银行在 2040 年发布的报告得出了完全相反的结论。" in expected


async def test_grounded_claims_stay_verified(mixed_raw_content, grounding_fail_payload: dict) -> None:
    provider = MockProvider(payload=grounding_fail_payload)
    outcome = await make_service(provider).analyze_raw(mixed_raw_content)
    assert outcome.analysis is not None
    # claim 1「中国人口正在下降。」有来源引用，实体也真实存在
    assert outcome.analysis.claims[1].needs_verification is False


async def test_grounding_never_triggers_retry(mixed_raw_content, grounding_fail_payload: dict) -> None:
    """Grounding 是规则层，不是输出格式问题 —— 不该消耗重试次数。"""
    provider = MockProvider(payload=grounding_fail_payload)
    outcome = await make_service(provider).analyze_raw(mixed_raw_content)
    assert len(outcome.attempts) == 1
    assert outcome.success is True


# --------------------------------------------------------------------------- #
# 事实 / 观点 / 推论 / 预测 分离
# --------------------------------------------------------------------------- #
async def test_four_way_separation_survives_the_pipeline(mixed_raw_content) -> None:
    provider = MockProvider(raw_text=load_fixture_text("llm_analysis_valid.json"))
    outcome = await make_service(provider).analyze_raw(mixed_raw_content)
    assert outcome.analysis is not None

    analysis = outcome.analysis
    assert [c.text for c in analysis.claims_of_type("fact")] == [
        "中国人口正在下降。",
        "过去十年，中国出生人口从每年 1600 万降到不足 1000 万。",
        "作者本人目前持有两套一线城市的房产。",
    ]
    assert [c.text for c in analysis.claims_of_type("prediction")] == ["作者预测房地产未来上涨。"]
    # 第九节禁令：预测句绝不能落进 facts
    assert all("上涨" not in c.text for c in analysis.claims_of_type("fact"))


async def test_schema_rejects_legacy_arrays_before_grounding() -> None:
    """模型给了旧数组 → 直接进重试，绝不让它落库。"""
    payload = load_fixture_json("llm_analysis_valid.json")
    payload["facts"] = payload["claims"]
    provider = ScriptedProvider([json.dumps(payload, ensure_ascii=False)] * 3)

    outcome = await make_service(provider).analyze_text("中国人口正在下降。所以未来房地产一定会大涨。")
    assert outcome.success is False
    assert outcome.error_type == "LLM_INVALID_OUTPUT"
    assert provider.call_count == 3


async def test_llm_provided_unverified_claims_is_ignored() -> None:
    payload = load_fixture_json("llm_analysis_valid.json")
    payload["unverified_claims"] = ["模型自己编的", "不该被采纳"]
    provider = MockProvider(payload=payload)

    outcome = await make_service(provider).analyze_text(
        "中国人口正在下降，所以未来房地产一定会大涨。过去十年，中国出生人口从每年 1600 万降到不足 1000 万。人口少了，需求就少；但核心城市的房子依然稀缺，所以我认为一线城市的房价未来一定会涨。作者本人目前持有两套一线城市的房产。"
    )
    assert outcome.analysis is not None
    assert "模型自己编的" not in outcome.analysis.unverified_claims


# --------------------------------------------------------------------------- #
# 超长输入：预算 → 分块 → synthesis
# --------------------------------------------------------------------------- #
async def test_long_input_full_path_with_default_settings(long_raw_content, default_budget) -> None:
    prepared = apply_budget(long_raw_content, default_budget)
    chunk_chars = chunk_chars_for(default_budget.max_total_input_chars, 4000)
    expected_chunks = len(chunk_text(prepared.text, max_chars=chunk_chars))

    provider = ScriptedProvider([valid_text()] * (expected_chunks + 1))
    service = LLMAnalysisService(provider, budget=default_budget, max_attempts=3)
    outcome = await service.analyze_raw(long_raw_content)

    assert expected_chunks > 1
    assert outcome.success is True
    assert outcome.chunk_count == expected_chunks
    assert outcome.prompt_version == SYNTHESIS_PROMPT_VERSION
    assert outcome.prepared_text is not None
    assert outcome.prepared_text.truncated is True
    assert provider.call_count == expected_chunks + 1


async def test_synthesis_output_is_validated_not_stored_raw(mixed_raw_content) -> None:
    """synthesis 的裸输出绝不直接落库：给脏的 synthesis 输出，必须走修复或失败。"""
    text = (mixed_raw_content.raw_text or "") * 30
    budget = TextBudget(max_transcript_chars=12000, max_ocr_chars=6000, max_total_input_chars=2000)
    expected_chunks = len(chunk_text(text, max_chars=chunk_chars_for(2000, 4000)))
    assert expected_chunks > 1

    # 前面 N 个分块正常，synthesis 返回脏输出 → 应该被 repair 后通过
    provider = ScriptedProvider(
        [valid_text()] * expected_chunks + [load_fixture_text("llm_analysis_fenced.json")]
    )
    outcome = await make_service(provider, budget=budget).analyze_text(text)

    assert outcome.success is True
    assert outcome.attempts[-1].repairs == ("stripped_code_fence",)
    assert outcome.attempts[-1].phase == "synthesis"


async def test_synthesis_failure_marks_run_failed(mixed_raw_content) -> None:
    text = (mixed_raw_content.raw_text or "") * 30
    budget = TextBudget(max_transcript_chars=12000, max_ocr_chars=6000, max_total_input_chars=2000)
    expected_chunks = len(chunk_text(text, max_chars=chunk_chars_for(2000, 4000)))

    provider = ScriptedProvider(
        [valid_text()] * expected_chunks + [invalid_text()] * 3
    )
    outcome = await make_service(provider, budget=budget).analyze_text(text)

    assert outcome.success is False
    assert outcome.error_type == "LLM_INVALID_OUTPUT"
    assert len(outcome.chunk_analyses) == expected_chunks
    assert outcome.attempts[-1].phase == "synthesis"


# --------------------------------------------------------------------------- #
# 全程不落库、不写文件
# --------------------------------------------------------------------------- #
async def test_phase2_does_not_touch_database_or_vault(mixed_raw_content) -> None:
    """Phase 2 只产出 AnalysisOutcome；落库是 Phase 4 的事。"""
    provider = MockProvider(raw_text=valid_text())
    outcome = await make_service(provider).analyze_raw(mixed_raw_content)
    fields = set(outcome.__dataclass_fields__)
    assert "content_id" not in fields
    assert "analysis_id" not in fields
    assert "vault_path" not in fields


@pytest.mark.parametrize("fixture", ["llm_analysis_valid.json", "llm_analysis_fenced.json", "llm_analysis_preamble.json"])
async def test_all_parseable_fixtures_reach_grounding(mixed_raw_content, fixture: str) -> None:
    provider = MockProvider(raw_text=load_fixture_text(fixture))
    outcome = await make_service(provider).analyze_raw(mixed_raw_content)
    assert outcome.success is True
    assert outcome.grounding is not None
    assert outcome.grounding.total_claims == 6
