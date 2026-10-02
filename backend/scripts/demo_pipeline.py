#!/usr/bin/env python
"""KnowledgeFlow A 线演示：`python scripts/demo_pipeline.py`

定稿文档第二十八节要求「前端暂缓，但 `python -m pytest` 与
`python scripts/demo_pipeline.py` 必须能跑」。

这个脚本把 fixture 真的走一遍完整 A 线（**离线**）：

```text
fixture → (Mock LLM) → Pydantic → Grounding Check → SQLite
        → 实体/主题归一化 → Markdown → 安全文件名 → Obsidian Vault
```

**每个场景用独立的数据库与独立的 vault 子目录**，场景之间互不干扰
（否则「同 source + content_hash 去重」会把后面的场景正确地去重掉，
演示就只剩一行 duplicate 了）。

默认把 vault 与数据库放在临时目录（跑完打印路径），可用 `--vault` / `--db-dir` 指定。
它**不会**碰你真实的 Obsidian Vault，也不碰网络。

脚本自带期望值校验：某个场景的实际结果与期望不符时，退出码为 1。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:  # pragma: no cover - 直接执行脚本时用
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import Settings  # noqa: E402
from app.db.session import Database  # noqa: E402
from app.errors import KnowledgeFlowError  # noqa: E402
from app.logging_config import setup_logging  # noqa: E402
from app.pipeline.service import ProcessingPipeline  # noqa: E402
from app.providers import MockProvider  # noqa: E402
from app.schemas import RawContent  # noqa: E402
from app.testing import load_fixture_json, load_fixture_text  # noqa: E402

ALL = "all"


@dataclass(frozen=True)
class Scenario:
    raw_fixture: str
    llm_fixture: str
    description: str
    #: **最后一次 run** 的期望结果
    expect: str
    expect_error: str | None = None
    expect_dedup_by: str | None = None
    #: 额外再跑几遍同一份内容；元素为 None 表示沿用 fixture 自带的 source_id，
    #: 给字符串则覆盖 source_id（用来演示「同 source 内 content_hash 去重」）。
    extra_source_ids: tuple[str | None, ...] = field(default=())


SCENARIOS: dict[str, Scenario] = {
    "manual": Scenario(
        raw_fixture="raw_content_mixed.json",
        llm_fixture="llm_analysis_valid.json",
        description="手动粘贴（无平台 ID → source_id 走 hash: fallback）",
        expect="completed",
    ),
    "duplicate": Scenario(
        raw_fixture="raw_content_mixed.json",
        llm_fixture="llm_analysis_valid.json",
        description="同一份内容连喂两次 → 按 (source, source_id) 判重，不产生第二个文件",
        expect="duplicate",
        expect_dedup_by="source_id",
        extra_source_ids=(None,),
    ),
    "hash_dedup": Scenario(
        raw_fixture="raw_content.json",
        llm_fixture="llm_analysis_valid.json",
        description="同 source、不同视频 id、但标题/作者/简介完全相同 → 按 content_hash 判重",
        expect="duplicate",
        expect_dedup_by="content_hash",
        extra_source_ids=("7300000000000000099",),
    ),
    "douyin": Scenario(
        raw_fixture="raw_content.json",
        llm_fixture="llm_analysis_valid.json",
        description="抖音视频（有 aweme_id）；模型输出讲的是别的内容 → R2.1 全拦",
        expect="completed",
    ),
    "fenced": Scenario(
        raw_fixture="raw_content_mixed.json",
        llm_fixture="llm_analysis_fenced.json",
        description="模型输出被 ``` 围栏包住 → JSON repair 后可用",
        expect="completed",
    ),
    "preamble": Scenario(
        raw_fixture="raw_content_mixed.json",
        llm_fixture="llm_analysis_preamble.json",
        description="模型输出前后有寒暄 → JSON repair 后可用",
        expect="completed",
    ),
    "invalid": Scenario(
        raw_fixture="raw_content_mixed.json",
        llm_fixture="llm_analysis_invalid.json",
        description="模型输出不合 schema → 三次失败状态机",
        expect="failed",
        expect_error="LLM_INVALID_OUTPUT",
    ),
    "grounding_fail": Scenario(
        raw_fixture="raw_content_mixed.json",
        llm_fixture="llm_analysis_grounding_fail.json",
        description="模型编造实体/数字 → Grounding Check 拦截（仍然写出笔记）",
        expect="completed",
    ),
    "long": Scenario(
        raw_fixture="raw_content_long.json",
        llm_fixture="llm_analysis_valid.json",
        description="超长输入 → 文本预算 → 分块 → synthesis",
        expect="completed",
    ),
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="KnowledgeFlow A 线离线演示（Mock LLM，不碰网络、不碰真实 Vault）"
    )
    parser.add_argument(
        "scenario",
        nargs="?",
        default=ALL,
        choices=[*SCENARIOS, ALL],
        help="要跑的场景（默认全部）",
    )
    parser.add_argument("--vault", type=Path, default=None, help="vault 根目录（每个场景一个子目录）")
    parser.add_argument("--db-dir", type=Path, default=None, help="SQLite 文件目录（每个场景一个库）")
    parser.add_argument("--quiet", action="store_true", help="只打印结果表")
    parser.add_argument("--show-note", action="store_true", help="额外打印生成的 Markdown")
    return parser.parse_args(argv)


def build_provider(llm_fixture: str) -> MockProvider:
    """干净 JSON 走 payload 模式；脏输出（围栏/寒暄）走 raw_text 模式。"""
    text = load_fixture_text(llm_fixture)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return MockProvider(raw_text=text)
    return MockProvider(payload=payload)


async def run_scenario(
    name: str,
    scenario: Scenario,
    *,
    vault_root: Path,
    db_dir: Path,
    settings: Settings,
    show_note: bool,
) -> dict[str, object]:
    """在自己的库与自己的 vault 子目录里跑完这个场景的所有 run。"""
    scenario_vault = vault_root / name
    scenario_vault.mkdir(parents=True, exist_ok=True)
    database = Database.create(f"sqlite:///{db_dir / (name + '.db')}")
    await database.create_all()

    base_raw = RawContent.from_fixture(load_fixture_json(scenario.raw_fixture))
    provider = build_provider(scenario.llm_fixture)
    pipeline = ProcessingPipeline.from_database(
        settings=settings,
        provider=provider,
        database=database,
        vault_root=scenario_vault,
    )

    source_ids: tuple[str | None, ...] = (None, *scenario.extra_source_ids)
    outcomes: list[str] = []
    result = None
    try:
        for override in source_ids:
            raw = base_raw if override is None else base_raw.model_copy(update={"source_id": override})
            result = await pipeline.process(raw)
            outcomes.append(result.outcome)
    finally:
        await database.dispose()

    assert result is not None  # source_ids 至少有一个
    summary = result.to_dict()
    summary["scenario"] = name
    summary["description"] = scenario.description
    summary["expect"] = scenario.expect
    summary["runs"] = "→".join(outcomes)
    summary["run_count"] = len(outcomes)
    summary["matched"] = (
        result.outcome == scenario.expect
        and (scenario.expect_error is None or result.error_type == scenario.expect_error)
        and (
            scenario.expect_dedup_by is None
            or result.deduplicated_by == scenario.expect_dedup_by
        )
    )
    summary["vault"] = str(scenario_vault)

    if show_note and result.note_path:
        note_path = scenario_vault / result.note_path
        if note_path.is_file():
            print("-" * 72)
            print(note_path.read_text(encoding="utf-8"))
            print("-" * 72)
    return summary


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(level="WARNING")

    temp_root = Path(tempfile.mkdtemp(prefix="knowledgeflow-demo-"))
    vault_root = (args.vault or (temp_root / "vault")).resolve()
    vault_root.mkdir(parents=True, exist_ok=True)
    db_dir = (args.db_dir or (temp_root / "db")).resolve()
    db_dir.mkdir(parents=True, exist_ok=True)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    names = list(SCENARIOS) if args.scenario == ALL else [args.scenario]
    summaries: list[dict[str, object]] = []
    for name in names:
        scenario = SCENARIOS[name]
        if not args.quiet:
            print(f"\n### {name}：{scenario.description}")
        summaries.append(
            await run_scenario(
                name,
                scenario,
                vault_root=vault_root,
                db_dir=db_dir,
                settings=settings,
                show_note=args.show_note,
            )
        )

    header = (
        f"{'scenario':<16}{'runs':<22}{'outcome':<11}{'expect':<11}"
        f"{'claims':>7}{'未验证':>7}{'chunks':>7}  判重依据 / 错误 / 笔记"
    )
    print("\n" + "=" * 108)
    print(header)
    print("-" * 108)
    for item in summaries:
        mark = "" if item["matched"] else "  ← 与期望不符"
        tail = item["error_type"] or item["deduplicated_by"] or item["note_path"] or ""
        print(
            f"{str(item['scenario']):<16}{str(item['runs']):<22}{str(item['outcome']):<11}"
            f"{str(item['expect']):<11}{int(item['claim_count']):>7}"
            f"{int(item['unverified_count']):>7}{int(item['chunk_count']):>7}  {tail}{mark}"
        )
    print("=" * 108)

    # 一律打印 ``/`` 分隔（``as_posix()``）：这个脚本的输出是**验收口径**，
    # 人要读、自动化要比对，不该因为跑在 Windows 上就换一套分隔符。
    notes = sorted(
        path.relative_to(vault_root).as_posix() for path in vault_root.rglob("*.md")
    )
    print(f"\nVault 里的笔记（{len(notes)} 份；实体/主题不建空白笔记）：")
    for path in notes:
        print(f"  {path}")
    print(f"\nvault 根目录: {vault_root}")
    print(f"数据库目录:   {db_dir}")

    mismatched = [item["scenario"] for item in summaries if not item["matched"]]
    if mismatched:
        print(f"\n[FAIL] 与期望不符的场景：{mismatched}")
        return 1
    print(f"\n[OK] {len(summaries)} 个场景全部符合期望")
    return 0


if __name__ == "__main__":  # pragma: no cover - 脚本入口
    try:
        raise SystemExit(asyncio.run(main()))
    except KnowledgeFlowError as exc:  # pragma: no cover - 配置类错误
        print(f"[FATAL] {exc.error_type_value}: {exc.message}", file=sys.stderr)
        raise SystemExit(2)
