"""``scripts/migrate_hash_v2.py``：v1 → v2 哈希迁移。

三条要钉住的语义：

1. **真实平台 id 绝不改** —— `source_id` 是身份不是指纹，只有 `hash:` fallback 才跟着重算；
2. **默认 dry-run** —— 不加 `--apply` 一行都不写；
3. **撞车只报不写** —— 合并是有损操作，脚本不代劳。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.db.models import Content
from app.db.mappers import ContentInsert
from app.db.repository import ContentRepository
from app.db.session import Database
from app.normalization.hashing import CONTENT_HASH_VERSION
from app.normalization.service import normalize_content
from app.schemas import RawContent

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "migrate_hash_v2.py"

_SPEC = importlib.util.spec_from_file_location("knowledgeflow_migrate_hash_v2", SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
migrate = importlib.util.module_from_spec(_SPEC)
# 必须先登记进 sys.modules：脚本里的 @dataclass 会去查 cls.__module__，
# 没登记的话 dataclasses 拿到 None 直接炸（AttributeError: 'NoneType' ... __dict__）。
sys.modules["knowledgeflow_migrate_hash_v2"] = migrate
_SPEC.loader.exec_module(migrate)


def v1_hash_of(*, source: str, title: str | None = None, raw_text: str | None = None) -> str:
    """按 v1 白名单（四项）算哈希 —— 用来造「升级前的旧行」。"""
    import hashlib

    from app.normalization.hashing import HASH_SEPARATOR
    from app.normalization.text import normalize_text

    payload = HASH_SEPARATOR.join(
        [normalize_text(source), normalize_text(title), "", ""]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _insert(repo: ContentRepository, **kwargs) -> None:
    await repo.insert_content(ContentInsert(**kwargs))


# --------------------------------------------------------------------------- #
# 计划阶段
# --------------------------------------------------------------------------- #
async def test_v1_rows_are_planned_v2_rows_are_not(db: Database, repo: ContentRepository) -> None:
    legacy = v1_hash_of(source="manual", title=None)
    await _insert(
        repo,
        id="legacy-1",
        source="manual",
        source_id="hash:1d515f1745bd2b6e",
        source_url="",
        title=None,
        raw_text="今天聊人工智能算力。",
        media_type="text",
        content_hash=legacy,
        content_hash_version=1,
    )
    current = normalize_content(
        RawContent(
            source="manual",
            source_id="",
            source_url="",
            media_type="text",
            raw_text="已经用 v2 入库的一条。",
        )
    )
    await _insert(
        repo,
        id="current-1",
        source="manual",
        source_id=current.source_id,
        source_url="",
        raw_text="已经用 v2 入库的一条。",
        media_type="text",
        content_hash=current.content_hash,
        content_hash_version=CONTENT_HASH_VERSION,
    )

    report = await migrate.build_report(db)
    assert report.scanned == 2
    assert report.up_to_date == 1
    assert [row.content_id for row in report.planned] == ["legacy-1"]


async def test_real_platform_id_is_never_touched(db: Database, repo: ContentRepository) -> None:
    """抖音的 aweme_id 是身份，不是指纹 —— 迁移只能改哈希，不能改它。"""
    await _insert(
        repo,
        id="douyin-1",
        source="douyin",
        source_id="7321567890123456789",
        source_url="",
        title="标题",
        raw_text="正文。",
        media_type="video",
        content_hash=v1_hash_of(source="douyin", title="标题"),
        content_hash_version=1,
    )

    report = await migrate.build_report(db)
    plan = report.planned[0]
    assert plan.source_id == "7321567890123456789"
    assert plan.new_source_id is None  # ← 关键：不重算


async def test_fallback_id_is_recomputed(db: Database, repo: ContentRepository) -> None:
    """`hash:` 前缀的 source_id 是哈希派生的，必须跟着一起重算。"""
    await _insert(
        repo,
        id="legacy-2",
        source="manual",
        source_id="hash:1d515f1745bd2b6e",
        source_url="",
        raw_text="完全不同的另一段正文。",
        media_type="text",
        content_hash=v1_hash_of(source="manual"),
        content_hash_version=1,
    )

    report = await migrate.build_report(db)
    plan = report.planned[0]
    assert plan.new_source_id is not None
    assert plan.new_source_id.startswith("hash:")
    assert plan.new_source_id != "hash:1d515f1745bd2b6e"


async def test_collision_is_reported_not_written(db: Database, repo: ContentRepository) -> None:
    """重算后的 id 已被别的行占用 → 进冲突清单，不算「可迁」。"""
    victim = normalize_content(
        RawContent(
            source="manual",
            source_id="",
            source_url="",
            media_type="text",
            raw_text="这条内容已经用 v2 入库了。",
        )
    )
    await _insert(
        repo,
        id="occupant",
        source="manual",
        source_id=victim.source_id,
        source_url="",
        raw_text="这条内容已经用 v2 入库了。",
        media_type="text",
        content_hash=victim.content_hash,
        content_hash_version=CONTENT_HASH_VERSION,
    )
    # 一条 v1 旧行，内容恰好与上面那条相同 → 重算后会撞上 occupant 的 id
    await _insert(
        repo,
        id="legacy-3",
        source="manual",
        source_id="hash:0000000000000000",
        source_url="",
        raw_text="这条内容已经用 v2 入库了。",
        media_type="text",
        content_hash=v1_hash_of(source="manual"),
        content_hash_version=1,
    )

    report = await migrate.build_report(db)
    assert [row.content_id for row in report.conflicts] == ["legacy-3"]
    assert report.migratable == 0


# --------------------------------------------------------------------------- #
# 写入阶段
# --------------------------------------------------------------------------- #
async def test_dry_run_writes_nothing(db: Database, repo: ContentRepository) -> None:
    await _insert(
        repo,
        id="legacy-4",
        source="manual",
        source_id="hash:1d515f1745bd2b6e",
        source_url="",
        raw_text="待迁移的旧行。",
        media_type="text",
        content_hash=v1_hash_of(source="manual"),
        content_hash_version=1,
    )

    before = await repo.get_content("legacy-4")
    migrate.print_report(await migrate.build_report(db), apply=False)
    after = await repo.get_content("legacy-4")
    assert after is not None and before is not None
    assert after.content_hash == before.content_hash
    assert after.content_hash_version == 1


async def test_apply_rewrites_hash_version_and_fallback_id(
    db: Database, repo: ContentRepository
) -> None:
    await _insert(
        repo,
        id="legacy-5",
        source="manual",
        source_id="hash:1d515f1745bd2b6e",
        source_url="",
        raw_text="待迁移的旧行。",
        media_type="text",
        content_hash=v1_hash_of(source="manual"),
        content_hash_version=1,
    )

    report = await migrate.build_report(db)
    await migrate.apply_report(db, report)

    row = await repo.get_content("legacy-5")
    assert row is not None
    assert row.content_hash_version == CONTENT_HASH_VERSION
    assert row.content_hash != v1_hash_of(source="manual")
    assert row.source_id.startswith("hash:")
    assert row.source_id != "hash:1d515f1745bd2b6e"

    # 迁移结果必须与「现在重新喂一遍」算出来的完全一致 —— 否则等于又造了一条重复
    fresh = normalize_content(
        RawContent(
            source="manual",
            source_id="",
            source_url="",
            media_type="text",
            raw_text="待迁移的旧行。",
        )
    )
    assert row.content_hash == fresh.content_hash
    assert row.source_id == fresh.source_id
    assert report.applied == 1
    assert report.failures == []


async def test_parse_args_defaults_to_dry_run() -> None:
    args = migrate.parse_args([])
    assert args.apply is False
    assert args.database is None
