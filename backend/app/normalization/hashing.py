"""``content_hash`` —— 稳定哈希（定稿文档第七节，强制）。

输入白名单（**只有这五项**）::

    source / title / author / description / raw_text

五项归一化后用 ``\\x1f`` 连接，取 SHA-256。

明确排除：``source_id``（会造成循环依赖）、``source_url``、``transcript``、
``ocr_text``（依赖 ASR/OCR，模型不同结果会漂移，导致去重失效）、时间戳、状态、LLM 输出。

为什么白名单里有 ``raw_text``
----------------------------
定稿第七节原文只列了四项，``raw_text`` 既没进白名单、也没进排除清单 —— 是一处漏写。
排除清单的**理由**只有两条：漂移（``transcript`` / ``ocr_text`` 依赖 ASR/OCR，
模型不同结果会变）和循环依赖（``source_id``）。``raw_text`` 是原始文本，
既不漂移也不循环，所以按第七节的**意图**它应当参与。

不参与的后果已被实测确认：手贴入口的 ``title/author/description`` 通常全空，
于是所有不带标题的粘贴算出**同一个**哈希、同一个 ``source_id`` fallback，
第二条起全部被静默判为 duplicate 吞掉 —— 直接违反第三十二节
「重复输入 → 不产生重复内容」（它要求的是「不同的内容不许被合并」）。

按第七节「未来若修改哈希算法，递增版本号，旧数据不重算」，本次 ``1 -> 2``。
"""

from __future__ import annotations

import hashlib
from typing import Final

from app.normalization.text import normalize_text

#: 哈希算法版本。未来若改算法，递增此值；旧数据不重算。
#: ``2`` = 白名单纳入 ``raw_text``（v1 只吃四项，无标题内容会算出同一个哈希）。
CONTENT_HASH_VERSION: Final[int] = 2

#: 字段分隔符（unit separator，正常文本里不会出现）。
HASH_SEPARATOR: Final[str] = "\x1f"

#: 允许参与哈希的字段，顺序即拼接顺序。
HASH_INPUT_FIELDS: Final[tuple[str, ...]] = (
    "source",
    "title",
    "author",
    "description",
    "raw_text",
)

#: 明确禁止参与哈希的字段（本常量用于测试与文档对照）。
FORBIDDEN_HASH_FIELDS: Final[tuple[str, ...]] = (
    "source_id",
    "source_url",
    "id",
    "created_at",
    "processed_at",
    "status",
    "error_type",
    "error_message",
    "transcript",
    "ocr_text",
    "media_type",
    "media_path",
    "content_hash",
    "content_hash_version",
    "needs_manual_review",
    "current_analysis_id",
)


def build_hash_payload(
    *,
    source: str | None,
    title: str | None,
    author: str | None,
    description: str | None,
    raw_text: str | None = None,
) -> str:
    """构造待哈希字符串（去掉分隔符后仍保持字段顺序）。"""
    parts = [
        normalize_text(source),
        normalize_text(title),
        normalize_text(author),
        normalize_text(description),
        normalize_text(raw_text),
    ]
    return HASH_SEPARATOR.join(parts)


def compute_content_hash(
    *,
    source: str | None,
    title: str | None = None,
    author: str | None = None,
    description: str | None = None,
    raw_text: str | None = None,
) -> str:
    """计算 content_hash v2。签名即白名单：不存在第 6 个参数。"""
    payload = build_hash_payload(
        source=source,
        title=title,
        author=author,
        description=description,
        raw_text=raw_text,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "CONTENT_HASH_VERSION",
    "HASH_SEPARATOR",
    "HASH_INPUT_FIELDS",
    "FORBIDDEN_HASH_FIELDS",
    "build_hash_payload",
    "compute_content_hash",
]
