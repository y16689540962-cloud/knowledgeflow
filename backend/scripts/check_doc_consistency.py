#!/usr/bin/env python
"""文档口径一致性检查：`python scripts/check_doc_consistency.py`

改了实现（用例数、场景数、命令行选项、某个「待做」变成「已完成」）之后，
交付物里的同一事实往往散落在好几处 —— 文档读起来自洽、代码跑起来正确，
只是两者说的不是同一组数字。**不会报错，只有人会发现。**

这个脚本把核对变成一次可复跑的命令，做四件事：

1. **正向**：文档里必须出现的「新口径」是否都在
2. **反向**：旧口径是否已归零（历史日志按约定豁免 —— 见 `APPEND_ONLY_LOGS`）
3. **算术**：分项用例之和 == 合计 == 实际收集到的用例数
4. **命令面**：文档里写的 CLI 选项必须真的被脚本支持

退出码 0 = 一致；1 = 有不一致。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
PROGRESS_DOC = REPO_ROOT / "KnowledgeFlow-进度与问题清单.md"

#: 追加式历史日志：**刻意**保留当时的口径，不参与反向扫描（否则会抹掉证据）。
APPEND_ONLY_LOGS = (
    REPO_ROOT / ".workbuddy/memory",
)

#: 反向扫描要覆盖的交付物。
SCAN_TARGETS = (PROGRESS_DOC,)

#: 旧口径模式 → 出现即视为漏改。
STALE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("A 线还差最后一环 Deduplication", "Phase 5 未完成时的结论"),
    ("Deduplication | **待做**", "Phase 5 状态未更新"),
    ("Phase 5 · Deduplication（需要", "Phase 5 尚未开工的措辞"),
    ("Q1（阻塞 Phase 5）", "Q1 已答复"),
    ("--db PATH", "demo 的 --db 已改名 --db-dir"),
    ("跨来源 `content_hash` 去重；目前只有", "Phase 5 未做时的描述"),
    ("B 线（Ingestion）：0%", "Phase 6 完成后 B 线不再是 0%"),
    ("B 线（抖音采集）为 0%", "Phase 6 完成后 B 线不再是 0%"),
    ("Ingestion Pipeline 整条链路为 0%", "Phase 6 完成后 B 线不再是 0%"),
    ("全部未实现", "Phase 6 完成后不该再这么说"),
    ("Q5（Phase 6）要不要联网", "Q5 已用实测回答"),
    ("# 1050 passed", "旧合计"),
    ("1050 项测试全绿", "旧合计"),
    ("# 1179 passed", "旧合计"),
    ("1179 项测试全绿", "旧合计"),
    ("| 7 | ASR / OCR | **待做** | — |", "Phase 7 状态未更新"),
    ("Phase 7 · ASR / OCR（需要 Q6 的答复）", "Q6 已按默认口径执行"),
    ("媒体下载到 `Assets/` 未实现", "Phase 7 已实现媒体下载"),
    ("1237 项用例", "旧合计"),
    ("# 1232 passed", "旧默认通过数"),
    ("| ? | API 层（FastAPI 路由） | **定稿没派阶段** |", "Phase 8 已完成"),
    ("| ? | 启动入口（调用任务恢复） | **缺失** |", "Phase 8 已完成"),
    ("缺口 A · API 层（需要 Q7 的答复）", "Q7 已按默认口径执行"),
    ("缺口 B · 启动入口（需要 Q8 的答复）", "Q8 已按默认口径执行"),
    ("# 已装，尚未使用（见 Q7）", "FastAPI 已在 Phase 8 使用"),
    ("**没有 API 层**", "Phase 8 已交付 API 层"),
    ("`recover_and_resume()` 无调用方", "Phase 8 的 lifespan 已调用启动恢复"),
    ("1263 项用例", "旧合计"),
    ("# 1259 passed", "旧默认通过数"),
    ("1278 项用例", "v2 哈希前的旧合计"),
    ("1282 项用例", "待办建议实施前的旧合计"),
    ("1295 项用例", "暴露守卫落地时的中间合计"),
    ("1303 项用例", "覆盖率守卫落地时的中间合计"),
    ("1316 项用例", "登录态支持落地时的中间合计"),
    ("1337 项用例", "本地媒体入口落地时的中间合计"),
    ("1367 项用例", "登录态配置项落地时的中间合计"),
    ("1376 项用例", "抖音详情接口落地时的中间合计"),
    ("1406 项用例", "Web UI 抖音入口落地时的中间合计"),
    ("1411 项用例", "分享文案链接提取落地时的中间合计"),
    ("1426 项用例", "Chrome 扩展落地时的中间合计"),
    ("1455 项用例", "扩展 URL 判定落地时的中间合计"),
    ("1465 项用例", "开源准备落地时的中间合计"),
    ("1465 项用例", "开源准备落地时的中间合计"),
    ("1455 项用例", "扩展 URL 判定落地时的中间合计"),
    ("1465 项用例", "开源准备落地时的中间合计"),
    ("1465 项用例", "开源准备落地时的中间合计"),
    ("Phase 1–8 已完成", "Phase 9 已完成"),
)

#: 新口径必须出现在文档里。
REQUIRED_IN_DOC: tuple[str, ...] = (
    "1466",
    "demo_capabilities.py",
    "scripts/serve.py",
    "scripts/export_openapi.py",
    "openapi-typescript",
    "create_app",
    "/api/health",
    "/api/contents",
    "/api/ingest/manual",
    "/api/ingest/douyin",
    "/api/tasks/recover",
    "app/web/",
    "docs/ui-preview.png",
    "textContent",
    "KNOWLEDGEFLOW_BROWSER_TEST",
    "faster-whisper",
    "tesseract",
    "--db-dir",
    "hash_dedup",
    "deduplicated_by",
    "A 线（含 Deduplication）已按定稿第二十二/三十二节跑通",
    "demo_ingestion.py",
    "REQUEST_BLOCKED",
    "ManualPasteSource",
    "--process",
    "--manual-text",
    "aweme/v1/web/aweme/detail",
    "detail_api_note",
    "/api/ingest/douyin",
    "extract_first_url",
    "install_autostart.sh",
    "chrome-extension",
)

#: 每个 demo 脚本 → 文档提到这些选项时，该脚本必须真的支持。
SCRIPTS_WITH_OPTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("demo_pipeline.py", ("--vault", "--db-dir", "--quiet", "--show-note")),
    (
        "demo_ingestion.py",
        ("--url", "--manual-text", "--manual-title", "--timeout", "--process", "--vault"),
    ),
    (
        "demo_capabilities.py",
        (
            "--file", "--url", "--model", "--lang", "--no-asr", "--no-ocr",
            "--process", "--vault", "--keep-dir",
        ),
    ),
    ("check_doc_consistency.py", ("--skip-tests", "--doc")),
    ("check_coverage.py", ("--min", "--include-engines")),
    ("migrate_hash_v2.py", ("--database", "--apply")),
    (
        "serve.py",
        (
            "--host",
            "--port",
            "--vault",
            "--mock",
            "--no-recovery",
            "--reload",
            "--no-web",
            "--expose",
        ),
    ),
    ("export_openapi.py", ("--out", "--indent")),
)

#: 文档里提到的**环境开关** → 必须出现在对应文件源码里。
#: 只对「全大写下划线 + 项目前缀」这种名字做子串匹配 —— 它们不可能撞上普通单词，
#: 和 argparse 选项那种「注释里举例就会误中」的情况不同。
ENV_SWITCHES: tuple[tuple[str, str], ...] = (
    ("KNOWLEDGEFLOW_BROWSER_TEST", "tests/test_web_ui_browser.py"),
    ("KNOWLEDGEFLOW_UI_SHOT", "tests/test_web_ui_browser.py"),
    ("KNOWLEDGEFLOW_REAL_ASR", "tests/test_asr_real.py"),
    ("KNOWLEDGEFLOW_REAL_OCR", "tests/test_ocr_real.py"),
)

#: 所有被跟踪脚本的**全部**选项文本，用来判断文档里提到的选项是否真的存在。
TRACKED_SCRIPTS: tuple[str, ...] = tuple(name for name, _ in SCRIPTS_WITH_OPTIONS)
#: shell 脚本没有 argparse，选项写在 ``case`` 分支里 —— 单独一套提取方式。
SHELL_SCRIPTS: tuple[str, ...] = ("install_autostart.sh",)
ALL_SCRIPTS: tuple[str, ...] = TRACKED_SCRIPTS + SHELL_SCRIPTS

#: argparse 自己就有、不需要在任何脚本里显式出现的选项。
BUILTIN_OPTIONS: frozenset[str] = frozenset({"--help", "-h"})

#: 文档里**正确地**提到、但不属于跟踪脚本的选项 —— 逐条注明出处，防止白名单被滥用。
FOREIGN_OPTIONS: dict[str, str] = {
    "--basetemp": "pytest 自己 CLI 选项（`pyproject.toml` 的 addopts 里配的，不是本项目的脚本）",
    "--check": "node 的 CLI 选项（文档里 `node --check` 校验 JS 语法，不是本项目脚本的选项）",
    "--disable-extensions": (
        "Google Chrome 自己的启动参数（文档里说明 Playwright 起 Chrome 时默认带它，"
        "不是本项目脚本的选项）"
    ),
}

DOC_OPTION = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]*")

#: Phase 号必须是 ``\d+`` 而不是 ``\d`` —— 写成单数字时，「| 10 | …」这一整行
#: 会被静默跳过（不报错、也不参与算术比对），Phase 10 的用例数就成了没人管的数字。
PLAN_ROW = re.compile(r"^\|\s*(\d+)\s*\|.*?\|\s*(\d+)\s*\|\s*$", re.MULTILINE)
#: 合计行写成「`N 项用例` —— 默认跑出 `...`」：用例总数与「默认通过数」是两个数，
#: 因为真实引擎测试默认跳过（不联网），不能被误写成「全部通过」。
TOTAL_ROW = re.compile(r"\*\*合计\*\*：`(\d+) 项用例`")
COLLECTED = re.compile(r"(\d+) tests? collected")
#: ``pytest --collect-only -q``（叠加 pyproject 里的 ``-q``）只打每个文件的用例数，没有总计行。
PER_FILE_COUNT = re.compile(r"^(\S+\.py):\s*(\d+)\s*$", re.MULTILINE)
#: 文档里的 demo 场景清单行（``#   场景：a | b | c``）。
SCENARIO_LIST_LINE = re.compile(r"#\s*场景：([a-z_ |]+)")


def actual_test_count() -> int:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=str(BACKEND_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    output = result.stdout + result.stderr
    match = COLLECTED.search(output)
    if match is not None:
        return int(match.group(1))

    counts = PER_FILE_COUNT.findall(output)
    if counts:
        return sum(int(count) for _path, count in counts)
    raise SystemExit("拿不到 pytest 的用例总数，先确认 `python -m pytest` 能跑")


def check_required(text: str) -> list[str]:
    return [f"文档里缺少新口径：{item!r}" for item in REQUIRED_IN_DOC if item not in text]


def check_stale(text: str) -> list[str]:
    return [
        f"文档里残留旧口径（{why}）：{pattern!r}"
        for pattern, why in STALE_PATTERNS
        if pattern in text
    ]


def check_arithmetic(text: str) -> list[str]:
    problems: list[str] = []
    parts = {int(phase): int(count) for phase, count in PLAN_ROW.findall(text)}
    total_match = TOTAL_ROW.search(text)
    if not parts or total_match is None:
        return ["解析不出「分项用例数」或「合计」，检查表格格式是否被改动"]

    declared_total = int(total_match.group(1))
    measured = actual_test_count()
    if sum(parts.values()) != declared_total:
        problems.append(
            f"分项之和 {sum(parts.values())} != 文档合计 {declared_total}"
            f"（分项：{parts}）"
        )
    if declared_total != measured:
        problems.append(f"文档合计 {declared_total} != 实际用例数 {measured}")
    return problems


def all_script_source() -> dict[str, str]:
    sources: dict[str, str] = {}
    for name in ALL_SCRIPTS:
        path = BACKEND_ROOT / "scripts" / name
        sources[name] = path.read_text(encoding="utf-8") if path.is_file() else ""
    return sources


#: 从脚本的 ``add_argument()`` 声明里抽取真正支持的选项。
ADD_ARGUMENT_OPTION = re.compile(r"""add_argument\(\s*["'](--[a-z][a-z0-9-]*)["']""")
#: shell 脚本的选项是 ``case`` 的分支标签（``  --install)   do_install ;;``）。
#: **只**认行首的分支标签，不做全文子串匹配 —— 否则脚本注释里提到的选项
#: 会被当成「支持它」（本项目已经三次栽在「检查器把自己的文档当证据」上）。
CASE_OPTION = re.compile(r"^\s*(--[a-z][a-z0-9-]*)\)", re.MULTILINE)


def script_options(sources: dict[str, str]) -> dict[str, set[str]]:
    """每个脚本**真正支持**的选项集合 —— 解析 argparse 声明，不做源码子串匹配。

    我一开始图省事，直接在脚本源码里 ``option in source`` 子串匹配，结果栽在自己手上：
    本文件下方的 docstring 里写着「注入 ``--mapreduce`` 它就漏过去了」，
    子串匹配转身就在**这份源码里**找到了 ``--mapreduce``，于是判定「脚本支持它」，
    那条负向测试永远绿。注释里的例子变成了假的通过理由。
    """
    options: dict[str, set[str]] = {}
    for name, source in sources.items():
        pattern = CASE_OPTION if name.endswith(".sh") else ADD_ARGUMENT_OPTION
        options[name] = set(pattern.findall(source))
    return options


def check_cli_options(text: str) -> list[str]:
    """文档里提到的选项，必须真的被**某个**跟踪脚本支持。

    两个方向都要堵：

    * **实现改了、文档没跟**：选项从脚本里消失，文档却还在写
    * **文档自己编**：写了个任何脚本都不支持的 ``--xxx``
      （第一版只核对「已知选项表」，扫不出这条）
    """
    sources = all_script_source()
    problems: list[str] = [
        f"找不到脚本：{name}" for name, source in sources.items() if not source
    ]

    known: set[str] = set()
    for options in script_options(sources).values():
        known |= options

    mentioned = set(DOC_OPTION.findall(text)) - BUILTIN_OPTIONS - FOREIGN_OPTIONS.keys()
    problems += [
        f"文档里写了 {option}，但 {', '.join(ALL_SCRIPTS)} 里都没有"
        for option in sorted(mentioned - known)
    ]
    return problems


def check_env_switches(text: str) -> list[str]:
    """文档里提到的环境开关，必须在对应实现文件里真的存在。"""
    problems: list[str] = []
    for marker, relative in ENV_SWITCHES:
        if marker not in text:
            continue
        path = BACKEND_ROOT / relative
        if not path.is_file() or marker not in path.read_text(encoding="utf-8"):
            problems.append(f"文档提到 {marker}，但 {relative} 里没有实现")
    return problems


def check_scenarios(text: str) -> list[str]:
    """文档里的场景「表格行」与「清单行」都必须与 demo 脚本一致。

    只做子串检查是不够的：``hash_dedup`` 在文档里出现两次（表格一行 + 清单一处），
    删掉其中一处子串检查照样通过。所以：

    * 表格检查要求名字出现在**行首**（``\\n| 名字 |``），清单行里的同名不算
    * 清单行解析出来按顺序比对
    """
    demo = (BACKEND_ROOT / "scripts" / "demo_pipeline.py").read_text(encoding="utf-8")
    names = re.findall(r'^    "([a-z_]+)": Scenario\(', demo, re.MULTILINE)
    if not names:
        return ["解析不出 demo 脚本里的场景名"]

    problems = [
        f"场景表里缺少 {name!r} 这一行"
        for name in names
        if f"\n| {name} |" not in text
    ]

    listed_match = SCENARIO_LIST_LINE.search(text)
    if listed_match is None:
        problems.append("文档里找不到「#   场景：…」清单行")
    else:
        listed = [
            item.strip() for item in listed_match.group(1).split("|") if item.strip()
        ]
        if listed != names:
            problems.append(f"文档场景清单 {listed} != 脚本顺序 {names}")

    declared = re.search(r"(\d+) 个场景", text)
    if declared and int(declared.group(1)) != len(names):
        problems.append(f"文档写 {declared.group(1)} 个场景，脚本里实际 {len(names)} 个")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="进度文档口径一致性检查")
    parser.add_argument("--skip-tests", action="store_true", help="跳过「实际用例数」比对（快）")
    parser.add_argument("--doc", type=Path, default=PROGRESS_DOC, help="被检查的文档（默认进度文档）")
    args = parser.parse_args(argv)

    if not args.doc.is_file():
        if args.doc != PROGRESS_DOC:
            # 显式指定了一个不存在的文档 → 是调用方写错了，必须报错。
            print(f"[FAIL] 找不到进度文档：{args.doc}")
            return 1
        # 默认文档不存在：它是**内部**进度文档，不随公开仓库发布。
        # 这种情况不是「不一致」，明确跳过 —— 报 OK 是撒谎，报 FAIL 会让人
        # 以为代码坏了。
        print(
            f"[SKIP] 找不到进度文档：{args.doc.name}"
            "（内部进度文档，不随公开仓库发布）"
        )
        return 0
    text = args.doc.read_text(encoding="utf-8")

    problems: list[str] = []
    problems += check_required(text)
    problems += check_stale(text)
    problems += check_cli_options(text)
    problems += check_env_switches(text)
    problems += check_scenarios(text)
    if not args.skip_tests:
        problems += check_arithmetic(text)

    if problems:
        print("[FAIL] 文档与实现不一致：")
        for item in problems:
            print(f"  - {item}")
        print(
            "\n注意：追加式历史日志（"
            + ", ".join(str(path.relative_to(REPO_ROOT)) for path in APPEND_ONLY_LOGS)
            + "）刻意保留当时口径，不在检查范围内。"
        )
        return 1

    print(f"[OK] 文档口径与实现一致：{args.doc.name}")
    return 0


if __name__ == "__main__":  # pragma: no cover - 脚本入口
    raise SystemExit(main())
