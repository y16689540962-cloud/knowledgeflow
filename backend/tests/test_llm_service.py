"""LLM 分析服务：重试状态机、分块、synthesis、Grounding（第十一 / 十二节）。"""

from __future__ import annotations

import io

import pytest

from app.chunking.budget import TextBudget, apply_budget, chunk_chars_for
from app.chunking.chunker import chunk_text
from app.config import Settings
from app.errors import ErrorType, LLMError
from app.llm.prompts import ANALYSIS_PROMPT_VERSION, SYNTHESIS_PROMPT_VERSION
from app.llm.service import (
    DEFAULT_MAX_ATTEMPTS,
    STEP_EXTRACTION,
    STEP_TRANSPORT,
    STEP_VALIDATION,
    LLMAnalysisService,
)
from app.logging_config import setup_logging
from app.providers import MockProvider
from app.testing import ScriptedProvider
from tests.conftest import make_service

TIMEOUT = LLMError("模拟超时", error_type=ErrorType.LLM_TIMEOUT)


def valid_text() -> str:
    from app.testing import load_fixture_text

    return load_fixture_text("llm_analysis_valid.json")


def invalid_text() -> str:
    from app.testing import load_fixture_text

    return load_fixture_text("llm_analysis_invalid.json")


def fenced_text() -> str:
    from app.testing import load_fixture_text

    return load_fixture_text("llm_analysis_fenced.json")


# --------------------------------------------------------------------------- #
# 单块成功路径
# --------------------------------------------------------------------------- #
async def test_single_chunk_success(mixed_raw_content, valid_analysis_payload) -> None:
    provider = MockProvider(payload=valid_analysis_payload)
    outcome = await make_service(provider).analyze_raw(mixed_raw_content)

    assert outcome.success is True
    assert outcome.chunk_count == 1
    assert outcome.prompt_version == ANALYSIS_PROMPT_VERSION
    assert outcome.provider == "mock"
    assert outcome.model == "mock-llm-v1"
    assert outcome.error_type is None
    assert outcome.error_message is None
    assert len(outcome.attempts) == 1
    assert outcome.attempts[0].succeeded is True
    assert outcome.attempts[0].phase == "chunk-1"
    assert outcome.attempts[0].elapsed_ms >= 0
    assert outcome.prepared_text is not None
    assert outcome.chunks[0].text == mixed_raw_content.raw_text


async def test_system_prompt_is_sent(mixed_raw_content, valid_analysis_payload) -> None:
    provider = MockProvider(payload=valid_analysis_payload)
    await make_service(provider).analyze_raw(mixed_raw_content)
    assert provider.systems[0]
    assert "claims" in provider.systems[0]


async def test_grounding_is_applied_to_result(mixed_raw_content, valid_analysis_payload) -> None:
    """事实三条通过；观点 / 推论 / 预测三条被规则层判为需验证。"""
    provider = MockProvider(payload=valid_analysis_payload)
    outcome = await make_service(provider).analyze_raw(mixed_raw_content)

    assert outcome.grounding is not None
    assert outcome.grounding.total_claims == 6
    assert outcome.grounding.needs_verification_count == 3
    assert set(outcome.grounding.rules_fired) == {"R1"}

    analysis = outcome.analysis
    assert analysis is not None
    assert [c.needs_verification for c in analysis.claims[:3]] == [False, False, False]
    assert [c.needs_verification for c in analysis.claims[3:]] == [True, True, True]
    assert len(analysis.unverified_claims) == 3


# --------------------------------------------------------------------------- #
# 脏输出：抽取 / 修复
# --------------------------------------------------------------------------- #
async def test_fenced_output_is_repaired(mixed_raw_content) -> None:
    provider = MockProvider(raw_text=fenced_text())
    outcome = await make_service(provider).analyze_raw(mixed_raw_content)

    assert outcome.success is True
    assert outcome.attempts[0].repairs == ("stripped_code_fence",)
    assert outcome.repairs == ("stripped_code_fence",)


async def test_preamble_output_is_repaired(mixed_raw_content, preamble_analysis_text: str) -> None:
    provider = MockProvider(raw_text=preamble_analysis_text)
    outcome = await make_service(provider).analyze_raw(mixed_raw_content)

    assert outcome.success is True
    assert outcome.attempts[0].repairs == ("extracted_json_object",)


async def test_clean_output_records_no_repairs(mixed_raw_content, valid_analysis_text: str) -> None:
    provider = MockProvider(raw_text=valid_analysis_text)
    outcome = await make_service(provider).analyze_raw(mixed_raw_content)
    assert outcome.attempts[0].repairs == ()


async def test_unparseable_then_success(mixed_raw_content) -> None:
    provider = ScriptedProvider(["我认为这个内容很有意思。", valid_text()])
    outcome = await make_service(provider).analyze_raw(mixed_raw_content)

    assert outcome.success is True
    assert len(outcome.attempts) == 2
    assert outcome.attempts[0].failed_step == STEP_EXTRACTION
    assert outcome.attempts[0].error_type == "LLM_INVALID_OUTPUT"
    assert outcome.attempts[1].succeeded is True


async def test_invalid_then_success(mixed_raw_content) -> None:
    provider = ScriptedProvider([invalid_text(), valid_text()])
    outcome = await make_service(provider).analyze_raw(mixed_raw_content)

    assert outcome.success is True
    assert outcome.attempts[0].failed_step == STEP_VALIDATION
    assert outcome.attempts[0].error_type == "LLM_INVALID_OUTPUT"


async def test_retry_hint_is_carried_to_next_attempt(mixed_raw_content) -> None:
    provider = ScriptedProvider([invalid_text(), valid_text()])
    await make_service(provider).analyze_raw(mixed_raw_content)

    assert "【重试提示】" not in provider.calls[0]
    assert "【重试提示】" in provider.calls[1]


async def test_retry_hint_mentions_the_actual_problem(mixed_raw_content) -> None:
    provider = ScriptedProvider([invalid_text(), valid_text()])
    await make_service(provider).analyze_raw(mixed_raw_content)
    assert "analysis_type" in provider.calls[1]


async def test_validation_summary_never_includes_input_values(mixed_raw_content) -> None:
    """Pydantic 错误的 input 可能含模型原文 —— 摘要里绝不能带。"""
    provider = ScriptedProvider([invalid_text(), valid_text()])
    outcome = await make_service(provider).analyze_raw(mixed_raw_content)
    message = outcome.attempts[0].error_message or ""
    assert "very high" not in message
    assert "high" in message or "analysis_type" in message


# --------------------------------------------------------------------------- #
# 三次失败状态机
# --------------------------------------------------------------------------- #
async def test_three_failures_end_with_llm_invalid_output(mixed_raw_content) -> None:
    provider = ScriptedProvider([invalid_text()] * DEFAULT_MAX_ATTEMPTS)
    outcome = await make_service(provider).analyze_raw(mixed_raw_content)

    assert outcome.success is False
    assert outcome.analysis is None
    assert outcome.error_type == "LLM_INVALID_OUTPUT"
    assert outcome.error_message
    assert len(outcome.attempts) == 3
    assert all(record.failed_step == STEP_VALIDATION for record in outcome.attempts)
    assert [record.attempt for record in outcome.attempts] == [1, 2, 3]
    assert provider.call_count == 3


async def test_never_retries_forever(mixed_raw_content) -> None:
    """脚本只放 3 条：如果真的重试第 4 次，ScriptedProvider 会抛 AssertionError。"""
    provider = ScriptedProvider([invalid_text()] * 3)
    outcome = await make_service(provider).analyze_raw(mixed_raw_content)
    assert provider.remaining == 0
    assert outcome.success is False


async def test_max_attempts_is_respected(mixed_raw_content) -> None:
    provider = ScriptedProvider([invalid_text()] * 5)
    outcome = await make_service(provider, max_attempts=2).analyze_raw(mixed_raw_content)
    assert provider.call_count == 2
    assert len(outcome.attempts) == 2


async def test_max_attempts_zero_is_clamped(mixed_raw_content) -> None:
    provider = ScriptedProvider([invalid_text()])
    outcome = await make_service(provider, max_attempts=0).analyze_raw(mixed_raw_content)
    assert provider.call_count == 1
    assert outcome.success is False


async def test_default_max_attempts_is_three() -> None:
    assert DEFAULT_MAX_ATTEMPTS == 3


# --------------------------------------------------------------------------- #
# 传输层失败
# --------------------------------------------------------------------------- #
async def test_transport_failure_keeps_error_type(mixed_raw_content) -> None:
    provider = ScriptedProvider([TIMEOUT] * 3)
    outcome = await make_service(provider).analyze_raw(mixed_raw_content)

    assert outcome.success is False
    assert outcome.error_type == "LLM_TIMEOUT"
    assert all(record.failed_step == STEP_TRANSPORT for record in outcome.attempts)


async def test_transport_failure_then_success(mixed_raw_content) -> None:
    provider = ScriptedProvider([TIMEOUT, valid_text()])
    outcome = await make_service(provider).analyze_raw(mixed_raw_content)
    assert outcome.success is True
    assert outcome.attempts[0].failed_step == STEP_TRANSPORT


async def test_transport_failure_hint_is_carried(mixed_raw_content) -> None:
    provider = ScriptedProvider([TIMEOUT, valid_text()])
    await make_service(provider).analyze_raw(mixed_raw_content)
    assert "【重试提示】" in provider.calls[1]


# --------------------------------------------------------------------------- #
# 空输入
# --------------------------------------------------------------------------- #
async def test_empty_text_fails_without_calling_provider() -> None:
    provider = ScriptedProvider([])
    outcome = await make_service(provider).analyze_text("   ")

    assert outcome.success is False
    assert outcome.error_type == "EMPTY_SOURCE_TEXT"
    assert outcome.chunk_count == 0
    assert provider.call_count == 0
    assert outcome.analysis is None


# --------------------------------------------------------------------------- #
# 分块 + synthesis
# --------------------------------------------------------------------------- #
def small_budget() -> TextBudget:
    return TextBudget(max_transcript_chars=12000, max_ocr_chars=6000, max_total_input_chars=2000)


def chunk_chars_for_small_budget() -> int:
    return chunk_chars_for(small_budget().max_total_input_chars, 4000)


def build_chunked_case(text: str):
    """按真实分块数量准备脚本：N 个分块 + 1 次 synthesis。"""
    expected = len(chunk_text(text, max_chars=chunk_chars_for_small_budget()))
    assert expected > 1, "测试文本不够长，没触发分块"
    provider = ScriptedProvider([valid_text()] * (expected + 1))
    return expected, provider


@pytest.fixture
def chunked_text(mixed_raw_content) -> str:
    """把同一段含实体与数字的文案重复多次 —— 保证分块后 Grounding 仍能对上。"""
    return (mixed_raw_content.raw_text or "") * 30


async def test_chunked_run_uses_synthesis(chunked_text: str) -> None:
    expected_chunks, provider = build_chunked_case(chunked_text)
    outcome = await make_service(provider, budget=small_budget()).analyze_text(chunked_text)

    assert outcome.success is True
    assert outcome.chunk_count == expected_chunks
    assert outcome.prompt_version == SYNTHESIS_PROMPT_VERSION
    assert len(outcome.chunk_analyses) == expected_chunks
    assert provider.call_count == expected_chunks + 1


async def test_each_chunk_gets_its_own_phase_label(chunked_text: str) -> None:
    expected_chunks, provider = build_chunked_case(chunked_text)
    outcome = await make_service(provider, budget=small_budget()).analyze_text(chunked_text)

    phases = [record.phase for record in outcome.attempts]
    assert phases[:expected_chunks] == [f"chunk-{i + 1}" for i in range(expected_chunks)]
    assert phases[-1] == "synthesis"


async def test_chunk_prompts_include_position_hint(chunked_text: str) -> None:
    expected_chunks, provider = build_chunked_case(chunked_text)
    await make_service(provider, budget=small_budget()).analyze_text(chunked_text)

    assert f"第 1/{expected_chunks} 个分块" in provider.calls[0]
    assert f"第 {expected_chunks}/{expected_chunks} 个分块" in provider.calls[expected_chunks - 1]


async def test_synthesis_prompt_embeds_chunk_results(chunked_text: str) -> None:
    _, provider = build_chunked_case(chunked_text)
    await make_service(provider, budget=small_budget()).analyze_text(chunked_text)

    synthesis_prompt = provider.calls[-1]
    assert "各分块分析结果" in synthesis_prompt
    assert "人口下降之后，房子还会涨吗" in synthesis_prompt
    assert "unverified_claims" not in synthesis_prompt


async def test_synthesis_uses_synthesis_system_prompt(chunked_text: str) -> None:
    _, provider = build_chunked_case(chunked_text)
    await make_service(provider, budget=small_budget()).analyze_text(chunked_text)
    assert "同一份长文档" in (provider.systems[-1] or "")


async def test_grounding_only_applied_after_synthesis(chunked_text: str) -> None:
    """分块中间结果不跑规则层 —— 只有最终结果被改写。"""
    _, provider = build_chunked_case(chunked_text)
    outcome = await make_service(provider, budget=small_budget()).analyze_text(chunked_text)

    assert all(
        not any(claim.needs_verification for claim in chunk.claims)
        for chunk in outcome.chunk_analyses
    )
    assert all(chunk.unverified_claims == [] for chunk in outcome.chunk_analyses)
    assert outcome.analysis is not None
    assert len(outcome.analysis.unverified_claims) == 3
    assert outcome.grounding is not None
    assert set(outcome.grounding.rules_fired) == {"R1"}


async def test_chunk_failure_aborts_run(chunked_text: str) -> None:
    expected_chunks, _ = build_chunked_case(chunked_text)
    assert expected_chunks > 1

    provider = ScriptedProvider([valid_text()] + [invalid_text()] * 3)
    outcome = await make_service(provider, budget=small_budget()).analyze_text(chunked_text)

    assert outcome.success is False
    assert outcome.error_type == "LLM_INVALID_OUTPUT"
    assert len(outcome.chunk_analyses) == 1
    assert provider.call_count == 4
    assert outcome.attempts[-1].phase == "chunk-2"


async def test_single_chunk_does_not_call_synthesis(
    mixed_raw_content, valid_analysis_text: str
) -> None:
    provider = ScriptedProvider([valid_analysis_text])
    outcome = await make_service(provider).analyze_raw(mixed_raw_content)

    assert outcome.chunk_count == 1
    assert outcome.prompt_version == ANALYSIS_PROMPT_VERSION
    assert provider.call_count == 1
    assert outcome.analysis is not None
    assert outcome.chunk_analyses[0].title == outcome.analysis.title


# --------------------------------------------------------------------------- #
# 预算与 prepared_text
# --------------------------------------------------------------------------- #
async def test_long_input_is_truncated_and_chunked(long_raw_content, default_budget) -> None:
    prepared = apply_budget(long_raw_content, default_budget)
    chunk_chars = chunk_chars_for(default_budget.max_total_input_chars, 4000)
    expected = len(chunk_text(prepared.text, max_chars=chunk_chars))

    provider = ScriptedProvider([valid_text()] * (expected + 1))
    service = LLMAnalysisService(provider, budget=default_budget, max_attempts=3)
    outcome = await service.analyze_raw(long_raw_content)

    assert outcome.prepared_text is not None
    assert outcome.prepared_text.transcript_truncated is True
    assert outcome.prepared_text.total_truncated is True
    assert len(outcome.prepared_text.text) == default_budget.max_total_input_chars
    assert outcome.chunk_count == expected
    assert expected > 1
    assert outcome.prompt_version == SYNTHESIS_PROMPT_VERSION


# --------------------------------------------------------------------------- #
# 日志与脱敏
# --------------------------------------------------------------------------- #
async def test_source_text_never_logged(mixed_raw_content, valid_analysis_text: str) -> None:
    stream = io.StringIO()
    logger = setup_logging(stream=stream, level="DEBUG", logger_name="knowledgeflow.llm")
    provider = ScriptedProvider([valid_analysis_text])
    await make_service(provider, logger=logger).analyze_raw(mixed_raw_content)

    logged = stream.getvalue()
    assert "人口正在下降" not in logged
    assert "老王聊经济" not in logged


async def test_failure_logs_do_not_leak_source() -> None:
    stream = io.StringIO()
    logger = setup_logging(stream=stream, level="DEBUG", logger_name="knowledgeflow.llm")
    marker = "绝密标记ABC"
    provider = ScriptedProvider([invalid_text()] * 3)
    await make_service(provider, logger=logger).analyze_text(f"{marker}。这句话有内容。")

    assert marker not in stream.getvalue()


async def test_chunk_progress_is_logged(mixed_raw_content, valid_analysis_text: str) -> None:
    stream = io.StringIO()
    logger = setup_logging(stream=stream, level="INFO", logger_name="knowledgeflow.llm")
    provider = ScriptedProvider([valid_analysis_text])
    await make_service(provider, logger=logger).analyze_raw(mixed_raw_content)

    logged = stream.getvalue()
    assert "stage=llm" in logged
    assert "chunk_count=1" in logged
    assert "prompt_version=analysis_prompt_v1" in logged


# --------------------------------------------------------------------------- #
# from_settings
# --------------------------------------------------------------------------- #
async def test_from_settings_uses_configured_attempts() -> None:
    settings = Settings(_env_file=None, max_llm_attempts=2)  # type: ignore[call-arg]
    provider = ScriptedProvider([invalid_text()] * 5)
    service = LLMAnalysisService.from_settings(provider, settings)
    outcome = await service.analyze_text("这句话有内容。")

    assert provider.call_count == 2
    assert outcome.success is False


async def test_from_settings_uses_configured_budget(long_raw_content) -> None:
    settings = Settings(_env_file=None, max_total_input_chars=1000)  # type: ignore[call-arg]
    provider = ScriptedProvider([valid_text()] * 40)
    service = LLMAnalysisService.from_settings(provider, settings)
    outcome = await service.analyze_raw(long_raw_content)

    assert outcome.prepared_text is not None
    assert len(outcome.prepared_text.text) == 1000
    assert outcome.success is True


# --------------------------------------------------------------------------- #
# 错误信息不含正文
# --------------------------------------------------------------------------- #
async def test_error_message_never_contains_source_text() -> None:
    marker = "绝密标记XYZ"
    provider = ScriptedProvider([invalid_text()] * 3)
    outcome = await make_service(provider).analyze_text(f"{marker}。这句话有内容。")
    assert marker not in (outcome.error_message or "")
