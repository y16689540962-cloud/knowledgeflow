"""``MediaCapabilityService``（Phase 7：ASR / OCR）。

这里真正要钉死的是**降级语义** —— 定稿第二条原则：
「不因为 ASR/OCR 环境问题阻塞 Core Pipeline」。

但「降级」很容易被写成「假装成功」，所以要同时钉死另一半：
**每一次跳过 / 不可用 / 失败，都必须在流水里留下 ``error_type`` 和原因。**
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.capabilities import MediaCapabilityService, NullASRProvider, NullOCRProvider
from app.capabilities.base import CapabilityAttempt
from app.capabilities.null import NULL_PROVIDER_NAME
from app.capabilities.service import ASR_MEDIA, OCR_MEDIA
from app.errors import CapabilityFailedError
from app.media import DownloadedMedia, HttpMediaDownloader
from app.schemas.content import RawContent

TRANSCRIPT = "这是一段视频的转写内容。"
OCR_TEXT = "画面上的文字"


class FakeASR:
    """测试替身：返回预设文本，并记录收到的路径。"""

    def __init__(self, text: str | None = TRANSCRIPT, *, raises: Exception | None = None) -> None:
        self._text = text
        self._raises = raises
        self.seen: list[Path] = []

    @property
    def name(self) -> str:
        return "fake-asr"

    def is_available(self) -> bool:
        return True

    async def transcribe(self, path: Path) -> str | None:
        self.seen.append(path)
        if self._raises is not None:
            raise self._raises
        return self._text


class FakeOCR:
    def __init__(self, text: str | None = OCR_TEXT) -> None:
        self._text = text

    @property
    def name(self) -> str:
        return "fake-ocr"

    def is_available(self) -> bool:
        return True

    async def extract_text(self, path: Path) -> str | None:
        return self._text


class BrokenASR(FakeASR):
    """engine 装了，但调用失败。"""

    def __init__(self) -> None:
        super().__init__(raises=CapabilityFailedError("模型加载失败"))

    @property
    def name(self) -> str:
        return "broken-asr"


def media_file(tmp_path: Path, name: str = "clip.mp4") -> Path:
    path = tmp_path / name
    path.write_bytes(b"\x00" * 64)
    return path


def video(tmp_path: Path) -> RawContent:
    return RawContent(
        source="douyin",
        source_id="123",
        source_url="",
        media_type="video",
        raw_text="原始文案",
        media_path=str(media_file(tmp_path)),
    )


# --------------------------------------------------------------------------- #
# 降级：没配能力时不阻塞
# --------------------------------------------------------------------------- #
async def test_default_service_has_no_capabilities() -> None:
    service = MediaCapabilityService()
    assert service.asr_name == NULL_PROVIDER_NAME
    assert service.ocr_name == NULL_PROVIDER_NAME


async def test_null_providers_are_not_available() -> None:
    assert NullASRProvider().is_available() is False
    assert NullOCRProvider().is_available() is False


@pytest.mark.parametrize("media_type", ["text", "article", "mixed"])
async def test_non_media_content_is_left_untouched(media_type: str) -> None:
    """text / article / mixed 不需要外部能力 —— 不该报「找不到媒体文件」。"""
    raw = RawContent(source="manual", source_id="1", source_url="", media_type=media_type, raw_text="正文")  # type: ignore[arg-type]
    out, attempts = await MediaCapabilityService().enrich(raw)

    assert out is raw
    assert len(attempts) == 1
    assert attempts[0].status == "skipped"
    assert "不需要 ASR 也不需要 OCR" in (attempts[0].message or "")


async def test_missing_file_skips_without_error() -> None:
    raw = RawContent(
        source="douyin", source_id="1", source_url="", media_type="video",
        raw_text="文案", media_path="/definitely/not/here.mp4",
    )
    out, attempts = await MediaCapabilityService().enrich(raw)

    assert out.transcript is None
    assert attempts[0].status == "skipped"
    assert attempts[0].error_type is None  # 不是失败，是「没的做」


async def test_no_file_and_no_url_skips(tmp_path: Path) -> None:
    raw = RawContent(source="douyin", source_id="1", source_url="", media_type="video", raw_text="文案")
    _out, attempts = await MediaCapabilityService().enrich(raw)
    assert attempts[0].status == "skipped"
    assert "没有可用媒体文件" in (attempts[0].message or "")


async def test_unavailable_engine_degrades_instead_of_raising(tmp_path: Path) -> None:
    """引擎没装 → 降级，但**如实记录** unavailable。"""
    _out, attempts = await MediaCapabilityService().enrich(video(tmp_path))

    record = attempts[-1]
    assert record.status == "unavailable"
    assert record.capability == "asr"
    assert record.provider == NULL_PROVIDER_NAME
    assert record.error_type is None
    assert "不阻塞 Core Pipeline" in (record.message or "")


# --------------------------------------------------------------------------- #
# 成功路径
# --------------------------------------------------------------------------- #
async def test_asr_fills_transcript(tmp_path: Path) -> None:
    asr = FakeASR()
    raw = video(tmp_path)
    out, attempts = await MediaCapabilityService(asr=asr).enrich(raw)

    assert out.transcript == TRANSCRIPT
    assert asr.seen == [Path(raw.media_path or "")]
    # 原始实例不被改动
    assert raw.transcript is None
    record = attempts[-1]
    assert record.ok
    assert record.text_length == len(TRANSCRIPT)


async def test_ocr_fills_ocr_text(tmp_path: Path) -> None:
    path = media_file(tmp_path, "pic.png")
    raw = RawContent(
        source="douyin", source_id="1", source_url="", media_type="image",
        raw_text=None, media_path=str(path),
    )
    out, attempts = await MediaCapabilityService(ocr=FakeOCR()).enrich(raw)

    assert out.ocr_text == OCR_TEXT
    assert attempts[-1].capability == "ocr"
    assert attempts[-1].ok


async def test_enriched_content_has_more_source_text(tmp_path: Path) -> None:
    """转写结果真的进到了 Grounding 的源文本里。"""
    raw = video(tmp_path)
    out, _ = await MediaCapabilityService(asr=FakeASR()).enrich(raw)

    assert TRANSCRIPT in out.source_text()
    assert len(out.source_text()) > len(raw.source_text())


async def test_audio_uses_asr_and_image_uses_ocr() -> None:
    assert "audio" in ASR_MEDIA and "video" in ASR_MEDIA
    assert "image" in OCR_MEDIA
    assert ASR_MEDIA.isdisjoint(OCR_MEDIA)


# --------------------------------------------------------------------------- #
# 失败路径：记录 error_type，但不抛给调用方
# --------------------------------------------------------------------------- #
async def test_engine_failure_is_recorded_not_raised(tmp_path: Path) -> None:
    """「有能力但出错」必须留下 error_type —— 降级不能变成静默。"""
    raw = video(tmp_path)
    out, attempts = await MediaCapabilityService(asr=BrokenASR()).enrich(raw)

    assert out.transcript is None
    record = attempts[-1]
    assert record.status == "failed"
    assert record.error_type == "CAPABILITY_FAILED"
    assert "模型加载失败" in (record.message or "")


async def test_engine_returning_nothing_is_not_a_failure(tmp_path: Path) -> None:
    """引擎跑完了但内容里没字 —— 是 skipped，不是 failed。"""
    _out, attempts = await MediaCapabilityService(asr=FakeASR(text=None)).enrich(video(tmp_path))
    record = attempts[-1]

    assert record.status == "skipped"
    assert record.error_type is None
    assert record.text_length == 0


# --------------------------------------------------------------------------- #
# 与下载器联动
# --------------------------------------------------------------------------- #
class StubDownloader:
    def __init__(self, result: DownloadedMedia | Exception) -> None:
        self._result = result
        self.calls: list[tuple[str, str]] = []

    async def download(self, url: str, *, media_type: str) -> DownloadedMedia:  # type: ignore[override]
        self.calls.append((url, media_type))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


async def test_media_url_is_downloaded_then_transcribed(tmp_path: Path) -> None:
    target = media_file(tmp_path, "downloaded.mp4")
    downloaded = DownloadedMedia(
        path=target, media_type="video", byte_size=64,
        content_type="video/mp4", source_url="https://cdn.example.com/v.mp4",
    )
    downloader = StubDownloader(downloaded)
    raw = RawContent(source="douyin", source_id="1", source_url="", media_type="video", raw_text="文案")

    out, attempts = await MediaCapabilityService(
        downloader=downloader, asr=FakeASR()  # type: ignore[arg-type]
    ).enrich(raw, media_url="https://cdn.example.com/v.mp4")

    assert downloader.calls == [("https://cdn.example.com/v.mp4", "video")]
    assert out.media_path == str(target)
    assert out.transcript == TRANSCRIPT
    assert attempts[0].capability == "download" and attempts[0].ok


async def test_download_failure_is_recorded_and_content_still_usable(
    tmp_path: Path,
) -> None:
    from app.errors import MediaDownloadError

    downloader = StubDownloader(MediaDownloadError("被平台挡住了"))
    raw = RawContent(source="douyin", source_id="1", source_url="", media_type="video", raw_text="文案")

    out, attempts = await MediaCapabilityService(
        downloader=downloader, asr=FakeASR()  # type: ignore[arg-type]
    ).enrich(raw, media_url="https://cdn.example.com/v.mp4")

    assert out.transcript is None
    record = attempts[0]
    assert record.capability == "download"
    assert record.status == "failed"
    assert record.error_type == "MEDIA_DOWNLOAD_FAILED"
    # 内容本身照样能用 —— 只是没有 transcript
    assert out.source_text() == "文案"


def test_capability_attempt_serializes() -> None:
    attempt = CapabilityAttempt(
        capability="asr", provider="fake", status="ok", text_length=12, elapsed_ms=1.5
    )
    payload = attempt.to_dict()
    assert payload["capability"] == "asr"
    assert payload["status"] == "ok"
    assert attempt.ok is True


def test_real_providers_report_their_own_availability() -> None:
    """没装引擎时，真实 provider 必须自己承认不可用（不是等到调用才崩）。"""
    from app.capabilities.tesseract import TesseractOCR
    from app.capabilities.whisper import FasterWhisperASR

    for provider in (FasterWhisperASR(), TesseractOCR()):
        available = provider.is_available()
        assert isinstance(available, bool)
        assert provider.name.startswith(("faster-whisper", "tesseract"))


def test_http_downloader_implements_the_protocol() -> None:
    """下载器可以按操作互换 —— 这是 service 只依赖协议的理由。"""
    downloader = HttpMediaDownloader(Path("/tmp/kf-not-used"))
    assert callable(downloader.download)
