"""ProcessingPipeline：把整条 A 线串起来（定稿文档第十六节）。

```text
RawContent → Normalize → Deduplicate → Persist Content
   → Prepare Text → (chunk?) → LLM → Validate → Grounding Check
   → Persist Analysis → Extract Topics → Extract Entities → Normalize Aliases
   → Render Markdown → Write Obsidian → Completed
```

铁律：

* 任何一步失败：``status = failed``，写入 ``error_type`` 与 ``error_message``，
  **绝不静默 ``except: pass``**
* 失败也要尽量把原文落到 ``Failed/``，方便人工重试
* 每一步都记 ``StepRecord``（步骤名 / 耗时 / 失败原因），故障定位不靠猜
* 去重（Phase 5）：先认精确身份 ``(source, source_id)``，再认同 source 内的内容指纹
  ``content_hash``。**跨来源不合并** —— 定稿第七节的哈希白名单含 ``source``，
  抖音与手贴的同一段文案本来就哈希不同（见进度文档 Q1）
"""

from __future__ import annotations

import contextlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, AsyncIterator, Final

from app.chunking.budget import PreparedText, TextBudget, apply_budget
from app.config import Settings, validate_runtime_requirements
from app.db.mappers import AnalysisInsert, ContentInsert
from app.db.models import Content
from app.db.repository import ContentRepository
from app.db.session import Database
from app.errors import (
    ContentNotFoundError,
    ErrorType,
    KnowledgeFlowError,
    PipelineStepError,
    RuntimeRequirementError,
)
from app.knowledge.aliases import DatabaseAliasLoader
from app.knowledge.entities import AliasConflict, EntityRegistry, ResolvedEntity
from app.knowledge.topics import ResolvedTopic, TopicRegistry
from app.llm.service import AnalysisOutcome, LLMAnalysisService
from app.logging_config import log_event
from app.normalization.service import NormalizedContent, normalize_content
from app.obsidian.renderer import (
    DEFAULT_TOP_CLAIMS,
    LinkedEntity,
    NoteContext,
    RenderedNote,
    render_failure_note,
    render_note,
)
from app.obsidian.writer import Identity, ObsidianWriter
from app.pipeline.recovery import RecoveryReport, TaskRecovery
from app.pipeline.result import (
    STEP_ANALYZE,
    STEP_COMPLETE,
    STEP_DEDUPLICATE,
    STEP_EXTRACT_ENTITIES,
    STEP_EXTRACT_TOPICS,
    STEP_LOAD_CONTENT,
    STEP_NORMALIZE,
    STEP_NORMALIZE_ALIASES,
    STEP_PERSIST_ANALYSIS,
    STEP_PERSIST_CONTENT,
    STEP_PREFLIGHT,
    STEP_PREPARE_TEXT,
    STEP_RENDER_MARKDOWN,
    STEP_WRITE_OBSIDIAN,
    ProcessingResult,
    StepRecord,
)
from app.providers.base import LLMProvider
from app.schemas.analysis import ContentAnalysis
from app.schemas.content import RawContent
from app.schemas.enums import MEDIA_TYPES
from app.utils import utc_now_iso

STAGE: Final[str] = "pipeline"
ERROR_MESSAGE_LIMIT: Final[int] = 500
STEP_DETAIL_LIMIT: Final[int] = 200

#: 命中重复时是靠哪个键认出来的。
DEDUP_BY_SOURCE_ID: Final[str] = "source_id"
DEDUP_BY_CONTENT_HASH: Final[str] = "content_hash"


@dataclass
class _StepBox:
    """步骤体内可以往这里写一句人看的说明。"""

    detail: str | None = None


def _ms(started: float) -> float:
    return round((perf_counter() - started) * 1000.0, 3)


def _short(text: str | None, limit: int = STEP_DETAIL_LIMIT) -> str | None:
    if not text:
        return None
    collapsed = " ".join(text.split())
    return collapsed[:limit]


def _error_type_of(value: str | None) -> ErrorType:
    if not value:
        return ErrorType.UNKNOWN_ERROR
    try:
        return ErrorType(value)
    except ValueError:  # pragma: no cover - 只有脏数据才会走到
        return ErrorType.UNKNOWN_ERROR


class ProcessingPipeline:
    """A 线流水线。构造一次可以反复 ``process()``。"""

    STAGE = STAGE

    def __init__(
        self,
        *,
        settings: Settings,
        provider: LLMProvider,
        repository: ContentRepository,
        entities: EntityRegistry,
        topics: TopicRegistry,
        alias_loader: DatabaseAliasLoader,
        vault_root: str | os.PathLike[str] | None = None,
        writer: ObsidianWriter | None = None,
        logger: logging.Logger | None = None,
        budget: TextBudget | None = None,
        top_claims: int = DEFAULT_TOP_CLAIMS,
        recovery: TaskRecovery | None = None,
    ) -> None:
        self._settings = settings
        self._provider = provider
        self._repository = repository
        self._entities = entities
        self._topics = topics
        self._alias_loader = alias_loader
        self._budget = budget or TextBudget.from_settings(settings)
        self._top_claims = top_claims
        self._logger = logger or logging.getLogger("knowledgeflow.pipeline")
        self._writer = writer
        self._vault_root: Path | None = (
            Path(vault_root) if vault_root is not None else settings.obsidian_vault
        )
        self._recovery = recovery or TaskRecovery(
            repository,
            timeout_seconds=settings.task_reset_timeout_seconds,
            logger=self._logger,
        )

    # ------------------------------------------------------------------ #
    # 构造
    # ------------------------------------------------------------------ #
    @classmethod
    def from_database(
        cls,
        *,
        settings: Settings,
        provider: LLMProvider,
        database: Database,
        vault_root: str | os.PathLike[str] | None = None,
        writer: ObsidianWriter | None = None,
        **kwargs: Any,
    ) -> "ProcessingPipeline":
        """一个 ``Database`` 接出全部依赖。"""
        factory = database.session_factory
        # loader 与 registry **共用同一个对象**：实体合并后 registry 能让它的快照失效，
        # 不会出现「合并完了但 Grounding 还在用旧索引」。
        loader = DatabaseAliasLoader(session_factory=factory)
        return cls(
            settings=settings,
            provider=provider,
            repository=ContentRepository(factory),
            entities=EntityRegistry(factory, alias_loader=loader),
            topics=TopicRegistry(factory),
            alias_loader=loader,
            vault_root=vault_root,
            writer=writer,
            **kwargs,
        )

    # ------------------------------------------------------------------ #
    # 只读属性（测试与 demo 用）
    # ------------------------------------------------------------------ #
    @property
    def budget(self) -> TextBudget:
        return self._budget

    @property
    def vault_root(self) -> Path | None:
        return self._vault_root

    @property
    def recovery(self) -> TaskRecovery:
        return self._recovery

    @property
    def entities(self) -> EntityRegistry:
        return self._entities

    @property
    def topics(self) -> TopicRegistry:
        return self._topics

    @property
    def alias_loader(self) -> DatabaseAliasLoader:
        return self._alias_loader

    # ------------------------------------------------------------------ #
    # 入口
    # ------------------------------------------------------------------ #
    async def process(
        self,
        raw: RawContent,
        *,
        content_id: str | None = None,
    ) -> ProcessingResult:
        return await self._run(
            raw, content_id=content_id, steps=[], started=perf_counter(), force=False
        )

    async def reprocess(self, content_id: str) -> ProcessingResult:
        """按 ``content_id`` 重跑（第十七节的「重新处理」/ 第十三节的 reprocess）。

        会**新增一行 analyses**，旧历史完整保留；不会命中「重复输入」短路。
        """
        steps: list[StepRecord] = []
        started = perf_counter()
        try:
            async with self._step(steps, STEP_LOAD_CONTENT) as box:
                raw = await self._load_raw(content_id)
                box.detail = f"source={raw.source} media_type={raw.media_type}"
        except KnowledgeFlowError as exc:
            return await self._fail(
                exc=exc, steps=steps, state={"content_id": content_id}, started=started
            )
        return await self._run(
            raw, content_id=content_id, steps=steps, started=started, force=True
        )

    async def recover_and_resume(self) -> RecoveryReport:
        """服务启动时调用：重置超时任务并重新处理（第十七节）。"""
        cutoff = self._recovery.cutoff()
        timeout = self._recovery.timeout_seconds
        reset = await self._recovery.reset_stale()

        resumed: list[str] = []
        failures: list[tuple[str, str]] = []
        for content_id in reset:
            result = await self.reprocess(content_id)
            if result.success:
                resumed.append(content_id)
            else:
                failures.append((content_id, result.error_type or ErrorType.UNKNOWN_ERROR.value))

        log_event(
            self._logger,
            logging.INFO,
            stage=STAGE,
            message="任务恢复完成",
            reset=len(reset),
            resumed=len(resumed),
            failed=len(failures),
        )
        return RecoveryReport(
            cutoff=cutoff,
            timeout_seconds=timeout,
            reset=reset,
            resumed=tuple(resumed),
            resume_failures=tuple(failures),
        )

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    async def _run(
        self,
        raw: RawContent,
        *,
        content_id: str | None,
        steps: list[StepRecord],
        started: float,
        force: bool,
    ) -> ProcessingResult:
        state: dict[str, Any] = {"raw": raw}
        try:
            async with self._step(steps, STEP_PREFLIGHT) as box:
                self._preflight()
                box.detail = f"vault={self._vault_root}" if self._vault_root else "injected-writer"

            async with self._step(steps, STEP_NORMALIZE) as box:
                normalized = normalize_content(raw)
                state["normalized"] = normalized
                box.detail = (
                    f"source_id={normalized.source_id} "
                    f"manual_review={normalized.needs_manual_review}"
                )

            existing: Content | None = None
            deduplicated_by: str | None = None
            async with self._step(steps, STEP_DEDUPLICATE) as box:
                # 无论是否强制重跑都要**先查**：查到就复用这一行，
                # 否则 force 会撞上 UNIQUE(source, source_id)。
                existing, deduplicated_by = await self._find_existing(normalized)
                state["existing"] = existing
                state["deduplicated_by"] = deduplicated_by
                if existing is None:
                    box.detail = "无重复"
                elif deduplicated_by == DEDUP_BY_CONTENT_HASH:
                    box.detail = (
                        f"同 source 内 content_hash 命中（source_id 不同）"
                        f" status={existing.status}"
                    )
                elif force:
                    box.detail = f"强制重跑已有内容 status={existing.status}"
                else:
                    box.detail = f"命中已有内容 status={existing.status}"

            if (
                not force
                and existing is not None
                and existing.status == "completed"
                and existing.current_analysis_id
            ):
                log_event(
                    self._logger,
                    logging.INFO,
                    stage=STAGE,
                    message="重复输入，跳过处理",
                    content_id=existing.id,
                    source_id=normalized.source_id,
                    deduplicated_by=deduplicated_by,
                )
                return ProcessingResult(
                    outcome="duplicate",
                    content_id=existing.id,
                    status=existing.status,
                    analysis_id=existing.current_analysis_id,
                    duplicate_of=existing.id,
                    deduplicated_by=deduplicated_by,
                    steps=tuple(steps),
                    elapsed_ms=_ms(started),
                )

            async with self._step(steps, STEP_PERSIST_CONTENT) as box:
                insert = ContentInsert.from_normalized(
                    normalized,
                    content_id=content_id or (existing.id if existing is not None else None),
                    status="processing",
                )
                if existing is None:
                    content_id = await self._repository.insert_content(insert)
                    content_created_at = insert.created_at
                else:
                    await self._repository.update_payload(existing.id, insert)
                    await self._repository.update_status(existing.id, "processing")
                    content_id = existing.id
                    content_created_at = existing.created_at
                state["content_id"] = content_id
                state["content_created_at"] = content_created_at
                box.detail = f"content_id={content_id}"

            async with self._step(steps, STEP_PREPARE_TEXT) as box:
                prepared = apply_budget(raw, self._budget)
                state["prepared"] = prepared
                box.detail = (
                    f"{len(prepared.text)} chars chunk_chars={self._budget.chunk_chars} "
                    f"truncated={prepared.truncated} cleaned={prepared.cleaned}"
                )

            async with self._step(steps, STEP_ANALYZE) as box:
                outcome = await self._analyze(prepared, content_id)
                state["outcome"] = outcome
                if outcome.analysis is None:
                    raise KnowledgeFlowError(
                        outcome.error_message or "LLM 分析失败",
                        error_type=_error_type_of(outcome.error_type),
                    )
                state["analysis"] = outcome.analysis
                box.detail = (
                    f"chunks={outcome.chunk_count} claims={len(outcome.analysis.claims)} "
                    f"prompt_version={outcome.prompt_version}"
                )

            analysis: ContentAnalysis = state["analysis"]
            processed_at = utc_now_iso()

            async with self._step(steps, STEP_PERSIST_ANALYSIS) as box:
                analysis_id = await self._repository.save_analysis_atomic(
                    AnalysisInsert(
                        content_id=content_id,
                        structured_json=analysis.model_dump_json(),
                        prompt_version=outcome.prompt_version,
                        analysis_type=analysis.analysis_type,
                        summary=analysis.summary,
                        model=outcome.model,
                        chunk_count=outcome.chunk_count,
                    )
                )
                state["analysis_id"] = analysis_id
                box.detail = f"analysis_id={analysis_id}"

            async with self._step(steps, STEP_EXTRACT_TOPICS) as box:
                resolved_topics = await self._topics.resolve_many(list(analysis.topics))
                if resolved_topics:
                    await self._topics.link_content(
                        content_id, [(item.topic_id, None) for item in resolved_topics]
                    )
                state["topics"] = resolved_topics
                box.detail = f"topics={len(resolved_topics)}"

            async with self._step(steps, STEP_EXTRACT_ENTITIES) as box:
                resolved_entities = await self._entities.resolve_many(
                    [(entity.name, entity.type) for entity in analysis.entities]
                )
                if resolved_entities:
                    await self._entities.link_content(
                        content_id, [(item.entity_id, None) for item in resolved_entities]
                    )
                state["entities"] = resolved_entities
                box.detail = f"entities={len(resolved_entities)}"

            async with self._step(steps, STEP_NORMALIZE_ALIASES) as box:
                conflicts = await self._normalize_aliases(analysis, resolved_entities)
                # 刷新别名索引：本轮新建的实体必须在**下一次** Grounding 前可见
                await self._alias_loader.load_index(use_cache=False)
                state["alias_conflicts"] = conflicts
                box.detail = f"conflicts={len(conflicts)}"

            async with self._step(steps, STEP_RENDER_MARKDOWN) as box:
                note = render_note(
                    self._build_context(
                        content_id=content_id,
                        raw=raw,
                        normalized=normalized,
                        analysis=analysis,
                        entities=resolved_entities,
                        topics=resolved_topics,
                        outcome=outcome,
                        created_at=state["content_created_at"],
                        processed_at=processed_at,
                    ),
                    top_claims=self._top_claims,
                )
                state["note"] = note
                box.detail = f"{len(note.markdown)} chars file={note.filename}.md"

            async with self._step(steps, STEP_WRITE_OBSIDIAN) as box:
                identity: Identity = (normalized.source, normalized.source_id)
                written = self._make_writer().write_rendered(
                    note, kind="processed", identity=identity
                )
                state["written"] = written
                box.detail = f"{written.relative_path} ({written.status})"

            async with self._step(steps, STEP_COMPLETE):
                await self._repository.update_status(
                    content_id, "completed", processed_at=processed_at
                )

        except KnowledgeFlowError as exc:
            return await self._fail(exc=exc, steps=steps, state=state, started=started)

        written = state.get("written")
        outcome = state["outcome"]
        analysis = state["analysis"]
        return ProcessingResult(
            outcome="completed",
            content_id=content_id,
            status="completed",
            analysis_id=state.get("analysis_id"),
            note_path=written.relative_path if written is not None else None,
            prompt_version=outcome.prompt_version,
            model=outcome.model,
            chunk_count=outcome.chunk_count,
            claim_count=len(analysis.claims),
            unverified_count=len(analysis.unverified_claims),
            entity_count=len(state.get("entities") or ()),
            topic_count=len(state.get("topics") or ()),
            needs_manual_review=bool(normalized.needs_manual_review),
            elapsed_ms=_ms(started),
            steps=tuple(steps),
        )

    # ------------------------------------------------------------------ #
    # 各步实现
    # ------------------------------------------------------------------ #
    def _preflight(self) -> None:
        """真正写 Obsidian 之前就要报错，不要白烧一轮 LLM 调用（第二十五节）。"""
        if self._writer is not None:
            return
        if self._vault_root is not None:
            if not self._vault_root.is_dir():
                raise RuntimeRequirementError(
                    f"OBSIDIAN_VAULT_PATH 指向的目录不存在：{self._vault_root}",
                    error_type=ErrorType.CONFIG_OBSIDIAN_VAULT_NOT_FOUND,
                    context={"path": str(self._vault_root)},
                )
            return
        validate_runtime_requirements(self._settings, require_obsidian=True)

    def _make_writer(self) -> ObsidianWriter:
        if self._writer is not None:
            return self._writer
        if self._vault_root is None:  # pragma: no cover - preflight 已经拦掉
            raise RuntimeRequirementError(
                "需要写入 Obsidian 但 OBSIDIAN_VAULT_PATH 未配置",
                error_type=ErrorType.CONFIG_OBSIDIAN_VAULT_MISSING,
            )
        return ObsidianWriter(self._vault_root)

    async def _find_existing(
        self, normalized: NormalizedContent
    ) -> tuple[Content | None, str | None]:
        """依次按「精确身份」→「内容指纹」找已有内容。

        * ``(source, source_id)``：最强身份，先认
        * 同 source 内 ``content_hash``：定稿第七节的白名单**含 source**，
          所以跨来源合并本来就不在定义里（Q1 已定）
        * 哈希命中多行时：**优先取已完成的**（能直接复用它的分析），
          否则取最早的一行（把它当成这条内容的正身来重跑）
        * **哈希没有区分度时不判重**（标题/作者/简介全空 → 所有内容哈希都一样）
        """
        exact = await self._repository.get_content_by_source(
            normalized.source, normalized.source_id
        )
        if exact is not None:
            return exact, DEDUP_BY_SOURCE_ID

        if not normalized.hash_is_discriminating:
            # 指纹只由 source 组成 → 它对「是不是同一条内容」没有任何信息量，
            # 拿去判重只会误杀（例如两条不同视频都没取到元数据）。
            return None, None

        candidates = await self._repository.find_by_source_and_content_hash(
            normalized.source,
            normalized.content_hash,
            content_hash_version=normalized.content_hash_version,
        )
        if not candidates:
            return None, None
        for row in candidates:
            if row.status == "completed" and row.current_analysis_id:
                return row, DEDUP_BY_CONTENT_HASH
        return candidates[0], DEDUP_BY_CONTENT_HASH

    async def _analyze(self, prepared: PreparedText, content_id: str) -> AnalysisOutcome:
        """跑 LLM 分析。Grounding 用的别名快照在**调用前**取，保证是最新的。"""
        snapshot = await self._alias_loader.snapshot()
        service = LLMAnalysisService(
            self._provider,
            budget=self._budget,
            max_attempts=self._settings.max_llm_attempts,
            alias_resolver=snapshot,
            logger=self._logger,
        )
        return await service.analyze_text(prepared.text, content_id=content_id)

    async def _normalize_aliases(
        self,
        analysis: ContentAnalysis,
        resolved: tuple[ResolvedEntity, ...],
    ) -> tuple[AliasConflict, ...]:
        """把「分析里出现的表面形式 → 归一化后的 canonical」补进 alias 表。

        表面形式 == canonical 时**不登记**（alias 表只放额外表面形式）；
        已归属别的 canonical 时记冲突、**不抢不改**。
        """
        index = await self._alias_loader.load_index()
        by_canonical = {item.canonical_name: item for item in resolved}
        conflicts: list[AliasConflict] = []

        for entity in analysis.entities:
            surface = " ".join((entity.name or "").split())
            if not surface:
                continue
            canonical = index.canonical_of(surface)
            if canonical is None or canonical == surface:
                continue
            target = by_canonical.get(canonical)
            if target is None:
                continue
            registration = await self._entities.add_alias(target.entity_id, surface)
            if registration.conflict is not None:
                conflicts.append(registration.conflict)
                log_event(
                    self._logger,
                    logging.WARNING,
                    stage=STAGE,
                    message="别名冲突，已跳过",
                    alias=surface,
                    existing_entity_id=registration.conflict.existing_entity_id,
                )
        return tuple(conflicts)

    async def _load_raw(self, content_id: str) -> RawContent:
        row = await self._repository.get_content(content_id)
        if row is None:
            raise ContentNotFoundError(
                f"content 不存在：{content_id}", context={"content_id": content_id}
            )
        media_type = row.media_type if row.media_type in MEDIA_TYPES else "text"
        return RawContent(
            source=row.source,
            source_id=row.source_id,
            source_url=row.source_url or "",
            title=row.title,
            author=row.author,
            author_id=row.author_id,
            description=row.description,
            media_type=media_type,  # type: ignore[arg-type]
            media_path=None,
            raw_text=row.raw_text,
            transcript=row.transcript,
            ocr_text=row.ocr_text,
        )

    def _build_context(
        self,
        *,
        content_id: str,
        raw: RawContent,
        normalized: NormalizedContent,
        analysis: ContentAnalysis,
        entities: tuple[ResolvedEntity, ...],
        topics: tuple[ResolvedTopic, ...],
        outcome: AnalysisOutcome,
        created_at: str,
        processed_at: str,
    ) -> NoteContext:
        return NoteContext(
            content_id=content_id,
            source=normalized.source,
            source_id=normalized.source_id,
            analysis=analysis,
            source_url=raw.source_url or None,
            title=raw.title,
            author=raw.author,
            media_type=raw.media_type,
            created_at=created_at,
            processed_at=processed_at,
            needs_manual_review=normalized.needs_manual_review,
            content_hash_version=normalized.content_hash_version,
            prompt_version=outcome.prompt_version,
            model=outcome.model,
            entities=tuple(_linked(item) for item in entities),
            topics=tuple(item.name for item in topics),
            raw_text=raw.raw_text,
            transcript=raw.transcript,
            ocr_text=raw.ocr_text,
        )

    # ------------------------------------------------------------------ #
    # 失败处理
    # ------------------------------------------------------------------ #
    async def _fail(
        self,
        *,
        exc: KnowledgeFlowError,
        steps: list[StepRecord],
        state: dict[str, Any],
        started: float,
    ) -> ProcessingResult:
        content_id = state.get("content_id")
        error_type = exc.error_type_value
        message = (exc.message or "")[:ERROR_MESSAGE_LIMIT]

        status: str | None = None
        if content_id:
            try:
                await self._repository.update_status(
                    content_id, "failed", error_type=error_type, error_message=message
                )
                status = "failed"
            except Exception as inner:  # noqa: BLE001 - 失败路径绝不能再炸一次
                log_event(
                    self._logger,
                    logging.ERROR,
                    stage=STAGE,
                    message="写入失败状态时又出错了",
                    content_id=content_id,
                    exc_info=inner,
                    error_summary=str(inner),
                )

        note_path = await self._write_failure_note(state, error_type, message)

        prepared: PreparedText | None = state.get("prepared")
        log_event(
            self._logger,
            logging.ERROR,
            stage=STAGE,
            message="Pipeline 失败",
            content_id=content_id,
            error_type=error_type,
            error_summary=message,
            failed_step=_failed_step(steps),
            content_chars=len(prepared.text) if prepared else 0,
        )

        outcome_state: AnalysisOutcome | None = state.get("outcome")
        return ProcessingResult(
            outcome="failed",
            content_id=content_id,
            status=status,
            note_path=note_path,
            error_type=error_type,
            error_message=message,
            prompt_version=outcome_state.prompt_version if outcome_state else None,
            model=outcome_state.model if outcome_state else None,
            chunk_count=outcome_state.chunk_count if outcome_state else 0,
            elapsed_ms=_ms(started),
            steps=tuple(steps),
            extra={"failed_step": _failed_step(steps), "context": dict(exc.context)},
        )

    async def _write_failure_note(
        self, state: dict[str, Any], error_type: str, message: str
    ) -> str | None:
        """尽力把失败笔记写进 ``Failed/``；写失败只记日志，不改变原始错误。"""
        raw: RawContent | None = state.get("raw")
        if raw is None:
            return None
        if self._writer is None and self._vault_root is None:
            return None

        try:
            normalized: NormalizedContent | None = state.get("normalized")
            analysis: ContentAnalysis | None = state.get("analysis")
            if analysis is None:
                analysis = ContentAnalysis(
                    title=raw.title or "",
                    summary="",
                    analysis_type="mixed",
                    claims=[],
                    overall_confidence=0.0,
                )

            source = normalized.source if normalized else raw.source
            source_id = normalized.source_id if normalized else (raw.source_id or "")
            outcome_state: AnalysisOutcome | None = state.get("outcome")

            context = NoteContext(
                content_id=state.get("content_id") or "unknown",
                source=source,
                source_id=source_id,
                analysis=analysis,
                source_url=raw.source_url or None,
                title=raw.title,
                author=raw.author,
                media_type=raw.media_type,
                created_at=utc_now_iso(),
                processed_at=None,
                needs_manual_review=True,
                prompt_version=outcome_state.prompt_version if outcome_state else "",
                model=outcome_state.model if outcome_state else None,
                entities=tuple(_linked(item) for item in state.get("entities") or ()),
                topics=tuple(item.name for item in state.get("topics") or ()),
                raw_text=raw.raw_text,
                transcript=raw.transcript,
                ocr_text=raw.ocr_text,
            )
            note = render_failure_note(context, error_type=error_type, error_message=message)
            written = self._make_writer().write_rendered(
                note, kind="failed", identity=(source, source_id)
            )
            return written.relative_path
        except Exception as inner:  # noqa: BLE001 - 失败路径绝不能再炸一次
            log_event(
                self._logger,
                logging.WARNING,
                stage=STAGE,
                message="写失败笔记失败（已忽略）",
                error_summary=str(inner),
                exc_info=inner,
            )
            return None

    # ------------------------------------------------------------------ #
    # 步骤记录
    # ------------------------------------------------------------------ #
    @contextlib.asynccontextmanager
    async def _step(self, steps: list[StepRecord], name: str) -> AsyncIterator[_StepBox]:
        started = perf_counter()
        box = _StepBox()
        try:
            yield box
        except KnowledgeFlowError as exc:
            steps.append(
                StepRecord(
                    step=name,
                    status="failed",
                    elapsed_ms=_ms(started),
                    detail=_short(exc.message),
                    error_type=exc.error_type_value,
                )
            )
            raise
        except Exception as exc:  # noqa: BLE001 - 未预期异常也要留痕，绝不 pass
            steps.append(
                StepRecord(
                    step=name,
                    status="failed",
                    elapsed_ms=_ms(started),
                    detail=type(exc).__name__,
                    error_type=ErrorType.PIPELINE_STEP_FAILED.value,
                )
            )
            log_event(
                self._logger,
                logging.ERROR,
                stage=STAGE,
                message="步骤抛出未预期异常",
                step=name,
                error_summary=f"{type(exc).__name__}: {exc}",
                exc_info=exc,
            )
            raise PipelineStepError(
                f"步骤 {name} 抛出未预期异常：{type(exc).__name__}",
                context={"step": name, "exception": type(exc).__name__},
            ) from exc
        else:
            steps.append(
                StepRecord(step=name, status="ok", elapsed_ms=_ms(started), detail=_short(box.detail))
            )


def _linked(item: ResolvedEntity) -> LinkedEntity:
    return LinkedEntity(
        canonical_name=item.canonical_name,
        aliases=tuple(item.aliases),
        entity_type=item.entity_type,
    )


def _failed_step(steps: list[StepRecord]) -> str | None:
    for record in reversed(steps):
        if record.status == "failed":
            return record.step
    return None


__all__ = [
    "ProcessingPipeline",
    "STAGE",
    "ERROR_MESSAGE_LIMIT",
    "STEP_DETAIL_LIMIT",
    "DEDUP_BY_SOURCE_ID",
    "DEDUP_BY_CONTENT_HASH",
]
