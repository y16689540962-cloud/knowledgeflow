"""用户自己的合法登录态支持（定稿第二十九条）。

**合规边界写死在测试里**：只接受用户显式传入的 Cookie，不窃取、不伪造、不绕过验证码；
且它**永不进日志**（第二十四节）。抖音对匿名请求返回验证页这件事已由 Q5 联网实测确认。
"""

from __future__ import annotations

import logging

import httpx
import pytest

from app.ingestion.douyin import DouyinSource
from app.ingestion.redirects import with_cookie

COOKIE = "sessionid=abc123; ttwid=xyz"

AWEME_ID = "7321567890123456789"
CANONICAL = f"https://www.douyin.com/video/{AWEME_ID}"
SHORT_LINK = "https://v.douyin.com/abcdefg/"


def redirect_handler(on_request=None):
    """短链 → 规范 URL；规范 URL 返回一个空页面（够跑完 URL → id 这一段）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if on_request is not None:
            on_request(request)
        if request.url.host == "v.douyin.com":
            return httpx.Response(302, headers={"location": CANONICAL}, request=request)
        return httpx.Response(200, text="<html></html>", request=request)

    return handler


# --------------------------------------------------------------------------- #
# with_cookie
# --------------------------------------------------------------------------- #
def test_none_and_blank_produce_no_header() -> None:
    """空的 Cookie 头比没有更糟 —— 服务端会以为有会话。"""
    assert with_cookie(None) == {}
    assert with_cookie("") == {}
    assert with_cookie("   ") == {}


def test_cookie_is_trimmed_into_header() -> None:
    assert with_cookie(f"  {COOKIE}  ") == {"Cookie": COOKIE}


# --------------------------------------------------------------------------- #
# 真的发出去
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cookie_reaches_the_wire() -> None:
    """短链解析的每一跳都要带上用户自己的 Cookie。"""
    seen: list[str | None] = []

    def record(request: httpx.Request) -> None:
        seen.append(request.headers.get("cookie"))

    transport = httpx.MockTransport(redirect_handler(record))
    async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
        source = DouyinSource(client=client, cookie=COOKIE)
        await source.resolve(SHORT_LINK)

    assert seen, "一次请求都没发出去"
    assert all(value == COOKIE for value in seen)


@pytest.mark.asyncio
async def test_anonymous_requests_send_no_cookie_header() -> None:
    """不传就是匿名 —— 不许悄悄塞一个空的或者继承别人的会话。"""
    seen: list[str | None] = []

    def record(request: httpx.Request) -> None:
        seen.append(request.headers.get("cookie"))

    transport = httpx.MockTransport(redirect_handler(record))
    async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
        source = DouyinSource(client=client)
        await source.resolve(SHORT_LINK)

    assert seen
    assert all(value is None for value in seen)


# --------------------------------------------------------------------------- #
# 脱敏
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cookie_never_appears_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    """第二十四节：日志只记结构，不记正文与凭据。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>验证后继续</html>", request=request)

    transport = httpx.MockTransport(handler)
    with caplog.at_level(logging.DEBUG, logger="knowledgeflow.douyin"):
        async with httpx.AsyncClient(transport=transport) as client:
            source = DouyinSource(client=client, cookie=COOKIE)
            with pytest.raises(Exception):  # noqa: B017 - 具体类型由 classify_page 决定
                await source.resolve("https://v.douyin.com/abcdefg/")

    assert COOKIE not in caplog.text
    assert "sessionid" not in caplog.text
