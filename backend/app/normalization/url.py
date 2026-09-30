"""URL 基础校验（定稿文档第六节的 Phase 1 部分）。

Phase 1 **不实现** ``resolve_short_link()`` 的真实网络请求，只做：

1. 校验 scheme（仅 ``http`` / ``https``）
2. 域名白名单
3. 基础格式校验与规范化（scheme/host 小写、去默认端口）

超时、最大重定向次数、aweme_id 解析属于 Phase 6。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Iterable
from urllib.parse import urlsplit

from app.errors import ErrorType, URLValidationError

#: 默认域名白名单（按后缀匹配，``www.`` 等子域自动放行）。
DEFAULT_ALLOWED_HOSTS: Final[tuple[str, ...]] = (
    "douyin.com",
    "iesdouyin.com",
    "bilibili.com",
    "b23.tv",
    "youtube.com",
    "youtu.be",
    "x.com",
    "twitter.com",
    "github.com",
    "xiaohongshu.com",
    "xhslink.com",
    "mp.weixin.qq.com",
)

ALLOWED_SCHEMES: Final[tuple[str, ...]] = ("http", "https")

#: URL 里**允许**出现的字符（RFC 3986 的 unreserved + reserved + ``%``）。
#: 非 ASCII 一律不算 —— 合法 URL 里的中文必须是 percent-encoded。
#: 这个字符集就是「链接在哪里结束」的边界：抖音文案里链接后面常紧跟中文，
#: 用 ``\S+`` 会把「复制此链接」也吞进去。
_URL_CHARS: Final[str] = r"A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%"
URL_PATTERN: Final[re.Pattern[str]] = re.compile(rf"https?://[{_URL_CHARS}]+")
#: 句末标点：抠出来的链接尾巴上可能挂着它们，但那不是链接的一部分。
#: 只列「几乎不可能作为 URL 有效结尾」的那些（``:`` 要留着 —— 端口用它）。
_TRAILING_PUNCTUATION: Final[str] = ".,;'\"、。，、；！？…）)】」』》"


@dataclass(frozen=True)
class ValidatedURL:
    scheme: str
    host: str
    path: str
    query: str
    original: str

    @property
    def normalized(self) -> str:
        base = f"{self.scheme}://{self.host}{self.path}"
        return f"{base}?{self.query}" if self.query else base


def host_allowed(host: str, allowed_hosts: Iterable[str]) -> bool:
    host = host.strip().lower().rstrip(".")
    for allowed in allowed_hosts:
        allowed = allowed.strip().lower().rstrip(".")
        if host == allowed or host.endswith("." + allowed):
            return True
    return False


def validate_url(
    url: str,
    *,
    allowed_hosts: Iterable[str] | None = None,
) -> ValidatedURL:
    """校验并规范化一个 URL。失败一律抛 ``URLValidationError``。"""
    candidate = (url or "").strip()
    if not candidate:
        raise URLValidationError("URL 为空", error_type=ErrorType.INVALID_URL)

    parts = urlsplit(candidate)
    scheme = parts.scheme.lower()

    # 1) scheme 先判：非 http/https 一律 UNSUPPORTED_URL_SCHEME
    #    （``file:///etc/passwd`` 这类没有 netloc 的恶意 scheme 也要落到这一支）
    if not scheme:
        raise URLValidationError(f"URL 格式不合法：{candidate!r}", error_type=ErrorType.INVALID_URL)
    if scheme not in ALLOWED_SCHEMES:
        raise URLValidationError(
            f"仅允许 http/https，收到 {scheme!r}",
            error_type=ErrorType.UNSUPPORTED_URL_SCHEME,
            context={"scheme": scheme},
        )

    # 2) 必须有 host
    if not parts.netloc:
        raise URLValidationError(f"URL 缺少主机名：{candidate!r}", error_type=ErrorType.INVALID_URL)

    host = (parts.hostname or "").lower()
    if not host:
        raise URLValidationError(f"URL 缺少主机名：{candidate!r}", error_type=ErrorType.INVALID_URL)

    allowed = tuple(allowed_hosts) if allowed_hosts is not None else DEFAULT_ALLOWED_HOSTS
    if not host_allowed(host, allowed):
        raise URLValidationError(
            f"域名不在白名单内：{host}",
            error_type=ErrorType.DOMAIN_NOT_ALLOWED,
            context={"host": host},
        )

    return ValidatedURL(
        scheme=scheme,
        host=host,
        path=parts.path or "",
        query=parts.query or "",
        original=candidate,
    )


def extract_first_url(text: str) -> str | None:
    """从一段文字里抠出**第一个** ``http(s)`` 链接；抠不到返回 ``None``。

    为什么需要它：抖音「复制链接」给的不是纯链接，而是**整段分享文案**
    ——口令、视频文案、链接、再加一句「复制此链接，打开Dou音搜索，直接观看视频！」。
    用户把它整段粘进输入框是最自然的动作，只接受纯 URL 会让这个动作直接 400。

    这里**只做「取出链接」这一件事**，边界刻意保守：

    * 只认 ``http`` / ``https``，字符集严格按 RFC 3986（非 ASCII 不算），
      所以链接后面紧跟的中文不会被吞进来；
    * 抠出来的链接**照样要过** :func:`validate_url`（scheme + 域名白名单），
      一步不让 —— 提取不削弱任何校验，只是省掉用户手动删文案这一步。

    **不猜**：一段文字里有多个链接时只取第一个（调用方会把这个决定记进日志）。
    """
    if not text:
        return None
    match = URL_PATTERN.search(text)
    if match is None:
        return None
    return match.group(0).rstrip(_TRAILING_PUNCTUATION) or None


def is_valid_url(url: str, *, allowed_hosts: Iterable[str] | None = None) -> bool:
    try:
        validate_url(url, allowed_hosts=allowed_hosts)
    except URLValidationError:
        return False
    return True


__all__ = [
    "DEFAULT_ALLOWED_HOSTS",
    "ALLOWED_SCHEMES",
    "URL_PATTERN",
    "ValidatedURL",
    "extract_first_url",
    "host_allowed",
    "validate_url",
    "is_valid_url",
]
