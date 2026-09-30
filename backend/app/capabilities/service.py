"""把「可选能力」接到 ``RawContent`` 上。

这是 Phase 7 唯一对外有意义的入口：给它一条 ``RawContent``，
还你一条**补好了 ``transcript`` / ``ocr_text``** 的 ``RawContent``，外加这次调用的流水。

三条纪律：

1. **失败了不算处理失败。** 能力调用的问题一律降级为记录上的
   ``CapabilityAttempt``，不抛到调用方 —— 定稿第二条原则：
   不因为 ASR/OCR 环境问题阻塞 Core Pipeline。
2. **降级 ≠ 假装成功。** 每一次 skipped / unavailable / failed 都留下
   ``error_type`` 和原因，看得见。绝不返回一条「看起来处理过」但实际没处理的记录。
3. **这里不做文本预算。** ``MAX_TRANSCRIPT_CHARS`` / ``MAX_OCR_CHARS``
   由 :mod:`app.chunking.budget` 统一施加 —— 两处截断会让「到底在哪被切短」变成谜。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from typing import Final

from app.capabilities.base import (
    ASRProvider,
    CapabilityAttempt,
    OCRProvider,
)
from app.capabilities.null import NullASRProvider, NullOCRProvider
from app.errors import KnowledgeFlowError
from app.logging_config import log_event
from app.media.base import MediaDownloader
from app.schemas.content import RawContent
from app.schemas.enums import MediaType

LOGGER = logging.getLogger("knowledgeflow.capabilities")

STAGE: Final[str] = "capabilities"

#: 哪些形态要跑 ASR，哪些要跑 OCR。
ASR_MEDIA: frozenset[str] = frozenset({"video", "audio"})
OCR_MEDIA: frozenset[str] = frozenset({"image"})


def _capability_for(media_type: MediaType) -> str | None:
    """这条内容需要哪种外部能力；不需要则返回 ``None``。"""
    if media_type in ASR_MEDIA:
        return "asr"
    if media_type in OCR_MEDIA:
        return "ocr"
    return None


def _provider_name(provider: ASRProvider | OCRProvider) -> str:
    return provider.name


class MediaCapabilityService:
    """下载 / 转写 / 识别的编排入口。

    默认 provider 是 :class:`NullASRProvider` / :class:`NullOCRProvider`：
    没配任何能力时，每条内容都安静地走同一条路径，不需要调用方做特判。
    """

    def __init__(
        self,
        *,
        downloader: MediaDownloader | None = None,
        asr: ASRProvider | None = None,
        ocr: OCRProvider | None = None,
    ) -> None:
        self._downloader = downloader
        self._asr = asr or NullASRProvider()
        self._ocr = ocr or NullOCRProvider()

    @property
    def asr_name(self) -> str:
        return self._asr.name

    @property
    def ocr_name(self) -> str:
        return self._ocr.name

    async def enrich(
        self,
        raw: RawContent,
        *,
        media_url: str | None = None,
    ) -> tuple[RawContent, tuple[CapabilityAttempt, ...]]:
        """补齐 ``media_path`` / ``transcript`` / ``ocr_text``。

        返回的 ``RawContent`` 是新实例（``RawContent`` 不可变约定：
        有 avoid 的后果，直接 model_copy）。
        """
        attempts: list[CapabilityAttempt] = []
        patch: dict[str, object] = {}

        wanted = _capability_for(raw.media_type)
        if wanted is None:
            # text / article / mixed 这类内容本来就不依赖外部能力，
            # 不该因为「找不到媒体文件」而报一条看起来像失败的记录。
            return raw, (
                CapabilityAttempt(
                    capability="none",
                    provider="none",
                    status="skipped",
                    message=f"media_type={raw.media_type!r} 不需要 ASR 也不需要 OCR",
                ),
            )

        local_path = self._existing_media_path(raw)
        if local_path is None and media_url:
            downloaded, download_attempts = await self._download(raw, media_url)
            attempts.extend(download_attempts)
            if downloaded is not None:
                local_path = downloaded
                patch["media_path"] = str(downloaded)

        if local_path is None:
            # 注意：这里必须**连下载失败的记录一起返回**，不能只回一条
            # 「没有可用媒体文件」—— 那会把真实的失败原因（被平台挡住 / 超限）
            # 悄悄吞掉，看起来和「本来就没给链接」一模一样。
            attempts.append(
                CapabilityAttempt(
                    capability=wanted,
                    provider=_provider_name(self._asr if wanted == "asr" else self._ocr),
                    status="skipped",
                    message="没有可用媒体文件（本地无、下载未成功）",
                )
            )
            return raw, tuple(attempts)

        provider: ASRProvider | OCRProvider = self._asr if wanted == "asr" else self._ocr
        text, attempt = await self._run(wanted, provider, local_path)
        attempts.append(attempt)
        if text is not None:
            patch["transcript" if wanted == "asr" else "ocr_text"] = text

        return raw.model_copy(update=patch), tuple(attempts)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    @staticmethod
    def _existing_media_path(raw: RawContent) -> Path | None:
        if not raw.media_path:
            return None
        path = Path(raw.media_path)
        return path if path.is_file() else None

    async def _download(
        self, raw: RawContent, media_url: str
    ) -> tuple[Path | None, tuple[CapabilityAttempt, ...]]:
        if self._downloader is None:
            return None, (
                CapabilityAttempt(
                    capability="download",
                    provider="none",
                    status="skipped",
                    message="未配置下载器",
                ),
            )

        started = time.perf_counter()
        try:
            media = await self._downloader.download(media_url, media_type=raw.media_type)
        except KnowledgeFlowError as exc:
            log_event(
                LOGGER,
                logging.WARNING,
                stage=STAGE,
                message="媒体下载失败，降级跳过 ASR/OCR",
                error_type=exc.error_type_value,
                error_summary=exc.message,
                media_type=raw.media_type,
            )
            return None, (
                CapabilityAttempt(
                    capability="download",
                    provider="http",
                    status="failed",
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                    error_type=exc.error_type_value,
                    message=exc.message,
                ),
            )
        return media.path, (
            CapabilityAttempt(
                capability="download",
                provider="http",
                status="ok",
                elapsed_ms=(time.perf_counter() - started) * 1000,
                message=f"{media.byte_size} 字节 → {media.path.name}",
            ),
        )

    async def _run(
        self, capability: str, provider: ASRProvider | OCRProvider, path: Path
    ) -> tuple[str | None, CapabilityAttempt]:
        started = time.perf_counter()

        if not provider.is_available():
            return None, CapabilityAttempt(
                capability=capability,
                provider=provider.name,
                status="unavailable",
                message="依赖未安装，已降级跳过（不阻塞 Core Pipeline）",
            )

        try:
            if capability == "asr":
                text = await provider.transcribe(path)  # type: ignore[attr-defined]
            else:
                text = await provider.extract_text(path)  # type: ignore[attr-defined]
        except KnowledgeFlowError as exc:
            log_event(
                LOGGER,
                logging.WARNING,
                stage=STAGE,
                message="可选能力执行失败，降级跳过",
                error_type=exc.error_type_value,
                error_summary=exc.message,
                capability=capability,
                provider=provider.name,
            )
            return None, CapabilityAttempt(
                capability=capability,
                provider=provider.name,
                status="failed",
                elapsed_ms=(time.perf_counter() - started) * 1000,
                error_type=exc.error_type_value,
                message=exc.message,
            )

        # None 表示「这句工具确实用了，但里面没有可识别的文字」—— 不是失败
        return text, CapabilityAttempt(
            capability=capability,
            provider=provider.name,
            status="ok" if text else "skipped",
            text_length=len(text or ""),
            elapsed_ms=(time.perf_counter() - started) * 1000,
            message=None if text else "引擎没返回任何文字",
        )


__all__ = ["ASR_MEDIA", "MediaCapabilityService", "OCR_MEDIA"]
