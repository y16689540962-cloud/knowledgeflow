"""``RawContent`` schema。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas import MEDIA_TYPES, RawContent


def test_minimal_valid_raw_content() -> None:
    raw = RawContent(
        source="manual",
        source_id="",
        source_url="",
        media_type="text",
    )
    assert raw.title is None
    assert raw.transcript is None
    assert raw.source_text() == ""


def test_source_id_and_source_url_accept_empty_string() -> None:
    """ManualPaste 无链接、source_id 待 normalize 阶段 fallback —— 允许空串但禁止 None。"""
    raw = RawContent(source="manual", source_id="", source_url="", media_type="text")
    assert raw.source_id == ""
    assert raw.source_url == ""


def test_required_fields() -> None:
    with pytest.raises(ValidationError) as excinfo:
        RawContent(source="manual", media_type="text")  # type: ignore[call-arg]
    missing = {err["loc"][0] for err in excinfo.value.errors() if err["type"] == "missing"}
    assert missing == {"source_id", "source_url"}


def test_blank_source_rejected() -> None:
    with pytest.raises(ValidationError):
        RawContent(source="   ", source_id="x", source_url="", media_type="text")


def test_source_is_trimmed() -> None:
    raw = RawContent(source="  douyin  ", source_id="1", source_url="", media_type="video")
    assert raw.source == "douyin"


@pytest.mark.parametrize("media_type", MEDIA_TYPES)
def test_all_media_types_accepted(media_type: str) -> None:
    raw = RawContent(source="s", source_id="1", source_url="", media_type=media_type)  # type: ignore[arg-type]
    assert raw.media_type == media_type


def test_unknown_media_type_rejected() -> None:
    with pytest.raises(ValidationError):
        RawContent(source="s", source_id="1", source_url="", media_type="podcast")  # type: ignore[arg-type]


def test_unknown_field_rejected() -> None:
    """RawContent 的字段集是契约，不允许偷偷塞平台私有字段。"""
    with pytest.raises(ValidationError):
        RawContent(
            source="s",
            source_id="1",
            source_url="",
            media_type="text",
            douyin_cookie="oops",  # type: ignore[call-arg]
        )


def test_source_text_concatenates_available_fields() -> None:
    raw = RawContent(
        source="douyin",
        source_id="1",
        source_url="https://www.douyin.com/video/1",
        media_type="video",
        raw_text="原文",
        transcript="逐字稿",
        ocr_text="图片文字",
    )
    assert raw.source_text() == "原文\n逐字稿\n图片文字"


def test_source_text_skips_empty_fields() -> None:
    raw = RawContent(
        source="douyin",
        source_id="1",
        source_url="",
        media_type="video",
        raw_text="原文",
        transcript=None,
        ocr_text="",
    )
    assert raw.source_text() == "原文"


def test_hash_inputs_only_whitelisted_fields() -> None:
    raw = RawContent(
        source="douyin",
        source_id="1",
        source_url="https://example.com",
        media_type="video",
        title="标题",
        author="作者",
        description="描述",
        transcript="逐字稿",
    )
    assert raw.hash_inputs() == {
        "source": "douyin",
        "title": "标题",
        "author": "作者",
        "description": "描述",
    }


def test_from_fixture(raw_payload: dict) -> None:
    raw = RawContent.from_fixture(raw_payload)
    assert raw.source == "douyin"
    assert raw.media_type == "video"
    assert raw.transcript


def test_long_fixture_is_long_enough(long_raw_content: RawContent) -> None:
    assert len(long_raw_content.transcript or "") > 12000
