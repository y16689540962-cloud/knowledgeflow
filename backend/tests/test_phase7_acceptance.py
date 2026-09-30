"""Phase 7 验收：媒体 → transcript/ocr_text → Core Pipeline。

定稿第三十二节的 A 线验收链里没有 ASR/OCR（那是可选能力），
但**「接进来的能力不会污染 A 线」**必须被验证：

* 补了 transcript 的内容能正常跑完 A 线
* transcript 真的进了 Grounding 的源文本（否则转写了等于没转写）
* 能力全部失效时，A 线照样完整通过 —— 这是第二/三十二条的核心要求
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.capabilities import MediaCapabilityService
from app.schemas.content import RawContent


class StaticASR:
    """什么都对，只是返回预设文本。"""

    def __init__(self, text: str) -> None:
        self._text = text

    @property
    def name(self) -> str:
        return "static-asr"

    def is_available(self) -> bool:
        return True

    async def transcribe(self, path: Path) -> str | None:
        return self._text


def make_video(tmp_path: Path, *, transcript: str | None = None) -> RawContent:
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"\x00" * 64)
    return RawContent(
        source="douyin",
        source_id="phase7-video-1",
        source_url="",
        media_type="video",
        title="一段讲解视频",
        author="某UP主",
        raw_text="这是视频简介。",
        media_path=str(media),
        transcript=transcript,
    )


async def test_transcript_reaches_grounding_source_text(tmp_path: Path) -> None:
    """定稿第十节：``raw_text + transcript + ocr_text`` 是统一 source text。"""
    long_text = "视频里明确说了：2024 年全球新能源车销量增长了百分之三十五。"
    raw = make_video(tmp_path)
    enriched, attempts = await MediaCapabilityService(asr=StaticASR(long_text)).enrich(raw)

    assert attempts[-1].ok
    source = enriched.source_text()
    assert long_text in source
    assert raw.raw_text in source


async def test_enriched_content_walks_the_full_pipeline(
    tmp_path: Path, pipeline, vault_root: Path
) -> None:
    """补完能力的 RawContent 必须能被 Pipeline 正常消化。"""
    text = "这段转写里有明确的事实：OpenAI 在 2023 年发布了 GPT-4。"
    raw = make_video(tmp_path)
    enriched, _attempts = await MediaCapabilityService(asr=StaticASR(text)).enrich(raw)

    result = await pipeline.process(enriched)

    assert result.outcome == "completed", result.to_dict()
    assert result.note_path is not None
    # note_path 是相对 vault 的路径，必须接上 vault_root 才能读到文件
    note = (vault_root / result.note_path).read_text(encoding="utf-8")
    assert "## Transcript" in note
    assert text in note


async def test_core_pipeline_is_untouched_when_capabilities_fail(
    tmp_path: Path, pipeline
) -> None:
    """ASR/OCR 全线失效时，A 线照样跑完 —— 第二/三十二条的核心要求。"""
    raw = make_video(tmp_path)
    # 不传任何 provider → 全是 Null，自动降级
    enriched, attempts = await MediaCapabilityService().enrich(raw)

    assert all(not a.ok for a in attempts)
    result = await pipeline.process(enriched)

    assert result.outcome == "completed", result.to_dict()


async def test_capability_failure_does_not_persist_as_content(tmp_path: Path) -> None:
    """降级不能变成「假装成功」：transcript 必须是 None，不是空字符串。"""
    raw = make_video(tmp_path)
    enriched, attempts = await MediaCapabilityService().enrich(raw)

    assert enriched.transcript is None
    assert enriched.transcript != ""
    assert attempts[-1].error_type is None  # 是降级，不是失败
    assert attempts[-1].status == "unavailable"


@pytest.mark.parametrize("media_type", ["video", "audio"])
async def test_audio_and_video_both_route_to_asr(tmp_path: Path, media_type: str) -> None:
    raw = RawContent(
        source="douyin", source_id="1", source_url="", media_type=media_type,  # type: ignore[arg-type]
        raw_text="简介", media_path=str(tmp_path / f"f.{media_type}"),
    )
    Path(raw.media_path or "").write_bytes(b"\x00" * 32)

    _out, attempts = await MediaCapabilityService(asr=StaticASR("说了些什么")).enrich(raw)
    assert attempts[-1].capability == "asr"
