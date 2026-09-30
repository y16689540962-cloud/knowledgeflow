"""``resolve_short_link``：URL 规范化 + 平台 id 解析（定稿第六节）。

全部用 ``httpx.MockTransport``，不碰网络。
"""

from __future__ import annotations

import httpx
import pytest

from app.errors import ErrorType, URLValidationError
from app.ingestion.redirects import (
    BROWSER_HEADERS,
    REDIRECT_STATUSES,
    AwemeIdNotFoundError,
    CookieRequiredError,
    NetworkTimeoutError,
    ParserUnsupportedError,
    RequestBlockedError,
    ShortLinkResolutionError,
    extract_aweme_id,
    platform_of,
    resolve_short_link,
)

VIDEO_URL = "https://www.douyin.com/video/7321567890123456789"


def client_with(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )


def redirect(to: str, status: int = 302) -> httpx.Response:
    return httpx.Response(status, headers={"location": to})


# --------------------------------------------------------------------------- #
# aweme_id 提取
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.douyin.com/video/7321567890123456789", "7321567890123456789"),
        ("https://www.douyin.com/note/7300000000000000001", "7300000000000000001"),
        ("https://www.douyin.com/slides/7300000000000000002", "7300000000000000002"),
        ("https://www.iesdouyin.com/share/video/7321567890123456789", "7321567890123456789"),
        ("https://www.douyin.com/discover?modal_id=7321567890123456789", "7321567890123456789"),
        ("https://www.douyin.com/video/x?aweme_id=7321567890123456789", "7321567890123456789"),
        ("7321567890123456789", "7321567890123456789"),
    ],
)
def test_extract_aweme_id(url: str, expected: str) -> None:
    assert extract_aweme_id(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://www.douyin.com/",
        "https://www.douyin.com/video/list",
        "https://www.douyin.com/user/MS4wLjABAAAA",
        "https://www.douyin.com/video/123",  # 太短，不像 aweme_id
        "https://www.douyin.com/live/7321567890123456789",
    ],
)
def test_extract_aweme_id_returns_none(url: str) -> None:
    """抠不到就返回 None —— **绝不猜**。"""
    assert extract_aweme_id(url) is None


def test_platform_of() -> None:
    assert platform_of("www.douyin.com") == "douyin"
    assert platform_of("v.douyin.com") == "douyin"
    assert platform_of("www.iesdouyin.com") == "douyin"
    assert platform_of("example.com") is None


# --------------------------------------------------------------------------- #
# 成功路径
# --------------------------------------------------------------------------- #
async def test_direct_video_url() -> None:
    async with client_with(lambda request: httpx.Response(200, text="<html>")) as client:
        resolved = await resolve_short_link(VIDEO_URL, client=client)

    assert resolved.platform == "douyin"
    assert resolved.platform_id == "7321567890123456789"
    assert resolved.canonical_url == VIDEO_URL
    assert resolved.redirect_count == 0
    assert resolved.method == "HEAD"
    assert resolved.final_url == VIDEO_URL


async def test_short_link_follows_redirect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "v.douyin.com":
            return redirect(VIDEO_URL)
        return httpx.Response(200, text="<html>")

    async with client_with(handler) as client:
        resolved = await resolve_short_link("https://v.douyin.com/abcdefg/", client=client)

    assert resolved.final_url == VIDEO_URL
    assert resolved.redirect_count == 1
    assert resolved.platform_id == "7321567890123456789"
    assert resolved.original == "https://v.douyin.com/abcdefg/"


async def test_multiple_redirects_are_counted() -> None:
    hops = {
        "https://v.douyin.com/a/": "https://v.douyin.com/b/",
        "https://v.douyin.com/b/": VIDEO_URL,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        target = hops.get(str(request.url))
        if target:
            return redirect(target)
        return httpx.Response(200, text="ok")

    async with client_with(handler) as client:
        resolved = await resolve_short_link("https://v.douyin.com/a/", client=client)

    assert resolved.redirect_count == 2
    assert resolved.final_url == VIDEO_URL


@pytest.mark.parametrize("head_status", [403, 404, 405, 501])
async def test_head_failure_falls_back_to_get(head_status: int) -> None:
    """HEAD 只要不是成功就换 GET —— 实测抖音首页 HEAD 返回 404、GET 返回 200。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "HEAD":
            return httpx.Response(head_status, text="no head for you")
        return httpx.Response(200, text="<html>")

    async with client_with(handler) as client:
        resolved = await resolve_short_link(VIDEO_URL, client=client)

    assert calls == ["HEAD", "GET"]
    assert resolved.method == "GET"


async def test_successful_head_is_kept() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, text="")

    async with client_with(handler) as client:
        resolved = await resolve_short_link(VIDEO_URL, client=client)

    assert calls == ["HEAD"]
    assert resolved.method == "HEAD"


async def test_get_uses_range_header() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(403)
        seen.update(dict(request.headers))
        return httpx.Response(200, text="x")

    async with client_with(handler) as client:
        await resolve_short_link(VIDEO_URL, client=client)
    assert seen.get("range") == "bytes=0-1"


async def test_browser_headers_are_sent() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update({key.lower(): value for key, value in request.headers.items()})
        return httpx.Response(200, text="ok")

    async with client_with(handler) as client:
        await resolve_short_link(VIDEO_URL, client=client)
    assert seen["user-agent"] == BROWSER_HEADERS["User-Agent"]


async def test_follow_redirects_is_disabled_on_the_client() -> None:
    """必须自己跟随 —— 这样才能逐跳校验白名单、如实记跳了几次。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return redirect(VIDEO_URL)

    async with client_with(handler) as client:
        with pytest.raises(ShortLinkResolutionError):
            await resolve_short_link(
                "https://v.douyin.com/x/", client=client, max_redirects=0
            )
    assert calls == ["https://v.douyin.com/x/"]


# --------------------------------------------------------------------------- #
# 失败路径：error_type 必须精确（第二十二节）
# --------------------------------------------------------------------------- #
async def test_timeout_maps_to_network_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout")

    async with client_with(handler) as client:
        with pytest.raises(NetworkTimeoutError) as excinfo:
            await resolve_short_link(VIDEO_URL, client=client)
    assert excinfo.value.error_type is ErrorType.NETWORK_TIMEOUT


async def test_transport_error_maps_to_short_link_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    async with client_with(handler) as client:
        with pytest.raises(ShortLinkResolutionError) as excinfo:
            await resolve_short_link(VIDEO_URL, client=client)
    assert excinfo.value.error_type is ErrorType.SHORT_LINK_RESOLUTION_FAILED


@pytest.mark.parametrize("status", [403, 429])
async def test_blocked_statuses(status: int) -> None:
    async with client_with(lambda request: httpx.Response(status, text="nope")) as client:
        with pytest.raises(RequestBlockedError) as excinfo:
            await resolve_short_link(VIDEO_URL, client=client)
    assert excinfo.value.error_type is ErrorType.REQUEST_BLOCKED


async def test_401_means_cookie_required() -> None:
    async with client_with(lambda request: httpx.Response(401, text="login")) as client:
        with pytest.raises(CookieRequiredError) as excinfo:
            await resolve_short_link(VIDEO_URL, client=client)
    assert excinfo.value.error_type is ErrorType.COOKIE_REQUIRED


async def test_server_error_maps_to_short_link_failed() -> None:
    async with client_with(lambda request: httpx.Response(500, text="boom")) as client:
        with pytest.raises(ShortLinkResolutionError) as excinfo:
            await resolve_short_link(VIDEO_URL, client=client)
    assert excinfo.value.error_type is ErrorType.SHORT_LINK_RESOLUTION_FAILED


async def test_too_many_redirects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://v.douyin.com/loop/"})

    async with client_with(handler) as client:
        with pytest.raises(ShortLinkResolutionError) as excinfo:
            await resolve_short_link("https://v.douyin.com/start/", client=client, max_redirects=2)
    assert "重定向次数超过上限" in excinfo.value.message
    assert excinfo.value.context["redirect_count"] == 3


async def test_redirect_without_location() -> None:
    async with client_with(lambda request: httpx.Response(302)) as client:
        with pytest.raises(ShortLinkResolutionError):
            await resolve_short_link(VIDEO_URL, client=client)


async def test_redirect_outside_whitelist_is_refused() -> None:
    """跳转目标也必须落在白名单里 —— 不跟着跑到别的地方去。"""
    async with client_with(lambda request: redirect("https://evil.example.com/video/1")) as client:
        with pytest.raises(ShortLinkResolutionError) as excinfo:
            await resolve_short_link(VIDEO_URL, client=client)
    assert excinfo.value.context["blocked_host"] == "evil.example.com"


async def test_redirect_to_login_page_means_cookie_required() -> None:
    async with client_with(lambda request: redirect("https://passport.douyin.com/login")) as client:
        with pytest.raises(CookieRequiredError) as excinfo:
            await resolve_short_link(VIDEO_URL, client=client)
    assert excinfo.value.error_type is ErrorType.COOKIE_REQUIRED


async def test_input_scheme_is_validated() -> None:
    async with client_with(lambda request: httpx.Response(200)) as client:
        with pytest.raises(URLValidationError):
            await resolve_short_link("ftp://v.douyin.com/x", client=client)


async def test_input_domain_whitelist_is_enforced() -> None:
    async with client_with(lambda request: httpx.Response(200)) as client:
        with pytest.raises(URLValidationError):
            await resolve_short_link("https://evil.example.com/video/1", client=client)


# --------------------------------------------------------------------------- #
# 不伪造 source_id（第五节 / 第六节第 7 条）
# --------------------------------------------------------------------------- #
async def test_resolved_url_never_invents_id() -> None:
    """页面取回来了但 URL 里没有 id → ``platform_id`` 是 None，不是编一个。"""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return redirect("https://www.douyin.com/user/MS4wLjABAAAA")
        return httpx.Response(200, text="<html>")

    async with client_with(handler) as client:
        resolved = await resolve_short_link(VIDEO_URL, client=client)

    assert resolved.platform == "douyin"
    assert resolved.platform_id is None
    assert resolved.canonical_url is None


async def test_unknown_platform_is_reported_not_guessed() -> None:
    async with client_with(lambda request: httpx.Response(200, text="ok")) as client:
        resolved = await resolve_short_link(
            "https://www.bilibili.com/video/BV1xx", client=client
        )
    assert resolved.platform is None
    assert resolved.platform_id is None


def test_error_types_are_declared() -> None:
    assert ErrorType.SHORT_LINK_RESOLUTION_FAILED.value == "SHORT_LINK_RESOLUTION_FAILED"
    assert ErrorType.AWEME_ID_NOT_FOUND.value == "AWEME_ID_NOT_FOUND"
    assert ErrorType.REQUEST_BLOCKED.value == "REQUEST_BLOCKED"
    assert ErrorType.COOKIE_REQUIRED.value == "COOKIE_REQUIRED"
    assert ErrorType.PARSER_UNSUPPORTED.value == "PARSER_UNSUPPORTED"
    assert ErrorType.NETWORK_TIMEOUT.value == "NETWORK_TIMEOUT"


def test_exception_classes_exist_for_every_b_line_error() -> None:
    assert AwemeIdNotFoundError().error_type is ErrorType.AWEME_ID_NOT_FOUND
    assert ParserUnsupportedError().error_type is ErrorType.PARSER_UNSUPPORTED


def test_redirect_statuses_cover_the_usual_ones() -> None:
    assert REDIRECT_STATUSES == frozenset({301, 302, 303, 307, 308})
