#!/usr/bin/env python
"""把 ``content_hash`` 从 v1 迁到 v2：`python scripts/migrate_hash_v2.py --apply`

背景
----

定稿第七节的哈希白名单原文只有四项，``raw_text`` 是漏写（详见进度清单 Q11）。
v2 把它纳入后：

* **新内容**用 v2 哈希；
* **旧行仍是 v1**，而判重按 ``content_hash_version`` 过滤（第七节「旧数据不重算」），
  所以同一份内容可能 v1 行与 v2 行各一条、出两份笔记。

这个脚本就是用来收拾这个尾巴的。

它做什么
--------

对每条 ``content_hash_version != 2`` 的行：

1. 用 v2 白名单重算 ``content_hash``，版本置 2；
2. **只**在该行的 ``source_id`` 是 ``hash:`` fallback（第五节）时才重算它 ——
   真实平台 id（如抖音 ``aweme_id``）是身份，不是指纹，**绝不动**；
3. 若重算后的 ``(source, source_id)`` 已被别的行占用 → **只报不写**，
   合并不由脚本代劳（见下）。

安全设计
--------

* **默认 dry-run**：不加 ``--apply`` 只打印计划，一行都不写。
* **不合并、不删除**：撞车时保留原样并列入冲突清单。把两条内容合成一条
  意味着要搬 analyses / content_entities / 笔记文件 —— 那是有损操作，
  交给人在看清冲突之后再决定，脚本不猜。
* 逐行在**自己的事务**里写：某行失败不影响其它行，失败原因写进报告。

用法
----

```bash
python scripts/migrate_hash_v2.py                      # 只看计划
python scripts/migrate_hash_v2.py --apply              # 真的写
python scripts/migrate_hash_v2.py --database path/to/kf.db  # 指定库（默认读 .env）
```
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:  # pragma: no cover - 直接执行脚本时用
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select, update  # noqa: E402

from app.config import load_settings  # noqa: E402
from app.db.models import Content  # noqa: E402
from app.db.session import Database  # noqa: E402
from app.normalization.hashing import CONTENT_HASH_VERSION  # noqa: E402
from app.normalization.service import normalize_content  # noqa: E402
from app.normalization.source_id import HASH_FALLBACK_PREFIX, fallback_source_id  # noqa: E402
from app.schemas import RawContent  # noqa: E402

EXIT_OK = 0
EXIT_FAILED = 1


@dataclass
class PlanRow:
    """一行内容的迁移计划。"""

    content_id: str
    source: str
    source_id: str
    new_source_id: str | None
    new_content_hash: str
    blocking_content_id: str | None = None

    @property
    def is_conflict(self) -> bool:
        return self.blocking_content_id is not None


@dataclass
class MigrationReport:
    scanned: int = 0
    up_to_date: int = 0
    planned: list[PlanRow] = field(default_factory=list)
    applied: int = 0
    conflicts: list[PlanRow] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)

    @property
    def migratable(self) -> int:
        return len(self.planned) - len(self.conflicts)


def plan_for(row: Content, taken: dict[tuple[str, str], str]) -> PlanRow:
    """给一行算出 v2 的哈希与（必要时）新的 fallback id。"""
    # 注意这里 source_id 传空串：fallback id 是**由哈希派生的**，
    # 拿着旧的 fallback 值去 normalize 会被当成「已有身份」原样返回（is_fallback=False），
    # 那就永远迁不动了。必须先算出 v2 哈希，再用第五节的规则重新派生 id。
    raw = RawContent(
        source=row.source,
        source_id="" if row.source_id.startswith(HASH_FALLBACK_PREFIX) else row.source_id,
        source_url=row.source_url or "",
        title=row.title,
        author=row.author,
        author_id=row.author_id,
        description=row.description,
        media_type=row.media_type,
        media_path=None,
        raw_text=row.raw_text,
        transcript=row.transcript,
        ocr_text=row.ocr_text,
    )
    normalized = normalize_content(raw)

    new_source_id: str | None = None
    if row.source_id.startswith(HASH_FALLBACK_PREFIX):
        new_source_id = fallback_source_id(normalized.content_hash)

    blocking: str | None = None
    if new_source_id is not None:
        owner = taken.get((row.source, new_source_id))
        # 占位者若是它自己（库里已经是这个 id）就不算冲突。
        if owner is not None and owner != row.id:
            blocking = owner

    return PlanRow(
        content_id=row.id,
        source=row.source,
        source_id=row.source_id,
        new_source_id=new_source_id,
        new_content_hash=normalized.content_hash,
        blocking_content_id=blocking,
    )


async def build_report(database: Database) -> MigrationReport:
    report = MigrationReport()
    async with database.session_factory() as session:
        rows = (await session.execute(select(Content))).scalars().all()

    taken: dict[tuple[str, str], str] = {
        (row.source, row.source_id): row.id for row in rows
    }

    for row in rows:
        report.scanned += 1
        if row.content_hash_version == CONTENT_HASH_VERSION:
            report.up_to_date += 1
            continue
        plan = plan_for(row, taken)
        report.planned.append(plan)
        if plan.is_conflict:
            report.conflicts.append(plan)
        elif plan.new_source_id is not None:
            # 预占，避免同一次迁移里两行算出同一个新 id 却都被判为「可迁」。
            taken[(plan.source, plan.new_source_id)] = plan.content_id

    return report


async def apply_report(database: Database, report: MigrationReport) -> None:
    for plan in report.planned:
        if plan.is_conflict:
            continue
        try:
            async with database.session_factory() as session:
                async with session.begin():
                    values: dict[str, object] = {
                        "content_hash": plan.new_content_hash,
                        "content_hash_version": CONTENT_HASH_VERSION,
                    }
                    if plan.new_source_id is not None:
                        values["source_id"] = plan.new_source_id
                    await session.execute(
                        update(Content).where(Content.id == plan.content_id).values(**values)
                    )
        except Exception as exc:  # noqa: BLE001 - 一行失败不该带走整批
            report.failures.append((plan.content_id, f"{type(exc).__name__}: {exc}"))
        else:
            report.applied += 1


def print_report(report: MigrationReport, *, apply: bool) -> None:
    verb = "已迁移" if apply else "计划迁移"
    print(f"扫描 {report.scanned} 行；已是 v{CONTENT_HASH_VERSION} 的有 {report.up_to_date} 行")
    print(f"{verb} {report.migratable} 行" + ("（已写入）" if apply else "（dry-run，未写入）"))
    print(f"冲突 {len(report.conflicts)} 行（**不动**，需要人来决定是否合并）")

    for plan in report.conflicts:
        print(
            f"  · {plan.content_id[:8]} {plan.source}/{plan.source_id} → "
            f"{plan.new_source_id} 已被 {plan.blocking_content_id[:8]} 占用"
        )
    for content_id, why in report.failures:
        print(f"  · 写入失败 {content_id[:8]}：{why}")

    if report.conflicts:
        print(
            "\n冲突行的处理建议：确认两份内容确实是同一份后，"
            "手动删掉多余那一行（笔记文件也要一并处理），再重跑本脚本。"
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="content_hash v1 → v2 迁移")
    parser.add_argument(
        "--database",
        default=None,
        help="SQLite 文件路径（默认读 .env 的 DATABASE_URL）",
    )
    parser.add_argument("--apply", action="store_true", help="真的写入；不加则只打印计划")
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    if args.database is not None:
        database_url = f"sqlite:///{Path(args.database).resolve()}"
    else:
        database_url = load_settings().database_url

    database = Database.create(database_url)
    try:
        report = await build_report(database)
        if args.apply:
            await apply_report(database, report)
        print_report(report, apply=args.apply)
    finally:
        await database.dispose()

    return EXIT_FAILED if report.failures else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":  # pragma: no cover - 脚本入口
    raise SystemExit(main())
