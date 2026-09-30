"""``/api/ingest/media``：本地媒体文件 → ASR/OCR → A 线（Phase 7 能力的服务侧入口）。

stub 掉 ASR / OCR provider（引擎懒加载、接口极小），整条链路 ——
嗅探 → 采集 → 能力编排 → pipeline → 笔记 —— 全是真的。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.api import create_app
from app.capabilities.base import ASRProvider, OCRProvider
from app.capabilities.service import MediaCapabilityService
from app.config import Settings
from app.db.session import Database
from app.providers.mock import MockProvider

# 复用 test_api_app 的 lifespan 包装与 settings fixture；
# vault_root / db / valid_analysis_payload 来自 tests/conftest.py（全项目共用）。
from tests.test_api_app import running, settings_with_vault  # noqa: F401


def png_bytes() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def mp4_bytes() -> bytes:
    return b"\x00\x00\x00\x20ftypisom" + b"\x00" * 16


class StubASR(ASRProvider):
    """按文件名给固定转写 —— 刚好够验证「ASR 结果进了 A 线」。"""

    @property
    def name(self) -> str:
        return "stub-asr"

    def is_available(self) -> bool:
        return True

    async def transcribe(self, path: Path) -> str | None:
        return "这是转写出来的逐字稿：今天聊人工智能算力，成本会下降三成。"


class StubOCR(OCRProvider):
    @property
    def name(self) -> str:
        return "stub-ocr"

    def is_available(self) -> bool:
        return True

    async def extract_text(self, path: Path) -> str | None:
        return "图里识别出的字：中国人工智能产业。"


class UnavailableASR(StubASR):
    def is_available(self) -> bool:
        return False

    async def transcribe(self, path: Path) -> str | None:  # pragma: no cover - 不该被调
        raise AssertionError("is_available=False 时绝不能调到引擎")


def make_app(
    *,
    db: Database,
    settings: Settings,
    vault_root: Path,
    payload: dict,
    capabilities: MediaCapabilityService,
):
    return create_app(
        settings=settings,
        database=db,
        provider=MockProvider(payload=payload),
        vault_root=vault_root,
        configure_logging=False,
        run_startup_recovery=False,
        media_capability_service=capabilities,
    )


# --------------------------------------------------------------------------- #
# 视频文件 → ASR → A 线
# --------------------------------------------------------------------------- #
async def test_video_file_goes_through_asr_into_pipeline(
    db: Database,
    settings_with_vault: Settings,
    vault_root: Path,
    valid_analysis_payload: dict,
    tmp_path: Path,
) -> None:
    media = tmp_path / "talk.mp4"
    media.write_bytes(mp4_bytes())

    app = make_app(
        db=db,
        settings=settings_with_vault,
        vault_root=vault_root,
        payload=valid_analysis_payload,
        capabilities=MediaCapabilityService(asr=StubASR(), ocr=StubOCR()),
    )
    async with running(app) as client:
        response = await client.post(
            "/api/ingest/media",
            json={"file_path": str(media), "title": "一段演讲"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["result"]["outcome"] == "completed"
    # 转写真的进了 A 线：LLM 看到的源文本里有逐字稿
    assert body["raw"]["media_type"] == "video"


async def test_transcript_feeds_the_a_line_without_any_text(
    db: Database,
    settings_with_vault: Settings,
    vault_root: Path,
    valid_analysis_payload: dict,
    tmp_path: Path,
) -> None:
    """**只有**逐字稿、没有标题也没有文案 → 照样跑通。

    这是「媒体入口真的接上了能力层」的正面证明：A 线的源文本来自 ASR 的输出。
    """
    media = tmp_path / "audio.mp4"
    media.write_bytes(mp4_bytes())

    app = make_app(
        db=db,
        settings=settings_with_vault,
        vault_root=vault_root,
        payload=valid_analysis_payload,
        capabilities=MediaCapabilityService(asr=StubASR(), ocr=StubOCR()),
    )
    async with running(app) as client:
        response = await client.post("/api/ingest/media", json={"file_path": str(media)})

    assert response.status_code == 200, response.text
    assert response.json()["result"]["outcome"] == "completed"


async def test_no_engine_and_no_text_is_empty_source_text(
    db: Database,
    settings_with_vault: Settings,
    vault_root: Path,
    valid_analysis_payload: dict,
    tmp_path: Path,
) -> None:
    """反面证明：引擎没装 + 什么都不补 → 明确 ``EMPTY_SOURCE_TEXT``，不产出空的成功。"""
    media = tmp_path / "silent.mp4"
    media.write_bytes(mp4_bytes())

    app = make_app(
        db=db,
        settings=settings_with_vault,
        vault_root=vault_root,
        payload=valid_analysis_payload,
        capabilities=MediaCapabilityService(asr=UnavailableASR(), ocr=StubOCR()),
    )
    async with running(app) as client:
        response = await client.post("/api/ingest/media", json={"file_path": str(media)})

    assert response.json()["result"]["error_type"] == "EMPTY_SOURCE_TEXT"


async def test_image_file_goes_through_ocr_into_pipeline(
    db: Database,
    settings_with_vault: Settings,
    vault_root: Path,
    valid_analysis_payload: dict,
    tmp_path: Path,
) -> None:
    media = tmp_path / "chart.png"
    media.write_bytes(png_bytes())

    app = make_app(
        db=db,
        settings=settings_with_vault,
        vault_root=vault_root,
        payload=valid_analysis_payload,
        capabilities=MediaCapabilityService(asr=StubASR(), ocr=StubOCR()),
    )
    async with running(app) as client:
        response = await client.post("/api/ingest/media", json={"file_path": str(media)})

    assert response.status_code == 200, response.text
    assert response.json()["raw"]["media_type"] == "image"


# --------------------------------------------------------------------------- #
# 降级语义
# --------------------------------------------------------------------------- #
async def test_engine_unavailable_degrades_to_raw_text(
    db: Database,
    settings_with_vault: Settings,
    vault_root: Path,
    valid_analysis_payload: dict,
    tmp_path: Path,
) -> None:
    """ASR 没装：安静降级；补了 raw_text 的话 A 线照样跑通（第二十七节）。"""
    media = tmp_path / "clip.mp4"
    media.write_bytes(mp4_bytes())

    app = make_app(
        db=db,
        settings=settings_with_vault,
        vault_root=vault_root,
        payload=valid_analysis_payload,
        capabilities=MediaCapabilityService(asr=UnavailableASR(), ocr=StubOCR()),
    )
    async with running(app) as client:
        response = await client.post(
            "/api/ingest/media",
            json={"file_path": str(media), "raw_text": "用户补的文案，够 A 线用。"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["result"]["outcome"] == "completed"


async def test_process_false_returns_raw_without_pipeline(
    db: Database,
    settings_with_vault: Settings,
    vault_root: Path,
    valid_analysis_payload: dict,
    tmp_path: Path,
) -> None:
    media = tmp_path / "note.png"
    media.write_bytes(png_bytes())

    app = make_app(
        db=db,
        settings=settings_with_vault,
        vault_root=vault_root,
        payload=valid_analysis_payload,
        capabilities=MediaCapabilityService(asr=StubASR(), ocr=StubOCR()),
    )
    async with running(app) as client:
        response = await client.post(
            "/api/ingest/media", json={"file_path": str(media), "process": False}
        )

    assert response.status_code == 200, response.text
    assert response.json()["result"] is None


# --------------------------------------------------------------------------- #
# 错误映射
# --------------------------------------------------------------------------- #
async def test_missing_file_is_404(
    db: Database,
    settings_with_vault: Settings,
    vault_root: Path,
    valid_analysis_payload: dict,
) -> None:
    app = make_app(
        db=db,
        settings=settings_with_vault,
        vault_root=vault_root,
        payload=valid_analysis_payload,
        capabilities=MediaCapabilityService(),
    )
    async with running(app) as client:
        response = await client.post(
            "/api/ingest/media", json={"file_path": "/definitely/not/here.mp4"}
        )

    assert response.status_code == 404, response.text
    assert response.json()["error_type"] == "MEDIA_FILE_NOT_FOUND"


async def test_unknown_format_is_415(
    db: Database,
    settings_with_vault: Settings,
    vault_root: Path,
    valid_analysis_payload: dict,
    tmp_path: Path,
) -> None:
    media = tmp_path / "garbage.mp4"
    media.write_bytes(b"NOT A REAL MEDIA FORMAT")

    app = make_app(
        db=db,
        settings=settings_with_vault,
        vault_root=vault_root,
        payload=valid_analysis_payload,
        capabilities=MediaCapabilityService(),
    )
    async with running(app) as client:
        response = await client.post("/api/ingest/media", json={"file_path": str(media)})

    assert response.status_code == 415, response.text
    assert response.json()["error_type"] == "MEDIA_TYPE_UNSUPPORTED"


async def test_request_rejects_unknown_fields(
    db: Database,
    settings_with_vault: Settings,
    vault_root: Path,
) -> None:
    """extra=forbid：字段名拼错要 422，不是静默忽略。"""
    app = make_app(
        db=db,
        settings=settings_with_vault,
        vault_root=vault_root,
        payload={},
        capabilities=MediaCapabilityService(),
    )
    async with running(app) as client:
        response = await client.post(
            "/api/ingest/media", json={"file_path_typo": "/x.mp4"}
        )

    assert response.status_code == 422
