"""通用小工具：ID 生成与 UTC 时间。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4


def new_id() -> str:
    """32 位 hex（uuid4 去掉横线）。"""
    return uuid4().hex


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    """ISO 8601 UTC，形如 ``2026-09-30T00:10:50Z``。"""
    return to_iso(utc_now())


def to_iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (
        moment.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def utc_iso_before(seconds: int) -> str:
    """当前 UTC 时间往前推 ``seconds`` 秒（用于超时任务的 cutoff）。"""
    return to_iso(utc_now() - timedelta(seconds=max(0, int(seconds))))


def parse_iso(value: str) -> datetime:
    """解析 ``...Z`` 形式的 ISO 8601；解析不了抛 ``ValueError``。"""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


__all__ = [
    "new_id",
    "utc_now",
    "utc_now_iso",
    "to_iso",
    "utc_iso_before",
    "parse_iso",
]
