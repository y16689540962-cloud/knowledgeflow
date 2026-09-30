"""HTTP 流式媒体下载器。

三个容易写错的地方，这里都规避了：

1. **体积上限必须在流式读取中判定。** 先读 ``response.content`` 再判断大小，
   等于「先把 2GB 读进内存，再告诉自己它太大了」。这里逐 chunk 累加，
   超限立刻收手，后续 chunk 不再读。
2. **不能只信 ``Content-Type``。** 服务端给的头想写啥就写啥（验证页也返回 200）。
   这里**还要嗅魔数**（magic bytes），和实际形态不一致就按不受支持处理。
3. **原子写。** 半截文件不能被别的进程当成完整文件读走 ——
   和 Obsidian Writer 一样 ``mkstemp`` + ``fsync`` + ``os.replace``。

文件名 = URL 的 SHA-256 前 16 位 + 嗅探出的扩展名：
重复下载天然幂等，也不把平台给的、可能带奇怪字符的文件名落到磁盘上。
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

import httpx

from app.errors import (
    MediaDownloadError,
    MediaTooLargeError,
    MediaTypeUnsupportedError,
)
from app.media.base import DownloadedMedia, MediaDownloader
from app.schemas.enums import MediaType

DEFAULT_MAX_BYTES: int = 200 * 1024 * 1024  # 200 MB
DEFAULT_CHUNK_BYTES: int = 64 * 1024
DEFAULT_TIMEOUT_SECONDS: int = 30

#: 嗅探所需的最小首部长度（够放下所有已知魔数）。
_MAGIC_READ_BYTES: int = 64

#: (Content-Type, 偏移 0 处的魔数, 扩展名) —— **仅用于开头即确定的格式**。
#: 容器型格式（RIFF / ISO-BMFF）的魔数不在偏移 0，由 :func:`sniff_content_type` 单独处理，
#: 放进这张表会得到「永远命中不了」的死数据，反而误导后人。
_MAGIC: tuple[tuple[str, bytes, str], ...] = (
    ("image/jpeg", b"\xff\xd8\xff", ".jpg"),
    ("image/png", b"\x89PNG\r\n\x1a\n", ".png"),
    ("image/gif", b"GIF8", ".gif"),
    ("audio/mpeg", b"ID3", ".mp3"),
)

#: 容器型格式的扩展名（key 是 :func:`sniff_content_type` 的返回值）。
_CONTAINER_EXT: dict[str, str] = {
    "audio/wav": ".wav",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "audio/mp4": ".m4a",
}

_EXT_BY_TYPE: dict[str, str] = {**{m[0]: m[2] for m in _MAGIC}, **_CONTAINER_EXT}

#: ``media_type`` → 允许的 Content-Type 前缀。
_FAMILY_PREFIX: dict[str, str] = {
    "video": "video/",
    "audio": "audio/",
    "image": "image/",
}


def sniff_content_type(head: bytes) -> str | None:
    """按实际字节猜 Content-Type，猜不出返回 ``None``。

    ISO Base Media（mp4 / m4a）的前 4 字节是长度不固定的 box size，
    真正的类型写在紧跟其后的 ``ftyp`` box 里，所以只能查找 ``ftyp`` 再读 brand，
    不能按固定偏移匹配。视频与音频的差异靠 brand 区分（``M4A `` 是音频）。
    """
    # RIFF 容器被三种格式共用，靠偏移 8 处的 fourcc 才能分开：
    # "WAVE" = wav、"WEBP" = webp —— 只匹配开头的 RIFF 会把 wav 认成 webp。
    if head.startswith(b"RIFF"):
        fourcc = head[8:12]
        if fourcc == b"WAVE":
            return "audio/wav"
        if fourcc == b"WEBP":
            return "image/webp"
        return None

    if b"ftyp" in head[:64] and head.find(b"ftyp") <= 32:
        brand = head[head.find(b"ftyp") + 4 : head.find(b"ftyp") + 12]
        if brand.startswith(b"M4A"):
            return "audio/mp4"
        return "video/mp4"

    for content_type, magic, _ext in _MAGIC:
        if head.startswith(magic):
            return content_type
    return None


def ensure_within_directory(base: Path, target: Path) -> Path:
    """确认 ``target`` 在 ``base`` 之内。

    Obsidian 那边叫 ``ensure_within_vault``，逻辑完全一样但根目录不同，
    所以写成接受参数的版本，避免两处路径安全实现漂移。
    """
    base_resolved = base.resolve()
    target_resolved = target.resolve()
    if target_resolved != base_resolved and base_resolved not in target_resolved.parents:
        raise MediaDownloadError(
            "下载目标路径不在允许的目录之内",
            context={"base": str(base_resolved), "target": str(target_resolved)},
        )
    return target_resolved


class HttpMediaDownloader:
    """把远端媒体流式取到本地目录。"""

    def __init__(
        self,
        download_dir: Path,
        *,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._download_dir = Path(download_dir)
        self._timeout_seconds = timeout_seconds
        self._max_bytes = max_bytes
        self._client = client
        self._owns_client = client is None

    @property
    def download_dir(self) -> Path:
        return self._download_dir

    async def download(self, url: str, *, media_type: MediaType) -> DownloadedMedia:
        if not httpx.URL(url).host:
            raise MediaDownloadError("URL 不合法（缺少 host）", context={"url": url})

        self._download_dir.mkdir(parents=True, exist_ok=True)

        client = self._client or httpx.AsyncClient(
            follow_redirects=True, timeout=self._timeout_seconds
        )
        try:
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise MediaDownloadError(
                        f"下载失败（HTTP {response.status_code}）",
                        context={"url": url, "status_code": response.status_code},
                    )
                declared = response.headers.get("content-type", "").split(";")[0].strip().lower()
                return await self._save(response, url=url, media_type=media_type, declared=declared)
        except httpx.TimeoutException as exc:
            raise MediaDownloadError(
                f"下载超时（{self._timeout_seconds}s）",
                context={"url": url, "timeout_seconds": self._timeout_seconds},
            ) from exc
        except httpx.HTTPError as exc:
            raise MediaDownloadError(
                f"下载失败：{type(exc).__name__}", context={"url": url}
            ) from exc
        finally:
            if self._owns_client and client is not None:
                await client.aclose()

    async def _save(
        self,
        response: httpx.Response,
        *,
        url: str,
        media_type: MediaType,
        declared: str,
    ) -> DownloadedMedia:
        # 只创建**一次**迭代器：第二次调用 ``aiter_bytes()`` 会得到一个新迭代器，
        # 某些传输（尤其是 Mock 的、已缓冲的响应）会从头再喂一遍 —— 结果就是
        # 同一段正文被写进文件两次，文件体积翻倍而 CRC 全错。真实网络流的行为
        # 还不一样，属于那种「测试绿、生产坏」的坑。这里复用同一个 stream。
        stream = response.aiter_bytes()

        buffered = b""
        async for chunk in stream:
            buffered += chunk
            self._check_limit(len(buffered), url=url)
            if len(buffered) >= _MAGIC_READ_BYTES:
                break
        head = buffered[:_MAGIC_READ_BYTES]

        actual = sniff_content_type(head) or declared
        self._require_supported(media_type, actual, url=url, declared=declared)

        extension = _EXT_BY_TYPE.get(actual, ".bin")
        target = ensure_within_directory(
            self._download_dir, self._download_dir / f"{self._name_for(url)}{extension}"
        )

        written = 0
        fd, raw = tempfile.mkstemp(dir=str(self._download_dir), prefix="dl-")
        os.close(fd)
        tmp = Path(raw)
        try:
            with tmp.open("wb") as handle:
                # 写**全部**已缓冲的字节，不能只写 head —— 首个 chunk 通常远大于 64 字节，
                # 只写前 64 字节会静默丢掉文件开头的几百字节。
                handle.write(buffered)
                written = len(buffered)

                async for chunk in stream:
                    written += len(chunk)
                    self._check_limit(written, url=url)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        finally:
            if tmp.exists():  # 中途超限或异常时不留半截文件
                tmp.unlink(missing_ok=True)

        return DownloadedMedia(
            path=target,
            media_type=media_type,
            byte_size=written,
            content_type=actual,
            source_url=url,
        )

    def _check_limit(self, written: int, *, url: str) -> None:
        if written > self._max_bytes:
            raise MediaTooLargeError(
                "媒体文件超过体积上限，已停止下载",
                context={"url": url, "max_bytes": self._max_bytes},
            )

    def _require_supported(
        self, media_type: MediaType, actual: str, *, url: str, declared: str
    ) -> None:
        prefix = _FAMILY_PREFIX.get(media_type)
        if prefix is None:
            # text / article / mixed 本来就没有要下载的媒体文件
            raise MediaTypeUnsupportedError(
                f"media_type={media_type!r} 不需要下载媒体",
                context={"url": url, "media_type": media_type},
            )
        if not actual.startswith(prefix):
            raise MediaTypeUnsupportedError(
                "实际媒体形态与声明的 media_type 不符",
                context={
                    "url": url,
                    "declared_type": declared,
                    "actual_type": actual,
                    "media_type": media_type,
                },
            )

    @staticmethod
    def _name_for(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "DEFAULT_CHUNK_BYTES",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "HttpMediaDownloader",
    "ensure_within_directory",
    "sniff_content_type",
]
