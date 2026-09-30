"""URL 基础校验（第六节的 Phase 1 范围）。"""

from __future__ import annotations

import pytest

from app.errors import ErrorType, URLValidationError
from app.normalization import (
    ALLOWED_SCHEMES,
    DEFAULT_ALLOWED_HOSTS,
    extract_first_url,
    host_allowed,
    is_valid_url,
    validate_url,
)


def test_default_whitelist_contains_douyin() -> None:
    assert "douyin.com" in DEFAULT_ALLOWED_HOSTS
    assert "iesdouyin.com" in DEFAULT_ALLOWED_HOSTS


def test_allowed_schemes() -> None:
    assert set(ALLOWED_SCHEMES) == {"http", "https"}


def test_valid_douyin_url() -> None:
    result = validate_url("https://www.douyin.com/video/7321567890123456789")
    assert result.scheme == "https"
    assert result.host == "www.douyin.com"
    assert result.path == "/video/7321567890123456789"


def test_short_link_domain_allowed() -> None:
    assert validate_url("https://v.douyin.com/abcdefg/").host == "v.douyin.com"


def test_host_is_lowercased() -> None:
    assert validate_url("HTTPS://WWW.DOUYIN.COM/video/1").host == "www.douyin.com"


def test_query_is_preserved() -> None:
    result = validate_url("https://www.douyin.com/video/1?modal_id=2")
    assert result.query == "modal_id=2"
    assert result.normalized == "https://www.douyin.com/video/1?modal_id=2"


@pytest.mark.parametrize(
    "url",
    [
        "ftp://www.douyin.com/video/1",
        "javascript:alert(1)",
        "file:///etc/passwd",
    ],
)
def test_unsupported_scheme(url: str) -> None:
    with pytest.raises(URLValidationError) as excinfo:
        validate_url(url)
    assert excinfo.value.error_type is ErrorType.UNSUPPORTED_URL_SCHEME


@pytest.mark.parametrize("url", ["", "   ", "not a url", "douyin.com/video/1", "https://"])
def test_invalid_url(url: str) -> None:
    with pytest.raises(URLValidationError) as excinfo:
        validate_url(url)
    assert excinfo.value.error_type is ErrorType.INVALID_URL


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.com/video/1",
        "https://douyin.com.evil.com/video/1",
        "https://notdouyin.com/video/1",
    ],
)
def test_domain_not_allowed(url: str) -> None:
    with pytest.raises(URLValidationError) as excinfo:
        validate_url(url)
    assert excinfo.value.error_type is ErrorType.DOMAIN_NOT_ALLOWED


def test_lookalike_suffix_is_rejected() -> None:
    """``douyin.com.evil.com`` 是合法域名，但不能被后缀白名单放行。"""
    assert not host_allowed("douyin.com.evil.com", DEFAULT_ALLOWED_HOSTS)
    assert host_allowed("sub.douyin.com", DEFAULT_ALLOWED_HOSTS)
    assert host_allowed("douyin.com", DEFAULT_ALLOWED_HOSTS)


def test_custom_allowlist() -> None:
    result = validate_url("https://example.org/a", allowed_hosts=("example.org",))
    assert result.host == "example.org"
    with pytest.raises(URLValidationError):
        validate_url("https://www.douyin.com/video/1", allowed_hosts=("example.org",))


def test_is_valid_url() -> None:
    assert is_valid_url("https://www.douyin.com/video/1")
    assert not is_valid_url("https://evil.com/")
    assert not is_valid_url("ftp://www.douyin.com/")


# --------------------------------------------------------------------------- #
# 从抖音分享文案里抠链接（2026-09-30）
# --------------------------------------------------------------------------- #
#: 真实的抖音分享文案形态：口令 + 视频文案 + 链接 + 引导语，全在一行里。
DOUYIN_SHARE_TEXT = (
    "2.05 08/27 CHv:/ U@Y.Zz :2pm 一百元在南京夜市都能吃到啥？今天又爽吃了！！"
    "# 日常vlog # 夜市 # 情侣 # 路边摊美味 https://v.douyin.com/_ySGdmh6RyM/ "
    "复制此链接，打开Dou音搜索，直接观看视频！"
)


def test_extract_url_from_douyin_share_text() -> None:
    """最常见也最容易踩的一条：用户粘的**不是**链接，是整段分享文案。"""
    assert extract_first_url(DOUYIN_SHARE_TEXT) == "https://v.douyin.com/_ySGdmh6RyM/"


def test_extraction_stops_at_chinese() -> None:
    """链接后面紧跟中文时不能吞进来 —— 中文必须 percent-encoded 才算 URL。"""
    assert extract_first_url("打开Dou音搜索 https://v.douyin.com/abc/复制此链接") == (
        "https://v.douyin.com/abc/"
    )


@pytest.mark.parametrize(
    "text",
    ["", "   ", "完全没有链接的一段话", "www.douyin.com/video/1", "ftp://x.com/a"],
)
def test_extract_url_returns_none(text: str) -> None:
    """抠不到就返回 ``None`` —— 调用方会如实报错，绝不猜一个出来。"""
    assert extract_first_url(text) is None


def test_trailing_punctuation_is_not_part_of_the_url() -> None:
    assert extract_first_url("看这个 https://v.douyin.com/abc/。") == "https://v.douyin.com/abc/"


def test_first_url_wins() -> None:
    """有多个链接时只取第一个 —— 这是刻意的保守选择，不猜「用户想要哪个」。"""
    assert (
        extract_first_url("https://v.douyin.com/x/，还有 https://v.douyin.com/y/")
        == "https://v.douyin.com/x/"
    )


def test_extracted_url_still_has_to_pass_the_whitelist() -> None:
    """提取**不削弱**任何校验：抠出来的链接照样要过 scheme + 域名白名单。"""
    text = "随便一段话 https://evil.com/video/1 结束"
    assert extract_first_url(text) == "https://evil.com/video/1"
    with pytest.raises(URLValidationError) as excinfo:
        validate_url(extract_first_url(text) or "")
    assert excinfo.value.error_type is ErrorType.DOMAIN_NOT_ALLOWED
