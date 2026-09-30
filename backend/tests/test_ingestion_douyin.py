"""``DouyinSource``：URL → metadata（定稿第六 / 二十二 / 二十九节）。

不碰网络：解析与取页面都用 ``httpx.MockTransport``。
"""

from __future__ import annotations

import json
from urllib.parse import quote

import httpx
import pytest

from app.errors import ErrorType
from app.ingestion.douyin import (
    BLOCK_MARKERS,
    DETAIL_STATUS_OK,
    DOUYIN_DETAIL_API,
    LOGIN_MARKERS,
    NOTE_EMPTY_BODY,
    NOTE_NO_DETAIL,
    NOTE_NOT_JSON,
    NOTE_NOT_OBJECT,
    NOTE_STATUS_NOT_OK,
    TITLE_LIMIT,
    DouyinSource,
    classify_page,
    detail_api_url,
    interpret_detail_payload,
    parse_detail_payload,
    parse_metadata,
)
from app.ingestion.redirects import (
    AwemeIdNotFoundError,
    CookieRequiredError,
    NetworkTimeoutError,
    ParserUnsupportedError,
    RequestBlockedError,
    ResolvedURL,
    ShortLinkResolutionError,
)
from app.errors import URLValidationError

AWEME_ID = "7321567890123456789"
CANONICAL = f"https://www.douyin.com/video/{AWEME_ID}"
SHORT_LINK = "https://v.douyin.com/abcdefg/"
OTHER_AWEME_ID = "7300000000000000001"
OTHER_CANONICAL = f"https://www.douyin.com/video/{OTHER_AWEME_ID}"
#: 用户自己的登录态（第二十九条）。测试里也用假值 —— 绝不能把真实凭据写进仓库。
COOKIE = "sessionid=test-session; ttwid=test-ttwid"


def aweme_node(
    *,
    aweme_id: str = AWEME_ID,
    desc: str = "三分钟讲清楚 AI Agent 到底是什么",
    nickname: str = "老张说AI",
    uid: str = "1234567890",
    images: bool = False,
) -> dict:
    """一个 ``aweme`` 节点的本体 —— 页面内嵌 JSON 与详情接口里是同构的。"""
    node: dict = {
        "aweme_id": aweme_id,
        "desc": desc,
        "author": {"nickname": nickname, "uid": uid},
        "video": {"cover": {"url_list": ["https://p3.example.com/cover.jpg"]}},
    }
    if images:
        node["images"] = [{"url_list": ["https://p3.example.com/img1.jpg"]}]
    return node


def aweme_payload(**kwargs) -> dict:
    return {"app": {"aweme": {"detail": aweme_node(**kwargs)}}}


def detail_payload(*, status_code: int = 0, **kwargs) -> dict:
    """详情接口的回包形态（实测：``{aweme_detail, log_pb, status_code}``）。"""
    return {"status_code": status_code, "aweme_detail": aweme_node(**kwargs)}


def render_data_page(payload: dict) -> str:
    encoded = quote(json.dumps(payload, ensure_ascii=False))
    return (
        "<html><head><title>抖音</title></head><body>"
        f'<script id="RENDER_DATA" type="application/json">{encoded}</script>'
        "</body></html>"
    )


def router_data_page(payload: dict) -> str:
    body = json.dumps(payload, ensure_ascii=False)
    return f"<html><body><script>window._ROUTER_DATA = {body};</script></body></html>"


def make_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )


def resolved(aweme_id: str = AWEME_ID) -> ResolvedURL:
    """直接构造一个「已经解析好」的结果 —— 用来单独测取元数据那一步。"""
    page = f"https://www.douyin.com/video/{aweme_id}"
    return ResolvedURL(
        original=page,
        final_url=page,
        host="www.douyin.com",
        scheme="https",
        status_code=200,
        redirect_count=0,
        method="GET",
        platform="douyin",
        platform_id=aweme_id,
    )


def ok_handler(html: str):
    """短链 → 规范 URL；规范 URL 返回给定页面。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "v.douyin.com":
            return httpx.Response(302, headers={"location": CANONICAL})
        return httpx.Response(200, text=html)

    return handler


#: 2026-09-30 实测：抖音页面只吐一个 JS 壳（``_$jsvmprt`` 虚拟机代码），
#: 服务端**不再**内嵌任何数据。``RENDER_DATA`` / ``_ROUTER_DATA`` 一个都没有。
JS_SHELL = (
    "<html><head><title>抖音</title></head><body>"
    "<script>var _$jsvmprt=function(){return {};}();</script>"
    "</body></html>"
)

DETAIL_API_PATH = "/aweme/v1/web/aweme/detail"


def is_detail_api(request: httpx.Request) -> bool:
    return request.url.path.startswith(DETAIL_API_PATH)


def api_aware_handler(
    *,
    api: str | None = None,
    page: str = JS_SHELL,
    api_status: int = 200,
    page_status: int = 200,
    on_request=None,
):
    """详情接口与页面分开回包 —— 用来验证「先接口、后页面」的顺序与降级。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if on_request is not None:
            on_request(request)
        if request.url.host == "v.douyin.com":
            return httpx.Response(302, headers={"location": CANONICAL})
        if is_detail_api(request):
            if api is None:  # 匿名访问的真实形态：200 + 空正文
                return httpx.Response(api_status, content=b"")
            return httpx.Response(
                api_status,
                content=api.encode("utf-8"),
                headers={"content-type": "application/json; charset=utf-8"},
            )
        return httpx.Response(page_status, text=page)

    return handler


# --------------------------------------------------------------------------- #
# 纯函数：解析
# --------------------------------------------------------------------------- #
def test_parse_render_data() -> None:
    metadata = parse_metadata(render_data_page(aweme_payload()), aweme_id=AWEME_ID)
    assert metadata is not None
    assert metadata.aweme_id == AWEME_ID
    assert metadata.title == "三分钟讲清楚 AI Agent 到底是什么"
    assert metadata.author == "老张说AI"
    assert metadata.author_id == "1234567890"
    assert metadata.media_type == "video"
    assert metadata.cover_url == "https://p3.example.com/cover.jpg"
    assert metadata.source == "embedded-json"


def test_parse_router_data_fallback() -> None:
    metadata = parse_metadata(router_data_page(aweme_payload()), aweme_id=AWEME_ID)
    assert metadata is not None
    assert metadata.title == "三分钟讲清楚 AI Agent 到底是什么"


def test_parse_prefers_render_data() -> None:
    html = f'<script id="RENDER_DATA">{quote(json.dumps(aweme_payload(desc="A"*300)))}</script>' + router_data_page(
        aweme_payload(desc="B")
    )
    metadata = parse_metadata(html, aweme_id=AWEME_ID)
    assert metadata is not None and metadata.title is not None
    assert metadata.title.startswith("A")


def test_title_is_single_line_and_truncated() -> None:
    metadata = parse_metadata(
        render_data_page(aweme_payload(desc="第一行\n第二行" + "长" * 200)), aweme_id=AWEME_ID
    )
    assert metadata is not None
    assert metadata.title is not None
    assert "\n" not in metadata.title
    assert len(metadata.title) == TITLE_LIMIT
    assert metadata.description is not None and "\n" in metadata.description


def test_image_post_media_type() -> None:
    metadata = parse_metadata(
        render_data_page(aweme_payload(images=True)), aweme_id=AWEME_ID
    )
    assert metadata is not None
    assert metadata.media_type == "image"
    assert metadata.cover_url == "https://p3.example.com/img1.jpg"


def test_parse_returns_none_without_embedded_json() -> None:
    assert parse_metadata("<html><body>普通页面</body></html>", aweme_id=AWEME_ID) is None


def test_parse_returns_none_when_no_aweme_node() -> None:
    assert parse_metadata(render_data_page({"app": {"other": 1}}), aweme_id=AWEME_ID) is None


def test_node_without_aweme_id_is_not_used() -> None:
    """只带 ``desc`` 的字典不算「视频节点」—— 宁可不解析，也不乱认。"""
    assert parse_metadata(render_data_page({"detail": {"desc": "没有 id"}}), aweme_id=AWEME_ID) is None


def test_empty_desc_gives_no_title() -> None:
    metadata = parse_metadata(render_data_page(aweme_payload(desc="   ")), aweme_id=AWEME_ID)
    assert metadata is not None and metadata.title is None


# --------------------------------------------------------------------------- #
# 纯函数：页面分类
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("marker", BLOCK_MARKERS)
def test_block_markers_are_classified(marker: str) -> None:
    error = classify_page(f"<html>{marker}</html>")
    assert isinstance(error, RequestBlockedError)
    assert error.error_type is ErrorType.REQUEST_BLOCKED


@pytest.mark.parametrize("marker", LOGIN_MARKERS)
def test_login_markers_are_classified(marker: str) -> None:
    error = classify_page(f"<html>{marker}</html>")
    assert isinstance(error, CookieRequiredError)
    assert error.error_type is ErrorType.COOKIE_REQUIRED


def test_normal_page_is_not_classified() -> None:
    assert classify_page("<html>正常内容</html>") is None


# --------------------------------------------------------------------------- #
# 端到端（mock）
# --------------------------------------------------------------------------- #
async def test_fetch_happy_path() -> None:
    async with make_client(ok_handler(render_data_page(aweme_payload()))) as client:
        source = DouyinSource(client=client)
        raw = await source.fetch(SHORT_LINK)

    assert raw.source == "douyin"
    assert raw.source_id == AWEME_ID  # source_id = aweme_id（不是 URL）
    assert raw.source_url == CANONICAL
    assert raw.title == "三分钟讲清楚 AI Agent 到底是什么"
    assert raw.author == "老张说AI"
    assert raw.author_id == "1234567890"
    assert raw.media_type == "video"
    assert raw.media_path is None
    assert raw.transcript is None  # Phase 6 只做 URL → metadata


async def test_direct_url_also_works() -> None:
    async with make_client(ok_handler(render_data_page(aweme_payload()))) as client:
        raw = await DouyinSource(client=client).fetch(CANONICAL)
    assert raw.source_id == AWEME_ID


async def test_source_name_property() -> None:
    async with make_client(ok_handler("")) as client:
        assert DouyinSource(client=client).name == "douyin"


async def test_no_cookie_header_is_sent() -> None:
    """合规证据（第二十九条）：一个 cookie 都不带。"""
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({key.lower(): value for key, value in request.headers.items()})
        if request.url.host == "v.douyin.com":
            return httpx.Response(302, headers={"location": CANONICAL})
        return httpx.Response(200, text=render_data_page(aweme_payload()))

    async with make_client(handler) as client:
        await DouyinSource(client=client).fetch(SHORT_LINK)

    assert seen, "应当发过请求"
    for headers in seen:
        assert "cookie" not in headers
        assert "authorization" not in headers


async def test_page_blocked() -> None:
    html = f"<html>{BLOCK_MARKERS[0]}</html>"
    async with make_client(ok_handler(html)) as client:
        with pytest.raises(RequestBlockedError):
            await DouyinSource(client=client).fetch(SHORT_LINK)


async def test_page_requires_login() -> None:
    html = f"<html>{LOGIN_MARKERS[0]}</html>"
    async with make_client(ok_handler(html)) as client:
        with pytest.raises(CookieRequiredError):
            await DouyinSource(client=client).fetch(SHORT_LINK)


async def test_no_data_without_cookie_reports_cookie_required() -> None:
    """匿名 + 页面壳页 + 接口空正文 = 唯一可操作的原因是「没有登录态」。

    别报 ``PARSER_UNSUPPORTED``：那会让人去翻页面结构，而真正该做的事是填 Cookie。
    """
    handler = api_aware_handler(api=None, page="<html><body>没有内嵌数据</body></html>")
    async with make_client(handler) as client:
        with pytest.raises(CookieRequiredError) as excinfo:
            await DouyinSource(client=client).fetch(SHORT_LINK)

    assert excinfo.value.error_type is ErrorType.COOKIE_REQUIRED
    assert excinfo.value.context["html_length"] > 0
    assert excinfo.value.context["detail_api_note"] == NOTE_EMPTY_BODY
    assert excinfo.value.context["cookie_configured"] is False


async def test_no_data_with_cookie_is_parser_unsupported() -> None:
    """带了登录态还是没数据 → 这时才该说「结构可能变了」。"""
    handler = api_aware_handler(api=None, page="<html><body>没有内嵌数据</body></html>")
    async with make_client(handler) as client:
        with pytest.raises(ParserUnsupportedError) as excinfo:
            await DouyinSource(client=client, cookie=COOKIE).fetch(SHORT_LINK)

    assert excinfo.value.error_type is ErrorType.PARSER_UNSUPPORTED
    assert excinfo.value.context["html_length"] > 0
    assert excinfo.value.context["cookie_configured"] is True


@pytest.mark.parametrize("status", [403, 429])
async def test_page_blocked_by_status(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "v.douyin.com":
            return httpx.Response(302, headers={"location": CANONICAL})
        return httpx.Response(status, text="denied")

    async with make_client(handler) as client:
        with pytest.raises(RequestBlockedError):
            await DouyinSource(client=client).fetch(SHORT_LINK)


async def test_page_401_means_cookie_required() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "v.douyin.com":
            return httpx.Response(302, headers={"location": CANONICAL})
        return httpx.Response(401, text="login")

    async with make_client(handler) as client:
        with pytest.raises(CookieRequiredError):
            await DouyinSource(client=client).fetch(SHORT_LINK)


async def test_page_server_error_is_resolution_failure() -> None:
    """解析那一步过了（200），但取正文时服务端 500 → 报「取不到」，不是「解析不了」。"""
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "v.douyin.com":
            return httpx.Response(302, headers={"location": CANONICAL})
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(200, text="ok")  # resolve 那一次
        return httpx.Response(500, text="boom")  # 取正文那一次

    async with make_client(handler) as client:
        with pytest.raises(ShortLinkResolutionError):
            await DouyinSource(client=client).fetch(SHORT_LINK)


async def test_page_not_found_is_parser_unsupported() -> None:
    """HTTP 404：页面根本不存在，没什么可解析的。"""
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "v.douyin.com":
            return httpx.Response(302, headers={"location": CANONICAL})
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(200, text="ok")
        return httpx.Response(404, text="not found")

    async with make_client(handler) as client:
        with pytest.raises(ParserUnsupportedError):
            await DouyinSource(client=client).fetch(SHORT_LINK)


async def test_page_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "v.douyin.com":
            return httpx.Response(302, headers={"location": CANONICAL})
        raise httpx.ReadTimeout("slow")

    async with make_client(handler) as client:
        with pytest.raises(NetworkTimeoutError) as excinfo:
            await DouyinSource(client=client).fetch(SHORT_LINK)
    assert excinfo.value.error_type is ErrorType.NETWORK_TIMEOUT


async def test_resolve_reports_missing_aweme_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "v.douyin.com":
            return httpx.Response(302, headers={"location": "https://www.douyin.com/user/MS4w"})
        return httpx.Response(200, text="<html>")

    async with make_client(handler) as client:
        with pytest.raises(AwemeIdNotFoundError) as excinfo:
            await DouyinSource(client=client).fetch(SHORT_LINK)
    assert excinfo.value.error_type is ErrorType.AWEME_ID_NOT_FOUND


async def test_resolve_reports_unsupported_platform() -> None:
    async with make_client(lambda request: httpx.Response(200, text="ok")) as client:
        with pytest.raises(ParserUnsupportedError) as excinfo:
            await DouyinSource(client=client).fetch("https://www.bilibili.com/video/BV1")
    assert excinfo.value.error_type is ErrorType.PARSER_UNSUPPORTED


async def test_resolve_returns_structured_result() -> None:
    async with make_client(ok_handler(render_data_page(aweme_payload()))) as client:
        resolved = await DouyinSource(client=client).resolve(SHORT_LINK)
    assert resolved.platform == "douyin"
    assert resolved.platform_id == AWEME_ID
    assert resolved.to_dict()["canonical_url"] == CANONICAL


async def test_two_videos_give_different_source_ids() -> None:
    """不同视频必须得到不同的 source_id —— 判重就靠它。"""
    payload_one = render_data_page(aweme_payload())
    payload_two = render_data_page(aweme_payload(aweme_id=OTHER_AWEME_ID, desc="另一条视频"))

    async with make_client(ok_handler(payload_one)) as client:
        first = await DouyinSource(client=client).fetch(SHORT_LINK)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "v.douyin.com":
            return httpx.Response(302, headers={"location": OTHER_CANONICAL})
        return httpx.Response(200, text=payload_two)

    async with make_client(handler) as client:
        second = await DouyinSource(client=client).fetch("https://v.douyin.com/other/")

    assert first.source_id != second.source_id
    assert first.source_url != second.source_url


# --------------------------------------------------------------------------- #
# 客户端生命周期
# --------------------------------------------------------------------------- #
async def test_injected_client_is_not_closed() -> None:
    client = make_client(lambda request: httpx.Response(200, text="x"))
    source = DouyinSource(client=client)
    await source.aclose()
    assert not client.is_closed
    await client.aclose()


async def test_owned_client_is_closed() -> None:
    source = DouyinSource()
    await source.aclose()
    assert source._client.is_closed  # noqa: SLF001 - 就是要断言内部客户端状态


async def test_async_context_manager() -> None:
    source = DouyinSource(client=make_client(lambda request: httpx.Response(200, text="x")))
    async with source as entered:
        assert entered is source


async def test_transport_error_on_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "v.douyin.com":
            return httpx.Response(302, headers={"location": CANONICAL})
        raise httpx.ConnectError("down")

    async with make_client(handler) as client:
        with pytest.raises(ShortLinkResolutionError):
            await DouyinSource(client=client).fetch(SHORT_LINK)


# --------------------------------------------------------------------------- #
# 详情接口这条来源（2026-09-30 加：页面已改成 JS 壳页）
# --------------------------------------------------------------------------- #
def test_detail_api_url_carries_only_the_id_and_public_params() -> None:
    """第二十九条：接口参数里**不许**出现任何签名/加密参数。"""
    url = detail_api_url(AWEME_ID)
    assert url.startswith(DOUYIN_DETAIL_API)
    assert f"aweme_id={AWEME_ID}" in url
    assert url.startswith("https://www.douyin.com/")


def test_parse_detail_payload_fields() -> None:
    metadata = parse_detail_payload(detail_payload(), aweme_id=AWEME_ID)
    assert metadata is not None
    assert metadata.aweme_id == AWEME_ID
    assert metadata.title == "三分钟讲清楚 AI Agent 到底是什么"
    assert metadata.author == "老张说AI"
    assert metadata.author_id == "1234567890"
    assert metadata.media_type == "video"
    assert metadata.cover_url == "https://p3.example.com/cover.jpg"
    assert metadata.source == "detail-api"
    assert metadata.to_dict()["parsed_by"] == "detail-api"


def test_parse_detail_payload_image_post() -> None:
    metadata = parse_detail_payload(detail_payload(images=True), aweme_id=AWEME_ID)
    assert metadata is not None
    assert metadata.media_type == "image"
    assert metadata.cover_url == "https://p3.example.com/img1.jpg"


@pytest.mark.parametrize(
    "payload",
    [
        "<html>不是对象</html>",
        [],
        {},
        {"aweme_detail": None},
        {"aweme_detail": {"desc": "没有 id 的节点"}},
    ],
)
def test_parse_detail_payload_rejects_unusable_payload(payload: object) -> None:
    assert parse_detail_payload(payload, aweme_id=AWEME_ID) is None


def test_interpret_detail_payload_ok_has_no_reason() -> None:
    metadata, reason = interpret_detail_payload(detail_payload(), aweme_id=AWEME_ID)
    assert metadata is not None
    assert reason is None


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (None, NOTE_NOT_OBJECT),
        ([1, 2], NOTE_NOT_OBJECT),
        ({"status_code": 5}, f"{NOTE_STATUS_NOT_OK}:5"),
        ({"status_code": DETAIL_STATUS_OK}, NOTE_NO_DETAIL),
    ],
)
def test_interpret_detail_payload_explains_the_miss(payload: object, reason: str) -> None:
    """拿不到数据时要说清楚**为什么** —— 否则线上只看到一个含糊的「结构已变」。"""
    metadata, got = interpret_detail_payload(payload, aweme_id=AWEME_ID)
    assert metadata is None
    assert got == reason


async def test_detail_api_is_used_before_the_page() -> None:
    """接口能用就不去解析页面 —— 页面已经是 JS 壳页，靠它必失败。

    这里直接喂一个 ``ResolvedURL`` 给 ``fetch_metadata``，把 ``resolve()``
    那一步自带的请求噪音摘掉，断言才看得清「元数据是从哪来的」。
    """
    seen: list[str] = []

    handler = api_aware_handler(
        api=json.dumps(detail_payload(), ensure_ascii=False),
        on_request=lambda request: seen.append(request.url.path),
    )
    async with make_client(handler) as client:
        metadata = await DouyinSource(client=client, cookie=COOKIE).fetch_metadata(
            resolved()
        )

    assert metadata.source == "detail-api"
    assert metadata.title == "三分钟讲清楚 AI Agent 到底是什么"
    assert metadata.to_dict()["parsed_by"] == "detail-api"
    assert len(seen) == 1, f"只该调接口，实际请求了 {seen}"
    assert seen[0].startswith(DETAIL_API_PATH)


async def test_detail_api_supplies_the_raw_text() -> None:
    """接口路径拿到的文案同样要进 ``raw_text`` —— 否则 A 线直接 EMPTY_SOURCE_TEXT。"""
    handler = api_aware_handler(api=json.dumps(detail_payload(), ensure_ascii=False))
    async with make_client(handler) as client:
        raw = await DouyinSource(client=client, cookie=COOKIE).fetch(SHORT_LINK)

    assert raw.source_id == AWEME_ID
    assert raw.title == "三分钟讲清楚 AI Agent 到底是什么"
    assert raw.raw_text == "三分钟讲清楚 AI Agent 到底是什么"
    assert raw.author == "老张说AI"


async def test_api_returning_html_records_not_json_reason() -> None:
    """接口回包不是 JSON 时要留下可定位的原因（不是闷声降级）。"""
    handler = api_aware_handler(api="<html>壳页</html>", page=JS_SHELL)
    async with make_client(handler) as client:
        with pytest.raises(ParserUnsupportedError) as excinfo:
            await DouyinSource(client=client, cookie=COOKIE).fetch(SHORT_LINK)

    assert excinfo.value.context["detail_api_note"] == NOTE_NOT_JSON


async def test_falls_back_to_page_when_api_is_not_json() -> None:
    """接口被墙成 HTML（或哪天改回去内嵌数据了）→ 依然能靠页面解析拿到元数据。"""
    handler = api_aware_handler(api="<html>不是 JSON</html>", page=render_data_page(aweme_payload()))
    async with make_client(handler) as client:
        raw = await DouyinSource(client=client, cookie=COOKIE).fetch(SHORT_LINK)

    assert raw.title == "三分钟讲清楚 AI Agent 到底是什么"


async def test_falls_back_to_page_when_status_code_is_not_ok() -> None:
    handler = api_aware_handler(
        api=json.dumps({"status_code": 8, "aweme_detail": None}),
        page=render_data_page(aweme_payload()),
    )
    async with make_client(handler) as client:
        raw = await DouyinSource(client=client, cookie=COOKIE).fetch(SHORT_LINK)

    assert raw.title == "三分钟讲清楚 AI Agent 到底是什么"


async def test_cookie_reaches_the_detail_api() -> None:
    """详情接口必须带上用户**自己**的登录态，否则只会拿到空正文。"""
    cookies: list[str | None] = []

    def record(request: httpx.Request) -> None:
        if is_detail_api(request):
            cookies.append(request.headers.get("cookie"))

    handler = api_aware_handler(api=json.dumps(detail_payload()), on_request=record)
    async with make_client(handler) as client:
        await DouyinSource(client=client, cookie=COOKIE).fetch(SHORT_LINK)

    assert cookies == [COOKIE]


async def test_anonymous_detail_api_call_sends_no_cookie() -> None:
    """不传 Cookie 就是匿名 —— 不许悄悄塞一个空的会话头。"""
    cookies: list[str | None] = []

    def record(request: httpx.Request) -> None:
        if is_detail_api(request):
            cookies.append(request.headers.get("cookie"))

    handler = api_aware_handler(api=json.dumps(detail_payload()), on_request=record)
    async with make_client(handler) as client:
        await DouyinSource(client=client).fetch(SHORT_LINK)

    assert cookies == [None]


@pytest.mark.parametrize("status", [401, 403, 429])
async def test_detail_api_status_errors_keep_their_meaning(status: int) -> None:
    """接口层的状态码语义与取页面**完全一致** —— 不给「接口」开小灶。"""
    handler = api_aware_handler(api="x", api_status=status)
    expected = CookieRequiredError if status == 401 else RequestBlockedError
    async with make_client(handler) as client:
        with pytest.raises(expected):
            await DouyinSource(client=client, cookie=COOKIE).fetch(SHORT_LINK)


async def test_detail_api_404_is_parser_unsupported() -> None:
    handler = api_aware_handler(api="x", api_status=404)
    async with make_client(handler) as client:
        with pytest.raises(ParserUnsupportedError):
            await DouyinSource(client=client, cookie=COOKIE).fetch(SHORT_LINK)


async def test_detail_api_server_error_is_resolution_failure() -> None:
    handler = api_aware_handler(api="x", api_status=503)
    async with make_client(handler) as client:
        with pytest.raises(ShortLinkResolutionError):
            await DouyinSource(client=client, cookie=COOKIE).fetch(SHORT_LINK)


async def test_verify_page_still_blocks_even_with_the_api_path() -> None:
    """页面被判定为验证页时照旧报 REQUEST_BLOCKED（接口也给不出数据）。"""
    handler = api_aware_handler(api=None, page=f"<html>{BLOCK_MARKERS[0]}</html>")
    async with make_client(handler) as client:
        with pytest.raises(RequestBlockedError):
            await DouyinSource(client=client, cookie=COOKIE).fetch(SHORT_LINK)


# --------------------------------------------------------------------------- #
# 分享文案（用户粘的往往**不是**链接，是整段抖音分享文案）
# --------------------------------------------------------------------------- #
DOUYIN_SHARE_TEXT = (
    "2.05 08/27 CHv:/ U@Y.Zz :2pm 一百元在南京夜市都能吃到啥？今天又爽吃了！！"
    "# 日常vlog # 夜市 # 情侣 # 路边摊美味 https://v.douyin.com/abcdefg/ "
    "复制此链接，打开Dou音搜索，直接观看视频！"
)


async def test_share_text_is_accepted_and_the_link_is_extracted() -> None:
    """抖音「复制链接」给的是整段文案 —— 只认纯 URL 会让这个最常见的动作直接 400。"""
    actions: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        actions.append(str(request.url))
        if request.url.host == "v.douyin.com":
            return httpx.Response(302, headers={"location": CANONICAL})
        return httpx.Response(200, text=render_data_page(aweme_payload()))

    async with make_client(handler) as client:
        raw = await DouyinSource(client=client).fetch(DOUYIN_SHARE_TEXT)

    assert raw.source_id == AWEME_ID
    # 真的只把链接那一段发出去了，不是整段文案
    assert any(url.startswith("https://v.douyin.com/abcdefg/") for url in actions)


async def test_plain_url_is_untouched() -> None:
    """本来就是链接时不走提取 —— 别给用户「我改了你的输入」的意外。"""
    async with make_client(ok_handler(render_data_page(aweme_payload()))) as client:
        raw = await DouyinSource(client=client).fetch(SHORT_LINK)
    assert raw.source_id == AWEME_ID


async def test_text_without_any_link_reports_invalid_url() -> None:
    """整段话里一个链接都没有 → INVALID_URL，且**不回显**用户粘的正文。"""
    async with make_client(ok_handler("<html>")) as client:
        with pytest.raises(URLValidationError) as excinfo:
            await DouyinSource(client=client).fetch("一百元在南京夜市都能吃到啥？没有链接")

    assert excinfo.value.error_type is ErrorType.INVALID_URL
    assert excinfo.value.context["input_length"] > 0
    assert "没有链接" not in str(excinfo.value.to_dict())


async def test_other_scheme_still_reports_unsupported_scheme() -> None:
    """带 ``://`` 但不是 http/https → 报错交给 ``validate_url``，语义不变。"""
    async with make_client(ok_handler("<html>")) as client:
        with pytest.raises(URLValidationError) as excinfo:
            await DouyinSource(client=client).fetch("ftp://www.douyin.com/video/1")
    assert excinfo.value.error_type is ErrorType.UNSUPPORTED_URL_SCHEME


async def test_extracted_link_still_has_to_pass_the_whitelist() -> None:
    """提取**不削弱**校验：抠出来的链接域名不在白名单 → 照样 DOMAIN_NOT_ALLOWED。"""
    async with make_client(ok_handler("<html>")) as client:
        with pytest.raises(URLValidationError) as excinfo:
            await DouyinSource(client=client).fetch("来看这个 https://evil.com/video/1 超好看")
    assert excinfo.value.error_type is ErrorType.DOMAIN_NOT_ALLOWED


def test_module_does_not_forge_signatures() -> None:
    """合规：模块里不允许出现「签名 / 破解 / 绕过」类实现痕迹。"""
    import inspect

    module_source = open(
        inspect.getsourcefile(DouyinSource) or "", encoding="utf-8"
    ).read().lower()
    for banned in ("x-bogus", "signature=", "a_bogus", "captcha_solve", "bypass"):
        assert banned not in module_source, f"doyin.py 出现了 {banned}"
