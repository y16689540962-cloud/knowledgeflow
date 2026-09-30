"""LLM 分析服务：重试状态机 + 分块 + synthesis（第十二节 / 第十一节 / 第十节）。

调用链（单次 attempt）::

    构建 prompt
      → provider.complete()            # 拿到**原始文本**
      → extract_json()                 # 只修格式
      → ContentAnalysis.model_validate()
      → 通过则结束；否则带上错误提示重试

状态机（``MAX_LLM_ATTEMPTS = 3``，禁止无限重试）：

| 第 1 次 | 正常请求 |
| JSON 无法解析 | 执行一次 JSON repair（只修格式） |
| 第 2 次 | 重新请求 LLM（携带上次的错误提示） |
| 第 3 次失败 | error_type = LLM_INVALID_OUTPUT |

分块路径：**synthesis 也走完全相同的链路**，只是 prompt 换成
``synthesis_prompt_v1``，绝不让裸输出直接落库。

Grounding Check 放在**最终结果**上执行（单块结果或 synthesis 结果），
分块中间结果不单独跑规则 —— 合并后再对完整源文本核对，避免分块级误报。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from time import perf_counter
from typing import Any, Final, Sequence

from pydantic import ValidationError

from app.chunking.budget import (
    DEFAULT_PROMPT_RESERVE_CHARS,
    PreparedText,
    TextBudget,
    apply_budget,
    chunk_chars_for,
)
from app.chunking.chunker import Chunk, chunk_text
from app.config import Settings
from app.errors import ErrorType, KnowledgeFlowError, LLMError, LLMInvalidOutputError
from app.grounding.aliases import AliasResolver, IdentityAliasResolver
from app.grounding.rules import GroundingReport, apply_grounding
from app.llm.json_extraction import JSONExtractionError, extract_json
from app.llm.prompts import (
    ANALYSIS_PROMPT_VERSION,
    RESPONSE_JSON_SCHEMA,
    SYNTHESIS_PROMPT_VERSION,
    Prompt,
    build_analysis_prompt,
    build_retry_hint,
    build_synthesis_prompt,
)
from app.logging_config import log_event
from app.providers.base import LLMProvider
from app.schemas.analysis import ContentAnalysis
from app.schemas.content import RawContent

#: attempt 的失败阶段。
STEP_TRANSPORT: Final[str] = "transport"
STEP_EXTRACTION: Final[str] = "extraction"
STEP_VALIDATION: Final[str] = "validation"

DEFAULT_MAX_ATTEMPTS: Final[int] = 3
ERROR_MESSAGE_LIMIT: Final[int] = 500


@dataclass(frozen=True)
class AttemptRecord:
    attempt: int
    phase: str
    prompt_version: str
    elapsed_ms: float
    failed_step: str | None
    error_type: str | None
    error_message: str | None
    repairs: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.failed_step is None


@dataclass(frozen=True)
class AnalysisOutcome:
    success: bool
    analysis: ContentAnalysis | None
    prompt_version: str
    model: str
    provider: str
    chunk_count: int
    chunks: tuple[Chunk, ...] = ()
    chunk_analyses: tuple[ContentAnalysis, ...] = ()
    prepared_text: PreparedText | None = None
    grounding: GroundingReport | None = None
    attempts: tuple[AttemptRecord, ...] = ()
    error_type: str | None = None
    error_message: str | None = None

    @property
    def repairs(self) -> tuple[str, ...]:
        return tuple(r for record in self.attempts for r in record.repairs)


class LLMAnalysisService:
    STAGE: Final[str] = "llm"

    def __init__(
        self,
        provider: LLMProvider,
        *,
        budget: TextBudget,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        alias_resolver: AliasResolver | None = None,
        prompt_reserve_chars: int = DEFAULT_PROMPT_RESERVE_CHARS,
        schema: dict[str, Any] | None = None,
        logger: logging.Logger | None = None,
        overlap_sentences: int = 1,
    ) -> None:
        self._provider = provider
        self._budget = budget
        self._max_attempts = max(1, max_attempts)
        self._alias_resolver = alias_resolver or IdentityAliasResolver()
        self._chunk_chars = chunk_chars_for(budget.max_total_input_chars, prompt_reserve_chars)
        self._schema = schema or RESPONSE_JSON_SCHEMA
        self._logger = logger or logging.getLogger("knowledgeflow.llm")
        self._overlap_sentences = overlap_sentences

    @classmethod
    def from_settings(
        cls,
        provider: LLMProvider,
        settings: Settings,
        **kwargs: Any,
    ) -> "LLMAnalysisService":
        return cls(
            provider,
            budget=TextBudget.from_settings(settings),
            max_attempts=settings.max_llm_attempts,
            **kwargs,
        )

    # ------------------------------------------------------------------ #
    # 对外入口
    # ------------------------------------------------------------------ #
    async def analyze_raw(
        self,
        raw: RawContent,
        *,
        content_id: str | None = None,
    ) -> AnalysisOutcome:
        prepared = apply_budget(raw, self._budget)
        outcome = await self.analyze_text(prepared.text, content_id=content_id)
        return _replace(outcome, prepared_text=prepared)

    async def analyze_text(
        self,
        text: str,
        *,
        content_id: str | None = None,
    ) -> AnalysisOutcome:
        attempts: list[AttemptRecord] = []
        chunk_analyses: list[ContentAnalysis] = []

        chunks = tuple(
            chunk_text(
                text,
                max_chars=self._chunk_chars,
                overlap_sentences=self._overlap_sentences,
            )
        )
        if not chunks:
            # 没有任何可分析文本：明确失败，绝不产出「空的成功」。
            failure = LLMInvalidOutputError(
                "源文本为空，没有可分析的内容",
                error_type=ErrorType.EMPTY_SOURCE_TEXT,
            )
            return self._failure(
                exc=failure,
                attempts=attempts,
                chunks=(),
                chunk_analyses=(),
                prompt_version=ANALYSIS_PROMPT_VERSION,
                content_id=content_id,
            )

        for chunk in chunks:
            prompt = build_analysis_prompt(
                chunk.text,
                chunk_index=chunk.index + 1,
                chunk_total=len(chunks),
            )
            try:
                analysis = await self._complete_with_retry(
                    prompt,
                    phase=f"chunk-{chunk.index + 1}",
                    attempts=attempts,
                    content_id=content_id,
                )
            except KnowledgeFlowError as exc:
                return self._failure(
                    exc=exc,
                    attempts=attempts,
                    chunks=chunks,
                    chunk_analyses=chunk_analyses,
                    prompt_version=prompt.version,
                    content_id=content_id,
                )
            chunk_analyses.append(analysis)

        if len(chunks) == 1:
            merged = chunk_analyses[0]
            final_prompt_version = ANALYSIS_PROMPT_VERSION
        else:
            synthesis = build_synthesis_prompt(chunk_analyses)
            try:
                merged = await self._complete_with_retry(
                    synthesis,
                    phase="synthesis",
                    attempts=attempts,
                    content_id=content_id,
                )
            except KnowledgeFlowError as exc:
                return self._failure(
                    exc=exc,
                    attempts=attempts,
                    chunks=chunks,
                    chunk_analyses=chunk_analyses,
                    prompt_version=synthesis.version,
                    content_id=content_id,
                )
            final_prompt_version = SYNTHESIS_PROMPT_VERSION

        grounded, report = apply_grounding(merged, text, resolver=self._alias_resolver)

        log_event(
            self._logger,
            logging.INFO,
            stage=self.STAGE,
            message="分析完成",
            content_id=content_id,
            chunk_count=len(chunks),
            prompt_version=final_prompt_version,
            needs_verification=report.needs_verification_count,
            source_chars=len(text),
        )

        return AnalysisOutcome(
            success=True,
            analysis=grounded,
            prompt_version=final_prompt_version,
            model=self._provider.model_name,
            provider=self._provider.name,
            chunk_count=len(chunks),
            chunks=chunks,
            chunk_analyses=tuple(chunk_analyses),
            grounding=report,
            attempts=tuple(attempts),
        )

    # ------------------------------------------------------------------ #
    # 重试状态机
    # ------------------------------------------------------------------ #
    async def _complete_with_retry(
        self,
        prompt: Prompt,
        *,
        phase: str,
        attempts: list[AttemptRecord],
        content_id: str | None,
    ) -> ContentAnalysis:
        retry_hint: str | None = None
        last_error: KnowledgeFlowError | None = None

        for attempt_index in range(1, self._max_attempts + 1):
            current_prompt = prompt
            if retry_hint:
                current_prompt = _with_retry_hint(prompt, retry_hint)

            started = perf_counter()
            raw_output = ""
            try:
                raw_output = await self._provider.complete(
                    current_prompt.user, self._schema, system=current_prompt.system
                )
            except LLMError as exc:
                elapsed = _elapsed_ms(started)
                attempts.append(
                    _record(
                        attempt_index,
                        phase,
                        current_prompt.version,
                        elapsed,
                        STEP_TRANSPORT,
                        exc,
                    )
                )
                last_error = exc
                retry_hint = build_retry_hint(f"上一次调用失败：{exc.message}")
                log_event(
                    self._logger,
                    logging.WARNING,
                    stage=self.STAGE,
                    message="LLM 调用失败",
                    content_id=content_id,
                    attempt=attempt_index,
                    phase=phase,
                    elapsed_ms=round(elapsed, 1),
                    error_type=exc.error_type_value,
                    error_summary=exc.message,
                )
                continue

            # 抽取 + 修复（只修格式）
            try:
                extraction = extract_json(raw_output)
            except JSONExtractionError as exc:
                elapsed = _elapsed_ms(started)
                attempts.append(
                    _record(
                        attempt_index,
                        phase,
                        current_prompt.version,
                        elapsed,
                        STEP_EXTRACTION,
                        exc,
                    )
                )
                last_error = exc
                retry_hint = build_retry_hint(
                    f"上一次输出不是可解析的 JSON（{exc.message}）。请只输出一个 JSON 对象，"
                    "不要代码围栏、不要任何解释文字。"
                )
                log_event(
                    self._logger,
                    logging.WARNING,
                    stage=self.STAGE,
                    message="LLM 输出无法解析为 JSON",
                    content_id=content_id,
                    attempt=attempt_index,
                    phase=phase,
                    elapsed_ms=round(elapsed, 1),
                    error_summary=exc.message,
                    raw_length=len(raw_output),
                )
                continue

            # Pydantic 校验
            try:
                analysis = ContentAnalysis.model_validate(extraction.payload)
            except ValidationError as exc:
                elapsed = _elapsed_ms(started)
                summary = summarize_validation_error(exc)
                wrapped = LLMInvalidOutputError(
                    f"LLM 输出未通过 Schema 校验：{summary}",
                    context={"errors": len(exc.errors())},
                )
                attempts.append(
                    _record(
                        attempt_index,
                        phase,
                        current_prompt.version,
                        elapsed,
                        STEP_VALIDATION,
                        wrapped,
                        repairs=extraction.repairs,
                    )
                )
                last_error = wrapped
                retry_hint = build_retry_hint(
                    f"上一次输出的字段不符契约（{summary}）。请严格按 JSON Schema 重新输出。"
                )
                log_event(
                    self._logger,
                    logging.WARNING,
                    stage=self.STAGE,
                    message="LLM 输出未通过校验",
                    content_id=content_id,
                    attempt=attempt_index,
                    phase=phase,
                    elapsed_ms=round(elapsed, 1),
                    error_summary=summary,
                    errors=len(exc.errors()),
                )
                continue

            elapsed = _elapsed_ms(started)
            attempts.append(
                _record(
                    attempt_index,
                    phase,
                    current_prompt.version,
                    elapsed,
                    None,
                    None,
                    repairs=extraction.repairs,
                )
            )
            if extraction.repairs:
                log_event(
                    self._logger,
                    logging.INFO,
                    stage=self.STAGE,
                    message="LLM 输出经格式修复后可用",
                    content_id=content_id,
                    attempt=attempt_index,
                    phase=phase,
                    repairs=",".join(extraction.repairs),
                )
            return analysis

        # 三次都用尽：对外统一报 LLM_INVALID_OUTPUT，但保留传输层的错误类型（如果是传输失败）
        final_error = _final_error(last_error, self._max_attempts)
        raise final_error

    # ------------------------------------------------------------------ #
    # 失败结果
    # ------------------------------------------------------------------ #
    def _failure(
        self,
        *,
        exc: KnowledgeFlowError,
        attempts: Sequence[AttemptRecord],
        chunks: Sequence[Chunk],
        chunk_analyses: Sequence[ContentAnalysis],
        prompt_version: str,
        content_id: str | None = None,
    ) -> AnalysisOutcome:
        message = exc.message[:ERROR_MESSAGE_LIMIT]
        log_event(
            self._logger,
            logging.ERROR,
            stage=self.STAGE,
            message="分析失败",
            content_id=content_id,
            attempts=len(attempts),
            chunk_count=len(chunks),
            error_type=exc.error_type_value,
            error_summary=message,
        )
        return AnalysisOutcome(
            success=False,
            analysis=None,
            prompt_version=prompt_version,
            model=self._provider.model_name,
            provider=self._provider.name,
            chunk_count=len(chunks),
            chunks=tuple(chunks),
            chunk_analyses=tuple(chunk_analyses),
            attempts=tuple(attempts),
            error_type=exc.error_type_value,
            error_message=message,
        )


# ---------------------------------------------------------------------- #
# 辅助
# ---------------------------------------------------------------------- #
def _elapsed_ms(started: float) -> float:
    return (perf_counter() - started) * 1000.0


def _record(
    attempt: int,
    phase: str,
    prompt_version: str,
    elapsed_ms: float,
    failed_step: str | None,
    exc: KnowledgeFlowError | None,
    *,
    repairs: tuple[str, ...] = (),
) -> AttemptRecord:
    return AttemptRecord(
        attempt=attempt,
        phase=phase,
        prompt_version=prompt_version,
        elapsed_ms=round(elapsed_ms, 3),
        failed_step=failed_step,
        error_type=exc.error_type_value if exc else None,
        error_message=(exc.message[:ERROR_MESSAGE_LIMIT] if exc else None),
        repairs=repairs,
    )


def _with_retry_hint(prompt: Prompt, hint: str) -> Prompt:
    """把上一次的错误提示追加进 user 内容（prompt version 保持不变）。"""
    return Prompt(
        version=prompt.version,
        system=prompt.system,
        user=f"{prompt.user}\n\n【重试提示】{hint}",
    )


def summarize_validation_error(exc: ValidationError, *, limit: int = 5) -> str:
    """把 Pydantic 错误压成短摘要。

    只取 ``loc`` / ``msg`` / ``type``，**绝不**带上 ``input`` ——
    那样会把模型原文（乃至源内容）写进日志与数据库。
    """
    errors = exc.errors()
    parts: list[str] = []
    for error in errors[:limit]:
        location = ".".join(str(item) for item in error.get("loc", ())) or "<root>"
        parts.append(f"{location}: {error.get('msg')} ({error.get('type')})")
    if len(errors) > limit:
        parts.append(f"…共 {len(errors)} 处")
    return "; ".join(parts)


def _final_error(last_error: KnowledgeFlowError | None, max_attempts: int) -> KnowledgeFlowError:
    if isinstance(last_error, LLMError) and last_error.error_type in (
        ErrorType.LLM_TIMEOUT,
        ErrorType.LLM_API_ERROR,
        ErrorType.LLM_CONFIG_ERROR,
    ):
        # 传输层问题：重试没用，保留真实的错误类型
        return LLMError(
            f"{max_attempts} 次调用均失败（传输层）：{last_error.message}"[:ERROR_MESSAGE_LIMIT],
            error_type=last_error.error_type,
            context=dict(last_error.context),
        )
    detail = last_error.message if last_error else "未知原因"
    return LLMInvalidOutputError(
        f"{max_attempts} 次尝试均未得到合法输出：{detail}"[:ERROR_MESSAGE_LIMIT],
        context={"attempts": max_attempts},
    )


def _replace(outcome: AnalysisOutcome, **updates: Any) -> AnalysisOutcome:
    return replace(outcome, **updates)


__all__ = [
    "LLMAnalysisService",
    "AnalysisOutcome",
    "AttemptRecord",
    "STEP_TRANSPORT",
    "STEP_EXTRACTION",
    "STEP_VALIDATION",
    "DEFAULT_MAX_ATTEMPTS",
    "ERROR_MESSAGE_LIMIT",
    "summarize_validation_error",
]
