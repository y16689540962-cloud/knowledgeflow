"""``DouyinSource``：抖音 URL → ``RawContent``（定稿文档第六 / 二十二 / 二十六节）。

链路：

```text
Douyin URL → resolve_short_link() → aweme_id → 取元数据 → RawContent
```

Phase 6 的范围是「**先只做 URL → metadata**」：不下载媒体、不做 ASR/OCR
（``Assets/`` 目录留到需要媒体时再用）。

**元数据有两个来源，按顺序试**（2026-09-30 实测后加的第一条）：

1. **详情接口** ``/aweme/v1/web/aweme/detail/?aweme_id=…`` —— 返回结构化 JSON
   （``aweme_detail``），字段与页面内嵌 JSON 完全同构，所以复用同一套解析逻辑。
   **它需要登录态**：匿名访问会返回 HTTP 200 + **空正文**（不是报错，是静默空响应）。
2. **页面内嵌 JSON**（``RENDER_DATA`` / ``_ROUTER_DATA``）—— 原始路径。
   实测抖音已改为服务端只吐 JS 壳页（正文里只有 ``_$jsvmprt`` 虚拟机代码，
   没有任何内嵌数据），所以这条**目前基本打不中**；保留它是为了
   「抖音哪天改回去」以及「别的镜像域名还能用」，而不是为了好看。

先试接口再退页面，是因为接口是结构化的、一次请求就够；页面解析留着当兜底。
两条都拿不到时**如实报错**，绝不伪造 ``RawContent``。

**允许 degraded**：采集失败必须记明确的 ``error_type``，不得阻塞 Core Pipeline、
不得伪造 ``RawContent``、不得返回假的成功。

合规边界（第二十九条）：只用公开可访问的页面做 GET，**不伪造签名、不窃取 Cookie、
不绕验证码**。被挡住就如实报 ``REQUEST_BLOCKED`` / ``COOKIE_REQUIRED``。

关于「标题」与「正文」：抖音视频没有独立的标题字段，只有文案（``desc``）。
所以：

* ``desc`` 当作 ``RawContent.title`` —— 文件名可读，``content_hash`` 也有区分度
  （否则所有拿不到元数据的视频哈希都一样，会被 Phase 5 的同 source hash 去重误杀）
* ``desc`` 同时进 ``raw_text`` —— 否则 A 线会因为「源文本为空」直接
  ``EMPTY_SOURCE_TEXT`` 失败；真正的逐字稿由 Phase 7 的 ASR 补进 ``transcript``
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import unquote, urlencode, urlsplit

import httpx

from app.errors import ErrorType, KnowledgeFlowError, URLValidationError
from app.ingestion.base import ContentSource
from app.ingestion.redirects import (
    DEFAULT_MAX_REDIRECTS,
    BROWSER_HEADERS,
    DEFAULT_TIMEOUT_SECONDS,
    PLATFORM_DOUYIN,
    AwemeIdNotFoundError,
    CookieRequiredError,
    NetworkTimeoutError,
    ParserUnsupportedError,
    RequestBlockedError,
    ResolvedURL,
    ShortLinkResolutionError,
    resolve_short_link,
    with_cookie,
)
from app.llm.json_extraction import find_balanced_json
from app.logging_config import log_event
from app.normalization.url import extract_first_url
from app.schemas.content import RawContent

SOURCE_NAME: Final[str] = "douyin"
STAGE: Final[str] = "douyin"
TITLE_LIMIT: Final[int] = 80

#: 页面里可能出现「需要验证 / 登录」的标记（命中则如实报错，不做任何绕过）。
BLOCK_MARKERS: Final[tuple[str, ...]] = (
    "验证后继续",
    "请完成验证",
    "captcha",
    "slider-verify",
)
LOGIN_MARKERS: Final[tuple[str, ...]] = (
    "登录后查看",
    "请先登录",
    "passport.douyin.com",
)

#: 内嵌 JSON 的两种形态。
RENDER_DATA_MARKER: Final[str] = 'id="RENDER_DATA"'
ROUTER_DATA_MARKER: Final[str] = "_ROUTER_DATA"

#: 详情接口（结构化元数据的来源）。域名 ``www.douyin.com`` 已在白名单内。
DOUYIN_DETAIL_API: Final[str] = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
#: 接口要求的固定查询串 —— 就是 web 端自己发的那些，**没有任何签名参数**
#: （第二十九条：不伪造签名）。``aweme_id`` 由调用方补上。
DOUYIN_DETAIL_API_PARAMS: Final[dict[str, str]] = {
    "device_platform": "webapp",
    "aid": "6383",
    "channel": "channel_pc_web",
    "pc_client_type": "1",
    "version_code": "190500",
    "version_name": "19.5.0",
    "cookie_enabled": "true",
    "platform": "PC",
    "downlink": "10",
}
#: 接口是 JSON 接口，请求头要跟取页面不一样。
JSON_ACCEPT: Final[str] = "application/json, text/plain, */*"
#: 接口回包里的业务状态码：0 = 正常。
DETAIL_STATUS_OK: Final[int] = 0

#: 拿不到接口数据时 ``note`` 可能取的值（进 ``error.context``，便于事后定位）。
NOTE_EMPTY_BODY: Final[str] = "empty-body"
NOTE_NOT_JSON: Final[str] = "not-json"
NOTE_NOT_OBJECT: Final[str] = "not-object"
NOTE_STATUS_NOT_OK: Final[str] = "status-not-ok"
NOTE_NO_DETAIL: Final[str] = "no-aweme-detail"


@dataclass(frozen=True)
class DouyinMetadata:
    aweme_id: str
    title: str | None
    author: str | None = None
    author_id: str | None = None
    description: str | None = None
    media_type: str = "video"
    cover_url: str | None = None
    source: str = "none"

    def to_dict(self) -> dict[str, object]:
        return {
            "aweme_id": self.aweme_id,
            "title": self.title,
            "author": self.author,
            "author_id": self.author_id,
            "media_type": self.media_type,
            "cover_url": self.cover_url,
            "parsed_by": self.source,
        }


def _first_line(text: str, limit: int = TITLE_LIMIT) -> str:
    cleaned = " ".join((text or "").split())
    return cleaned[:limit]


def _find_aweme_dict(node: Any) -> dict[str, Any] | None:
    """在任意嵌套结构里找第一个**同时**带 ``aweme_id`` 与 ``desc`` 的字典。"""
    if isinstance(node, dict):
        if "aweme_id" in node and "desc" in node:
            return node
        for value in node.values():
            found = _find_aweme_dict(value)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_aweme_dict(item)
            if found is not None:
                return found
    return None


def parse_metadata(html: str, *, aweme_id: str) -> DouyinMetadata | None:
    """从页面 HTML 里解出元数据。解不出来返回 ``None``（由调用方决定报什么错）。"""
    payload = _extract_embedded_json(html)
    if payload is None:
        return None

    node = _find_aweme_dict(payload)
    if node is None:
        return None
    return _metadata_from_node(node, aweme_id=aweme_id, source="embedded-json")


def detail_api_url(aweme_id: str) -> str:
    """拼详情接口 URL（固定查询串 + ``aweme_id``）。"""
    params = dict(DOUYIN_DETAIL_API_PARAMS)
    params["aweme_id"] = aweme_id
    return f"{DOUYIN_DETAIL_API}?{urlencode(params)}"


def interpret_detail_payload(
    payload: Any, *, aweme_id: str
) -> tuple[DouyinMetadata | None, str | None]:
    """接口回包 → ``(元数据, 未命中原因)``。命中时原因为 ``None``。

    把「回包为什么不能用」也返回出去，是为了让最终的 ``PARSER_UNSUPPORTED`` /
    ``COOKIE_REQUIRED`` 里能带上可定位的上下文 —— 否则线上只看得到一个
    「页面结构已变」，不知道到底是空回包、还是业务码非 0。
    """
    if not isinstance(payload, dict):
        return None, NOTE_NOT_OBJECT
    status = payload.get("status_code")
    if status != DETAIL_STATUS_OK:
        return None, f"{NOTE_STATUS_NOT_OK}:{status}"
    metadata = parse_detail_payload(payload, aweme_id=aweme_id)
    if metadata is None:
        return None, NOTE_NO_DETAIL
    return metadata, None


def parse_detail_payload(payload: Any, *, aweme_id: str) -> DouyinMetadata | None:
    """从详情接口的 JSON 里解出元数据。

    与页面路径的**唯一**差别是「怎么找到那个 ``aweme`` 节点」：接口把它放在
    ``aweme_detail`` 里，不需要在嵌套结构里瞎找。找到之后是同一套字段映射
    （``_metadata_from_node``），所以两条来源的行为天然一致。

    不满足条件一律返回 ``None``（由调用方决定降级还是报错），**不猜**。
    """
    if not isinstance(payload, dict):
        return None
    node = payload.get("aweme_detail")
    if not isinstance(node, dict) or "aweme_id" not in node:
        return None
    return _metadata_from_node(node, aweme_id=aweme_id, source="detail-api")


def _metadata_from_node(node: dict[str, Any], *, aweme_id: str, source: str) -> DouyinMetadata:
    """把 ``aweme`` 节点映射成 ``DouyinMetadata``（两条来源共用）。"""
    desc = node.get("desc")
    title = _first_line(desc) if isinstance(desc, str) and desc.strip() else None

    author_node = node.get("author")
    author = author_node.get("nickname") if isinstance(author_node, dict) else None
    author_id = None
    if isinstance(author_node, dict):
        raw_id = author_node.get("uid") or author_node.get("sec_uid")
        author_id = str(raw_id) if raw_id else None

    images = node.get("images")
    media_type = "image" if isinstance(images, list) and images else "video"

    # 图文帖用第一张图当封面；视频用 video.cover
    cover = None
    if isinstance(images, list) and images:
        cover = _first_url(images[0])
    video_node = node.get("video")
    if cover is None and isinstance(video_node, dict):
        cover = _first_url(video_node.get("cover")) or _first_url(video_node.get("origin_cover"))

    return DouyinMetadata(
        aweme_id=str(node.get("aweme_id") or aweme_id),
        title=title,
        author=author if isinstance(author, str) else None,
        author_id=author_id,
        description=desc if isinstance(desc, str) else None,
        media_type=media_type,
        cover_url=cover,
        source=source,
    )


def _extract_embedded_json(html: str) -> Any | None:
    """``RENDER_DATA``（URL 编码的 JSON）优先，其次 ``window._ROUTER_DATA``。"""
    marker_at = html.find(RENDER_DATA_MARKER)
    if marker_at != -1:
        start = html.find(">", marker_at)
        end = html.find("</script>", start)
        if start != -1 and end != -1:
            raw = html[start + 1 : end].strip()
            for candidate in (unquote(raw), raw):
                loaded = _loads(candidate)
                if loaded is not None:
                    return loaded

    router_at = html.find(ROUTER_DATA_MARKER)
    if router_at != -1:
        brace_at = html.find("{", router_at)
        if brace_at != -1:
            balanced = find_balanced_json(html[brace_at:])
            if balanced:
                loaded = _loads(balanced)
                if loaded is not None:
                    return loaded
    return None


def _loads(text: str) -> Any | None:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _first_url(node: Any) -> str | None:
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        url_list = node.get("url_list")
        if isinstance(url_list, list):
            for item in url_list:
                if isinstance(item, str) and item.startswith("http"):
                    return item
    return None


def classify_page(html: str) -> KnowledgeFlowError | None:
    """页面里出现验证/登录标记时，返回对应的错误（**不尝试绕过**）。"""
    lowered = html.lower()
    if any(marker.lower() in lowered for marker in BLOCK_MARKERS):
        return RequestBlockedError("页面要求完成验证，采集中止（不绕验证）")
    if any(marker.lower() in lowered for marker in LOGIN_MARKERS):
        return CookieRequiredError("页面要求登录态才能查看内容")
    return None


class DouyinSource(ContentSource[str]):
    """抖音采集入口。``fetch(url)`` 返回 ``RawContent``；失败带明确 ``error_type``。"""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        logger: logging.Logger | None = None,
        cookie: str | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(follow_redirects=False)
        self._owns_client = client is None
        self._timeout_seconds = timeout_seconds
        self._max_redirects = max_redirects
        self._logger = logger or logging.getLogger("knowledgeflow.douyin")
        #: 用户**自己**的登录态（第二十九条允许）。不传 = 匿名访问。
        self._cookie = cookie

    @property
    def name(self) -> str:
        return SOURCE_NAME

    @property
    def timeout_seconds(self) -> int:
        return self._timeout_seconds

    # ------------------------------------------------------------------ #
    # 两步：先解析 id，再取元数据
    # ------------------------------------------------------------------ #
    async def resolve(self, url: str) -> ResolvedURL:
        resolved = await resolve_short_link(
            url,
            client=self._client,
            timeout_seconds=self._timeout_seconds,
            max_redirects=self._max_redirects,
            cookie=self._cookie,
        )
        log_event(
            self._logger,
            logging.INFO,
            stage=STAGE,
            message="短链解析完成",
            host=resolved.host,
            redirects=resolved.redirect_count,
            aweme_id=resolved.platform_id or "-",
        )
        if resolved.platform is None:
            raise ParserUnsupportedError(
                f"不支持的平台域名：{resolved.host}", context=resolved.to_dict()
            )
        if not resolved.platform_id:
            raise AwemeIdNotFoundError(
                f"最终 URL 里没有 aweme_id：{resolved.final_url}", context=resolved.to_dict()
            )
        return resolved

    async def fetch_metadata(self, resolved: ResolvedURL) -> DouyinMetadata:
        """取元数据：**先详情接口，再页面内嵌 JSON**。都拿不到就明确报错。

        为什么这个顺序（2026-09-30 真机实测的结论）：

        * 详情接口返回结构化 JSON，一次请求拿全 ``desc`` / 作者 / 封面；
        * 页面已经变成 JS 壳页，服务端不再内嵌任何数据 —— 死守页面解析
          就是「永远 ``PARSER_UNSUPPORTED``」。
        """
        aweme_id = resolved.platform_id or ""

        payload, api_note = await self._get_detail_json(aweme_id)
        if payload is not None:
            metadata, reason = interpret_detail_payload(payload, aweme_id=aweme_id)
            if metadata is not None:
                log_event(
                    self._logger,
                    logging.INFO,
                    stage=STAGE,
                    message="元数据取自详情接口",
                    aweme_id=aweme_id,
                    parsed_by=metadata.source,
                )
                return metadata
            api_note = reason

        metadata, html, html_length = await self._fetch_metadata_from_page(
            resolved, aweme_id=aweme_id
        )
        if metadata is not None:
            return metadata

        # 两条来源都没给出数据。**如实报错**，并说明各自为什么不行。
        context: dict[str, object] = {
            "url": resolved.canonical_url or resolved.final_url,
            "html_length": html_length,
            "aweme_id": aweme_id,
            "detail_api_note": api_note or NOTE_NOT_JSON,
            "cookie_configured": bool((self._cookie or "").strip()),
        }
        if not context["cookie_configured"]:
            # 详情接口对匿名访问返回 200 + 空正文（实测），页面也不再内嵌数据 ——
            # 这时唯一的、可操作的原因是「没有登录态」，报别的都是误导。
            raise CookieRequiredError(
                "详情接口在匿名访问下不返回数据，页面也没有内嵌数据 —— "
                "需要在 .env 里配置 DOUYIN_COOKIE（你自己的登录态，第二十九条允许）",
                context=context,
            )
        raise ParserUnsupportedError(
            "详情接口与页面都没有可解析的数据（页面结构可能已变）",
            context=context,
        )

    async def _fetch_metadata_from_page(
        self, resolved: ResolvedURL, *, aweme_id: str
    ) -> tuple[DouyinMetadata | None, str, int]:
        """页面内嵌 JSON 这条兜底路径。返回 ``(元数据, 页面正文, 正文长度)``。

        页面被判定为「验证页 / 登录页」时直接抛错（与加详情接口之前的行为一致）
        —— 那说明请求已经被挡住，再去解析正文没有意义。
        """
        url = resolved.canonical_url or resolved.final_url
        html = await self._get_html(url)

        blocked = classify_page(html)
        if blocked is not None:
            raise blocked

        return parse_metadata(html, aweme_id=aweme_id), html, len(html)

    async def fetch(self, payload: str) -> RawContent:
        resolved = await self.resolve(self._coerce_payload(payload))
        metadata = await self.fetch_metadata(resolved)
        return self.to_raw_content(resolved, metadata)

    def _coerce_payload(self, payload: str) -> str:
        """把「用户粘进来的东西」变成一个 URL。

        抖音的分享文案是**整段文字**（口令 + 视频文案 + 链接 + 「复制此链接，
        打开Dou音搜索，直接观看视频！」）。只接受纯 URL 会让「粘贴分享文案」
        这个最常见的动作直接 400 ``INVALID_URL`` —— 而这正是用户会做的动作。

        所以先抠链接；抠出来的链接**照样要过**完整的 ``validate_url``
        （scheme + 域名白名单），一步不让。抠不到就原样抛错，但报错要说清楚
        「没在输入里找到链接」，而不是含糊的「格式不合法」。
        """
        candidate = (payload or "").strip()
        if candidate.startswith(("http://", "https://")):
            return candidate
        scheme = urlsplit(candidate).scheme.lower()
        if scheme and scheme not in ("http", "https"):
            # 别的 scheme（ftp:// 之类）：原样交给 validate_url，它会报
            # UNSUPPORTED_URL_SCHEME —— 这比「没找到链接」准确，别抢它的活。
            # 注意必须判「开头的 scheme」而不是 ``"://" in candidate``：
            # 分享文案里本来就含 ``https://``，那样写会把整段文案原样放过去。
            return candidate

        found = extract_first_url(candidate)
        if found is not None:
            log_event(
                self._logger,
                logging.INFO,
                stage=STAGE,
                message="输入是分享文案，已抠出链接",
                input_length=len(candidate),
                url_length=len(found),
            )
            return found

        # 错误消息**不回显整段文案**（第二十四节）：只给长度与明确指引。
        raise URLValidationError(
            "没有在输入里找到 http(s) 链接 —— 抖音分享文案里应当含一个链接，"
            "请确认复制完整（也可以只粘那一段链接）",
            error_type=ErrorType.INVALID_URL,
            context={"input_length": len(candidate)},
        )

    # ------------------------------------------------------------------ #
    # 组装 RawContent
    # ------------------------------------------------------------------ #
    def to_raw_content(self, resolved: ResolvedURL, metadata: DouyinMetadata) -> RawContent:
        return RawContent(
            source=SOURCE_NAME,
            # 第五 / 六节：source_id = aweme_id（**不是** URL）
            source_id=metadata.aweme_id,
            source_url=resolved.canonical_url or resolved.final_url,
            title=metadata.title,
            author=metadata.author,
            author_id=metadata.author_id,
            description=metadata.description,
            media_type=metadata.media_type,  # type: ignore[arg-type]
            media_path=None,
            # Phase 6 只做 URL → metadata：抖音唯一可得的正文文本就是文案（desc）。
            # 不把它塞进 raw_text 的话，A 线会因为「源文本为空」直接 EMPTY_SOURCE_TEXT 失败。
            # 真正的逐字稿由 Phase 7 的 ASR 补进 transcript。
            raw_text=metadata.description,
            transcript=None,
            ocr_text=None,
        )

    # ------------------------------------------------------------------ #
    # HTTP
    # ------------------------------------------------------------------ #
    async def _get(self, url: str, *, accept: str) -> httpx.Response:
        """一次带用户登录态的 GET。**只做传输与状态码判定**，不碰正文语义。

        状态码 → ``error_type`` 的映射对「页面」和「详情接口」是同一套：
        401 需要登录、403/429 被拒、5xx 是取不到、其余 4xx 是没东西可解析。
        """
        try:
            headers = dict(BROWSER_HEADERS)
            headers["Accept"] = accept
            headers.update(with_cookie(self._cookie))
            response = await self._client.get(
                url,
                headers=headers,
                timeout=self._timeout_seconds,
                follow_redirects=True,
            )
        except httpx.TimeoutException as exc:
            raise NetworkTimeoutError(
                f"取页面超时（{self._timeout_seconds}s）", context={"url": url}
            ) from exc
        except httpx.HTTPError as exc:
            raise ShortLinkResolutionError(
                f"取页面失败：{type(exc).__name__}", context={"url": url}
            ) from exc

        if response.status_code in (401,):
            raise CookieRequiredError("取页面要求登录态（HTTP 401）", context={"url": url})
        if response.status_code in (403, 429):
            raise RequestBlockedError(
                f"取页面被拒（HTTP {response.status_code}）",
                context={"url": url, "status_code": response.status_code},
            )
        if response.status_code >= 500:
            # 服务端问题 —— 这是「取不到」，不是「解析不了」
            raise ShortLinkResolutionError(
                f"取页面服务端错误（HTTP {response.status_code}）",
                context={"url": url, "status_code": response.status_code},
            )
        if response.status_code >= 400:
            # 404 之类：页面根本不存在，没什么可解析的
            raise ParserUnsupportedError(
                f"取页面返回 HTTP {response.status_code}",
                context={"url": url, "status_code": response.status_code},
            )
        return response

    async def _get_html(self, url: str) -> str:
        return (await self._get(url, accept=BROWSER_HEADERS["Accept"])).text

    async def _get_detail_json(self, aweme_id: str) -> tuple[Any | None, str | None]:
        """取详情接口的 JSON。返回 ``(payload, 未命中原因)``。

        **HTTP 层的硬错误照抛不误**（401/403/429/5xx/4xx 的语义与取页面一致）；
        只有「HTTP 200 但正文用不了」才走 ``(None, 原因)`` 让调用方降级 ——
        匿名访问恰好就是这个形态：200 + 长度 0 的正文。
        """
        if not aweme_id:
            return None, NOTE_NO_DETAIL
        url = detail_api_url(aweme_id)
        response = await self._get(url, accept=JSON_ACCEPT)

        body = response.content
        if not body:
            return None, NOTE_EMPTY_BODY
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None, NOTE_NOT_JSON
        return payload, None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "DouyinSource":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()


__all__ = [
    "DouyinSource",
    "DouyinMetadata",
    "parse_metadata",
    "parse_detail_payload",
    "interpret_detail_payload",
    "detail_api_url",
    "classify_page",
    "SOURCE_NAME",
    "STAGE",
    "TITLE_LIMIT",
    "BLOCK_MARKERS",
    "LOGIN_MARKERS",
    "RENDER_DATA_MARKER",
    "ROUTER_DATA_MARKER",
    "DOUYIN_DETAIL_API",
    "DOUYIN_DETAIL_API_PARAMS",
    "JSON_ACCEPT",
    "DETAIL_STATUS_OK",
    "NOTE_EMPTY_BODY",
    "NOTE_NOT_JSON",
    "NOTE_NOT_OBJECT",
    "NOTE_STATUS_NOT_OK",
    "NOTE_NO_DETAIL",
]
