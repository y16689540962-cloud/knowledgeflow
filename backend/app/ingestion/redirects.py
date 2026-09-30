"""``resolve_short_link``：URL 规范化 + 平台 id 解析（定稿文档第六节）。

七条要求逐条落地：

1. 验证 URL scheme（仅 ``http`` / ``https``）
2. 限制允许的域名白名单
3. 设置连接超时
4. 设置最大重定向次数
5. 记录最终 URL
6. 尝试解析平台唯一 ID（抖音为 ``aweme_id``）
7. **解析失败不得伪造 source_id** —— 由调用方走第五节的 ``hash:`` fallback

合规边界（定稿第二十九条）：只做公开访问的 GET/HEAD。
**不绕验证码、不破签名、不窃取 Cookie、不绕过账号权限。**
被平台挡住时如实抛出对应 ``error_type``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Iterable
from urllib.parse import urljoin, urlsplit

import httpx

from app.errors import ErrorType, KnowledgeFlowError
from app.normalization.url import DEFAULT_ALLOWED_HOSTS, validate_url

#: 单次请求超时（秒）。``.env.example`` 是固定 17 个键，所以走构造参数。
DEFAULT_TIMEOUT_SECONDS: Final[int] = 10
#: 最大重定向次数（超过即 SHORT_LINK_RESOLUTION_FAILED）。
DEFAULT_MAX_REDIRECTS: Final[int] = 5

REDIRECT_STATUSES: Final[frozenset[int]] = frozenset({301, 302, 303, 307, 308})
#: 平台标识。
PLATFORM_DOUYIN: Final[str] = "douyin"

#: 抖音「视频 / 图文」唯一 ID 的常见 URL 形态。
#: 全部要求 6 位以上数字，避免把 ``/video/list`` 这类路径误当成 id。
AWEME_ID_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"/(?:video|note|slides|share/video|share/note)/(\d{6,})"),
    re.compile(r"[?&]modal_id=(\d{6,})"),
    re.compile(r"[?&]aweme_id=(\d{6,})"),
    re.compile(r"^(\d{6,})$"),
)

#: 判定「打开的是登录/验证页」而不是内容页。
LOGIN_HOSTS: Final[tuple[str, ...]] = ("passport.douyin.com", "sso.douyin.com")
LOGIN_PATH_MARKERS: Final[tuple[str, ...]] = ("/login", "/passport", "/verify")

BROWSER_HEADERS: Final[dict[str, str]] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def with_cookie(cookie: str | None) -> dict[str, str]:
    """把用户**自己的** Cookie 并进请求头（第二十九条允许：自己的合法登录态）。

    合规边界：这里只接受用户显式传入的值 —— **不窃取、不伪造、不绕过验证码**。
    Cookie 是敏感串，因此：

    * 为空就返回空 dict（不塞一个空的 ``Cookie`` 头，那会让服务端以为有会话）；
    * 值**永不进日志**（日志脱敏，第二十四节）。
    """
    if cookie is None:
        return {}
    stripped = cookie.strip()
    return {"Cookie": stripped} if stripped else {}


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #
class ShortLinkResolutionError(KnowledgeFlowError):
    error_type = ErrorType.SHORT_LINK_RESOLUTION_FAILED
    default_message = "短链解析失败"


class AwemeIdNotFoundError(KnowledgeFlowError):
    error_type = ErrorType.AWEME_ID_NOT_FOUND
    default_message = "最终 URL 里没有抖音唯一 ID"


class RequestBlockedError(KnowledgeFlowError):
    error_type = ErrorType.REQUEST_BLOCKED
    default_message = "请求被平台拒绝"


class CookieRequiredError(KnowledgeFlowError):
    error_type = ErrorType.COOKIE_REQUIRED
    default_message = "平台要求登录态"


class ParserUnsupportedError(KnowledgeFlowError):
    error_type = ErrorType.PARSER_UNSUPPORTED
    default_message = "不支持的 URL 形态"


class NetworkTimeoutError(KnowledgeFlowError):
    error_type = ErrorType.NETWORK_TIMEOUT
    default_message = "请求超时"


# --------------------------------------------------------------------------- #
# 结果
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ResolvedURL:
    original: str
    final_url: str
    host: str
    scheme: str
    status_code: int
    redirect_count: int
    method: str
    platform: str | None = None
    platform_id: str | None = None

    @property
    def canonical_url(self) -> str | None:
        """抖音视频的规范 URL（拿不到 id 就是 ``None``，**绝不猜**）。"""
        if self.platform == PLATFORM_DOUYIN and self.platform_id:
            return f"https://www.douyin.com/video/{self.platform_id}"
        return None

    def to_dict(self) -> dict[str, object]:
        return {
            "original": self.original,
            "final_url": self.final_url,
            "host": self.host,
            "status_code": self.status_code,
            "redirect_count": self.redirect_count,
            "method": self.method,
            "platform": self.platform,
            "platform_id": self.platform_id,
            "canonical_url": self.canonical_url,
        }


def extract_aweme_id(url: str) -> str | None:
    """从 URL 里抠出抖音唯一 ID（``aweme_id``）。抠不到返回 ``None``，不编。"""
    if not url:
        return None
    candidate = url.strip()
    for pattern in AWEME_ID_PATTERNS:
        match = pattern.search(candidate)
        if match:
            return match.group(1)
    return None


def platform_of(host: str) -> str | None:
    host = (host or "").lower()
    if host.endswith("douyin.com") or host.endswith("iesdouyin.com"):
        return PLATFORM_DOUYIN
    return None


def _looks_like_login(host: str, path: str) -> bool:
    if any(host.endswith(candidate) for candidate in LOGIN_HOSTS):
        return True
    lowered = (path or "").lower()
    return any(marker in lowered for marker in LOGIN_PATH_MARKERS)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
async def resolve_short_link(
    url: str,
    *,
    client: httpx.AsyncClient,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    allowed_hosts: Iterable[str] | None = None,
    cookie: str | None = None,
) -> ResolvedURL:
    """把短链解析成最终 URL，并尝试解出平台唯一 ID。

    **手动跟随重定向**（不用 ``follow_redirects=True``），这样才能
    逐跳校验域名白名单、并如实记录跳了几次。

    ``cookie`` 只接受用户**自己提供**的登录态（第二十九条）。不传就是原来的
    匿名行为 —— 抖音大概率返回验证页，如实报 ``REQUEST_BLOCKED``。
    """
    allowed = tuple(allowed_hosts) if allowed_hosts is not None else DEFAULT_ALLOWED_HOSTS
    validated = validate_url(url, allowed_hosts=allowed)

    current = validated.normalized
    method = "HEAD"
    redirect_count = 0
    response: httpx.Response | None = None

    while True:
        response = await _request(
            client, method, current, timeout_seconds=timeout_seconds, cookie=cookie
        )

        if method == "HEAD" and response.status_code >= 400:
            # HEAD 只是优化，**权威答案以 GET 为准**。
            # 实测：抖音首页 HEAD 返回 404，同一 URL 的 GET 返回 200 ——
            # 只针对 403/405/501 回退是不够的，干脆「HEAD 只要不是成功就换 GET 一次」。
            method = "GET"
            continue

        location = response.headers.get("location")
        if response.status_code in REDIRECT_STATUSES:
            if not location:
                raise ShortLinkResolutionError(
                    "重定向响应缺少 Location 头",
                    context={"url": current, "status_code": response.status_code},
                )
            redirect_count += 1
            if redirect_count > max_redirects:
                raise ShortLinkResolutionError(
                    f"重定向次数超过上限（{max_redirects}）",
                    context={"url": validated.normalized, "redirect_count": redirect_count},
                )
            target = urljoin(current, location)
            target_parts = urlsplit(target)
            target_host = (target_parts.hostname or "").lower()
            if _looks_like_login(target_host, target_parts.path):
                # 跳到登录页 ≠ 解析失败，是「需要登录态」（第二十二节）
                raise CookieRequiredError(
                    "平台要求登录态（被重定向到登录页）",
                    context={"url": url, "login_url": target},
                )
            _ensure_host_allowed(target, allowed, original=url)
            current = target
            continue

        if response.status_code in (401,):
            raise CookieRequiredError(
                "平台要求登录态（HTTP 401）", context={"url": current}
            )
        if response.status_code in (403, 429):
            raise RequestBlockedError(
                f"请求被平台拒绝（HTTP {response.status_code}）",
                context={"url": current, "status_code": response.status_code},
            )
        if response.status_code >= 400:
            raise ShortLinkResolutionError(
                f"最终请求返回 HTTP {response.status_code}",
                context={"url": current, "status_code": response.status_code},
            )
        break

    parts = urlsplit(current)
    host = (parts.hostname or "").lower()
    if _looks_like_login(host, parts.path):
        raise CookieRequiredError(
            "最终落在登录页，说明需要登录态",
            context={"url": url, "final_url": current},
        )
    platform = platform_of(host)
    platform_id = extract_aweme_id(current) if platform == PLATFORM_DOUYIN else None

    return ResolvedURL(
        original=url,
        final_url=current,
        host=host,
        scheme=parts.scheme.lower(),
        status_code=response.status_code,
        redirect_count=redirect_count,
        method=method,
        platform=platform,
        platform_id=platform_id,
    )


def _ensure_host_allowed(target: str, allowed: tuple[str, ...], *, original: str) -> None:
    """跳转目标也必须落在白名单里 —— 不跟着跑到别的地方去。"""
    from app.normalization.url import host_allowed

    host = (urlsplit(target).hostname or "").lower()
    if not host_allowed(host, allowed):
        raise ShortLinkResolutionError(
            f"重定向目标不在允许的域名内：{host}",
            context={"url": original, "blocked_host": host},
        )


async def _request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    timeout_seconds: int,
    cookie: str | None = None,
) -> httpx.Response:
    headers = dict(BROWSER_HEADERS)
    headers.update(with_cookie(cookie))
    if method == "GET":
        # 只要头，不要正文；有些服务器会忽略 Range，但代价很小。
        headers["Range"] = "bytes=0-1"
    try:
        return await client.request(
            method,
            url,
            headers=headers,
            timeout=timeout_seconds,
            follow_redirects=False,
        )
    except httpx.TimeoutException as exc:
        raise NetworkTimeoutError(
            f"请求超时（{timeout_seconds}s）", context={"url": url}
        ) from exc
    except httpx.HTTPError as exc:
        raise ShortLinkResolutionError(
            f"传输层失败：{type(exc).__name__}", context={"url": url}
        ) from exc


__all__ = [
    "resolve_short_link",
    "with_cookie",
    "extract_aweme_id",
    "platform_of",
    "ResolvedURL",
    "ShortLinkResolutionError",
    "AwemeIdNotFoundError",
    "RequestBlockedError",
    "CookieRequiredError",
    "ParserUnsupportedError",
    "NetworkTimeoutError",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_MAX_REDIRECTS",
    "REDIRECT_STATUSES",
    "PLATFORM_DOUYIN",
    "AWEME_ID_PATTERNS",
    "LOGIN_HOSTS",
    "LOGIN_PATH_MARKERS",
    "BROWSER_HEADERS",
]
