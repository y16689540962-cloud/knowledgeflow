"""source_id 规范与 fallback（第五节，强制）。"""

from __future__ import annotations

import pytest

from app.errors import ErrorType
from app.normalization import (
    HASH_FALLBACK_PREFIX,
    fallback_source_id,
    looks_like_url,
    normalize_content,
    resolve_source_id,
)
from app.schemas import RawContent

DIGEST = "a" * 64


def raw(**overrides: object) -> RawContent:
    payload = {
        "source": "douyin",
        "source_id": "7321567890123456789",
        "source_url": "https://www.douyin.com/video/7321567890123456789",
        "title": "标题",
        "author": "作者",
        "description": "描述",
        "media_type": "video",
    }
    payload.update(overrides)
    return RawContent.from_fixture(payload)


# --------------------------------------------------------------------------- #
# fallback 形态
# --------------------------------------------------------------------------- #
def test_fallback_format() -> None:
    assert fallback_source_id(DIGEST) == "hash:aaaaaaaaaaaaaaaa"


def test_fallback_prefix_constant() -> None:
    assert HASH_FALLBACK_PREFIX == "hash:"
    assert fallback_source_id(DIGEST).startswith(HASH_FALLBACK_PREFIX)


def test_fallback_requires_long_enough_hash() -> None:
    with pytest.raises(ValueError):
        fallback_source_id("short")


# --------------------------------------------------------------------------- #
# 正常解析
# --------------------------------------------------------------------------- #
def test_valid_source_id_kept() -> None:
    resolution = resolve_source_id("7321567890123456789", content_hash=DIGEST)
    assert resolution.source_id == "7321567890123456789"
    assert resolution.needs_manual_review is False
    assert resolution.error_type is None
    assert resolution.is_fallback is False


def test_source_id_is_trimmed() -> None:
    assert resolve_source_id("  12345  ", content_hash=DIGEST).source_id == "12345"


# --------------------------------------------------------------------------- #
# fallback 触发条件
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", [None, "", "   ", "\n\t"])
def test_blank_source_id_falls_back(value: str | None) -> None:
    resolution = resolve_source_id(value, content_hash=DIGEST)
    assert resolution.source_id == "hash:aaaaaaaaaaaaaaaa"
    assert resolution.needs_manual_review is True
    assert resolution.error_type == ErrorType.SOURCE_ID_RESOLUTION_FAILED.value


@pytest.mark.parametrize(
    "value",
    [
        "https://v.douyin.com/xxxx",
        "http://www.douyin.com/video/123",
        "  www.douyin.com/video/123  ",
    ],
)
def test_url_is_never_used_as_source_id(value: str) -> None:
    """禁止用 source_url 去重：短链每次分享都可能不同。"""
    resolution = resolve_source_id(value, content_hash=DIGEST)
    assert resolution.source_id.startswith(HASH_FALLBACK_PREFIX)
    assert "http" not in resolution.source_id
    assert resolution.needs_manual_review is True
    assert resolution.error_type == ErrorType.SOURCE_ID_RESOLUTION_FAILED.value


def test_looks_like_url() -> None:
    assert looks_like_url("https://a.com")
    assert looks_like_url("HTTP://A.COM")
    assert looks_like_url("www.a.com")
    assert not looks_like_url("7321567890123456789")
    assert not looks_like_url("hash:aaaaaaaaaaaaaaaa")


# --------------------------------------------------------------------------- #
# 不变量：永不为空 / 永不回退成 URL
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value",
    [None, "", " ", "https://v.douyin.com/a", "www.douyin.com/x", "aweme:123", "12345", "\u200b"],
)
def test_source_id_invariants(value: str | None) -> None:
    resolution = resolve_source_id(value, content_hash=DIGEST)
    assert resolution.source_id.strip() != ""
    assert not resolution.source_id.lower().startswith(("http://", "https://"))


# --------------------------------------------------------------------------- #
# 端到端（normalize_content）
# --------------------------------------------------------------------------- #
def test_normalize_keeps_aweme_id() -> None:
    normalized = normalize_content(raw())
    assert normalized.source_id == "7321567890123456789"
    assert normalized.needs_manual_review is False
    assert normalized.error_type is None


def test_normalize_falls_back_for_manual_paste() -> None:
    normalized = normalize_content(raw(source="manual", source_id="", source_url=""))
    assert normalized.source_id == fallback_source_id(normalized.content_hash)
    assert normalized.needs_manual_review is True
    assert normalized.error_type == "SOURCE_ID_RESOLUTION_FAILED"


def test_normalize_fallback_is_deterministic() -> None:
    first = normalize_content(raw(source="manual", source_id=""))
    second = normalize_content(raw(source="manual", source_id=""))
    assert first.source_id == second.source_id


def test_fallback_ignores_mutating_source_url() -> None:
    """短链会变，但 fallback 只吃 content_hash → 仍然稳定。"""
    first = normalize_content(raw(source="manual", source_id="", source_url="https://v.douyin.com/a"))
    second = normalize_content(raw(source="manual", source_id="", source_url="https://v.douyin.com/b"))
    assert first.source_id == second.source_id
