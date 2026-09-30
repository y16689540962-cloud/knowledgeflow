"""``LocalMediaSource``：本地媒体文件 → ``RawContent``（定稿第二 / 七节）。

为什么需要它
------------

Phase 7 交付了媒体下载与 ASR/OCR，但那条链**只存在于 demo 脚本里**：
``scripts/demo_capabilities.py --file <本地文件> --process`` 手动把能力层跑完再喂给 pipeline。
服务侧（API）没有任何入口能把「一个视频/音频/图片文件」变成一条笔记。

于是产生一个真实的能力落差：抖音 URL 进来时只能分析文案（``desc``，几十字），
而本机明明装着 Whisper 与 Tesseract。这个模块补的就是这一段：

```text
本地文件 → 嗅探类型 → RawContent（带 media_path）
        → MediaCapabilityService.enrich()（ASR / OCR）
        → Pipeline.process()
```

设计取舍
--------

* **不复制文件**：文件已经在本机磁盘上，就地引用即可。复制一份既费磁盘，
  又会让「用户删掉原文件后笔记里的 media_path 变成死链」这件事更难解释。
* **类型靠嗅探，不靠扩展名**：``.mp4`` 后缀的验证页 HTML 是真实存在过的坑
  （见 ``app/media/downloader.py`` 的 Q6 记录）。嗅不出来就报
  ``MEDIA_TYPE_UNSUPPORTED``，不猜。
* **不做 ASR/OCR**：那是 :class:`MediaCapabilityService` 的职责，本类只负责
  「文件 → 一条可信的 RawContent」。分开之后，两条路径（下载来的 / 本地已有的）
  能共用同一段能力编排代码。
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, field_validator

from app.errors import ErrorType, KnowledgeFlowError
from app.ingestion.base import ContentSource
from app.media.downloader import sniff_content_type
from app.schemas.content import RawContent
from app.schemas.enums import MediaType

SOURCE_NAME: Final[str] = "media"

#: 嗅探要读的首部长度（与下载器保持一致）。
_MAGIC_READ_BYTES: Final[int] = 64

#: Content-Type 前缀 → ``media_type``。与下载器的 ``_FAMILY_PREFIX`` 是同一套口径。
_FAMILY_PREFIX: Final[tuple[tuple[str, str], ...]] = (
    ("video/", "video"),
    ("audio/", "audio"),
    ("image/", "image"),
)


class MediaFileNotFoundError(KnowledgeFlowError):
    error_type = ErrorType.MEDIA_FILE_NOT_FOUND
    default_message = "本地媒体文件不存在"


class MediaTypeUnsupportedError(KnowledgeFlowError):
    error_type = ErrorType.MEDIA_TYPE_UNSUPPORTED
    default_message = "无法从文件内容判断媒体类型"


class LocalMediaPayload(BaseModel):
    """用户指定的一个本地媒体文件（外加可选的元数据）。"""

    model_config = ConfigDict(extra="forbid")

    file_path: str
    title: str | None = None
    author: str | None = None
    description: str | None = None
    #: 用户自己知道的补充文本（例如视频文案）。给了就能在没有 ASR 时也跑通 A 线。
    raw_text: str | None = None
    source_url: str = ""

    @field_validator("source_url", mode="before")
    @classmethod
    def _none_to_empty(cls, value: object) -> object:
        return "" if value is None else value


def family_of(content_type: str) -> MediaType | None:
    """``image/png`` → ``image``；认不出返回 ``None``。"""
    for prefix, family in _FAMILY_PREFIX:
        if content_type.startswith(prefix):
            return family  # type: ignore[return-value]
    return None


def sniff_media_type(path: Path) -> MediaType | None:
    """读首部字节判断媒体类型。读不出来（含空文件）返回 ``None``。"""
    try:
        with path.open("rb") as handle:
            head = handle.read(_MAGIC_READ_BYTES)
    except OSError:
        return None
    if not head:
        return None
    content_type = sniff_content_type(head)
    if content_type is None:
        return None
    return family_of(content_type)


class LocalMediaSource(ContentSource[LocalMediaPayload]):
    """与 ``ManualPasteSource`` / ``DouyinSource`` 平级的第三个 ``ContentSource``。"""

    @property
    def name(self) -> str:
        return SOURCE_NAME

    def resolve_path(self, value: str) -> Path:
        """把用户给的路径解析成绝对路径，并确认它真的是个文件。"""
        candidate = Path(value).expanduser()
        try:
            resolved = candidate.resolve()
        except OSError as exc:  # pragma: no cover - 极端路径
            raise MediaFileNotFoundError(
                "路径无法解析", context={"file_path": value}
            ) from exc

        if not resolved.exists():
            raise MediaFileNotFoundError(
                "文件不存在", context={"file_path": str(resolved)}
            )
        if not resolved.is_file():
            raise MediaFileNotFoundError(
                "不是一个文件（目录？）", context={"file_path": str(resolved)}
            )
        return resolved

    async def fetch(self, payload: LocalMediaPayload) -> RawContent:
        path = self.resolve_path(payload.file_path)

        media_type = sniff_media_type(path)
        if media_type is None:
            raise MediaTypeUnsupportedError(
                "读不出媒体类型（空文件或未知格式）",
                context={"file_path": str(path), "suffix": path.suffix},
            )

        return RawContent(
            source=SOURCE_NAME,
            # 留空 → normalize 阶段填 ``hash:<content_hash[:16]>``（第五节 fallback）。
            # 本地文件没有平台 id，这正是 fallback 存在的意义。
            source_id="",
            source_url=payload.source_url or "",
            title=payload.title,
            author=payload.author,
            description=payload.description,
            media_type=media_type,
            media_path=str(path),
            raw_text=payload.raw_text,
            transcript=None,
            ocr_text=None,
        )


__all__ = [
    "SOURCE_NAME",
    "LocalMediaPayload",
    "LocalMediaSource",
    "MediaFileNotFoundError",
    "MediaTypeUnsupportedError",
    "family_of",
    "sniff_media_type",
]
