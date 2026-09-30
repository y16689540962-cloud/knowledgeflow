"""``ManualPasteSource``：手动粘贴入口（定稿第二十三条，强制）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.ingestion import ManualPastePayload, ManualPasteSource
from app.ingestion.manual import SOURCE_NAME
from app.normalization import normalize_content
from app.schemas import RawContent


@pytest.fixture
def source() -> ManualPasteSource:
    return ManualPasteSource()


# --------------------------------------------------------------------------- #
# 契约
# --------------------------------------------------------------------------- #
def test_source_name(source: ManualPasteSource) -> None:
    assert source.name == SOURCE_NAME == "manual"


async def test_is_a_content_source(source: ManualPasteSource) -> None:
    from app.ingestion import ContentSource

    assert isinstance(source, ContentSource)


async def test_fetch_returns_raw_content(source: ManualPasteSource) -> None:
    raw = await source.fetch(
        ManualPastePayload(title="标题", author="作者", raw_text="正文", description="简介")
    )
    assert isinstance(raw, RawContent)
    assert raw.source == "manual"
    assert raw.title == "标题"
    assert raw.author == "作者"
    assert raw.description == "简介"
    assert raw.raw_text == "正文"
    assert raw.media_type == "text"
    assert raw.media_path is None


async def test_source_id_is_left_empty_for_fallback(source: ManualPasteSource) -> None:
    """第二十三条：``source_id`` 走 ``hash:`` fallback —— 由 normalize 阶段填。"""
    raw = await source.fetch(ManualPastePayload(title="标题", raw_text="正文"))
    assert raw.source_id == ""

    normalized = normalize_content(raw)
    assert normalized.source_id.startswith("hash:")
    assert normalized.needs_manual_review is True
    assert normalized.error_type == "SOURCE_ID_RESOLUTION_FAILED"


async def test_source_url_is_kept_but_not_used_for_dedup(source: ManualPasteSource) -> None:
    """链接存进 ``source_url``，但**不参与去重**（不在 content_hash 白名单里）。"""
    first = await source.fetch(
        ManualPastePayload(
            title="同一段话", raw_text="正文", source_url="https://v.douyin.com/aaa"
        )
    )
    second = await source.fetch(
        ManualPastePayload(
            title="同一段话", raw_text="正文", source_url="https://v.douyin.com/bbb"
        )
    )
    assert first.source_url != second.source_url

    one = normalize_content(first)
    two = normalize_content(second)
    assert one.content_hash == two.content_hash
    assert one.source_id == two.source_id


async def test_no_network_needed(source: ManualPasteSource) -> None:
    """不需要网络、不需要登录态、不需要解析。"""
    import inspect

    module_source = open(
        inspect.getsourcefile(ManualPasteSource) or "", encoding="utf-8"
    ).read()
    for banned in ("httpx", "requests", "aiohttp", "socket", "urllib.request"):
        assert banned not in module_source, f"manual.py 引用了 {banned}"


# --------------------------------------------------------------------------- #
# payload
# --------------------------------------------------------------------------- #
def test_payload_all_optional() -> None:
    payload = ManualPastePayload()
    assert payload.title is None
    assert payload.source_url == ""
    assert payload.media_type == "text"
    assert payload.is_empty() is True


def test_payload_is_empty_accounts_for_text() -> None:
    assert ManualPastePayload(raw_text="有内容").is_empty() is False
    assert ManualPastePayload(transcript="有内容").is_empty() is False
    assert ManualPastePayload(ocr_text="有内容").is_empty() is False
    assert ManualPastePayload(title="只有标题").is_empty() is False
    assert ManualPastePayload(description="只有简介").is_empty() is False


def test_payload_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ManualPastePayload(raw_txt="打错了字段名")  # type: ignore[call-arg]


def test_payload_media_type_is_validated() -> None:
    assert ManualPastePayload(media_type="article").media_type == "article"
    with pytest.raises(ValidationError):
        ManualPastePayload(media_type="podcast")  # type: ignore[arg-type]


def test_payload_source_url_none_becomes_empty() -> None:
    assert ManualPastePayload(source_url=None).source_url == ""  # type: ignore[arg-type]


async def test_fetch_accepts_all_media_types(source: ManualPasteSource) -> None:
    for media_type in ("video", "image", "article", "audio", "text", "mixed"):
        raw = await source.fetch(ManualPastePayload(raw_text="x", media_type=media_type))  # type: ignore[arg-type]
        assert raw.media_type == media_type


async def test_empty_paste_yields_empty_raw_content(source: ManualPasteSource) -> None:
    """一个字都没有 → 交给 Pipeline 报 EMPTY_SOURCE_TEXT（这里不抛错）。"""
    raw = await source.fetch(ManualPastePayload())
    assert raw.source_text() == ""


async def test_full_media_metadata(source: ManualPasteSource) -> None:
    raw = await source.fetch(
        ManualPastePayload(
            title="t",
            author="a",
            author_id="uid-1",
            description="d",
            raw_text="正文",
            transcript="逐字稿",
            ocr_text="图片文字",
            media_type="video",
        )
    )
    assert raw.author_id == "uid-1"
    assert raw.source_text() == "正文\n逐字稿\n图片文字"
