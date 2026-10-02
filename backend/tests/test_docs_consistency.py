"""进度文档的口径一致性检查（`scripts/check_doc_consistency.py`）。

交付物里的同一个事实会散落在好几处，改了实现只改一处就会「文档读起来自洽、
代码跑起来正确，只是两者说的不是同一组数字」—— 不会报错，只有人会发现。
这里把它钉成测试。
"""

from __future__ import annotations

import importlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
SCRIPT = BACKEND_ROOT / "scripts" / "check_doc_consistency.py"
PROGRESS_DOC = REPO_ROOT / "KnowledgeFlow-进度与问题清单.md"

TIMEOUT_SECONDS = 240

#: 进度文档是**内部**文档，不随公开仓库发布 —— 缺了就明确跳过这几条，
#: 而不是让整套测试变红（红会让人以为是代码坏了）。判定逻辑本身仍然被测
#: （见 `test_cli_option_parsing_ignores_prose_mentions` 等不依赖该文档的用例）。
requires_progress_doc = pytest.mark.skipif(
    not PROGRESS_DOC.is_file(),
    reason="找不到进度文档（内部文档，不随公开仓库发布）",
)


def run_checker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(BACKEND_ROOT),
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )


def load_checker():
    """把 ``check_doc_consistency.py`` 当模块加载。

    它在 ``scripts/`` 下，不是包，所以只能临时把它那个目录塞进 ``sys.path``；
    用完立刻摘掉，免得影响同一进程里其它测试的导入。
    """
    sys.path.insert(0, str(SCRIPT.parent))
    module_name = SCRIPT.stem
    try:
        if module_name in sys.modules:
            return importlib.reload(sys.modules[module_name])
        return importlib.import_module(module_name)
    finally:
        sys.path.remove(str(SCRIPT.parent))


def test_script_exists() -> None:
    assert SCRIPT.is_file()


@requires_progress_doc
def test_progress_doc_exists() -> None:
    assert PROGRESS_DOC.is_file()


@requires_progress_doc
def test_checker_passes_on_current_docs() -> None:
    result = run_checker()
    assert result.returncode == 0, result.stdout + result.stderr
    # 文档在（本仓库）→ OK；不在（公开仓库）→ 明确 SKIP，两种都算通过
    assert "[OK]" in result.stdout or "[SKIP]" in result.stdout


@requires_progress_doc
def test_checker_catches_stale_counts(tmp_path: Path) -> None:
    """把合计改错 → 必须报错（证明这个守卫是有牙的，不是永远绿）。

    刻意**不写死**当前用例数：否则每次加测试这个测试自己就过期了。
    数出来的格式也跟着文档走 —— 文档把「用例总数」和「默认通过数」分开写之后，
    这里必须按新格式替换，照抄旧格式会替换失败（静默变成「没做变异」）。
    """
    text = PROGRESS_DOC.read_text(encoding="utf-8")
    total = re.search(r"\*\*合计\*\*：`(\d+) 项用例`", text)
    assert total is not None, "进度文档里找不到「合计：N 项用例」"
    assert total.group(0) in text

    mutated = tmp_path / "progress.md"
    mutated.write_text(text.replace(total.group(0), "**合计**：`7 项用例`", 1), encoding="utf-8")
    assert "**合计**：`7 项用例`" in mutated.read_text(encoding="utf-8"), "变异没生效"

    result = run_checker("--doc", str(mutated))
    assert result.returncode == 1
    assert "文档合计 7 != 实际用例数" in result.stdout


@requires_progress_doc
def test_checker_catches_reverted_phase_status(tmp_path: Path) -> None:
    mutated = tmp_path / "progress.md"
    text = PROGRESS_DOC.read_text(encoding="utf-8")
    mutated.write_text(text.replace("Deduplication | 完成", "Deduplication | **待做**", 1), encoding="utf-8")

    result = run_checker("--skip-tests", "--doc", str(mutated))
    assert result.returncode == 1
    assert "残留旧口径" in result.stdout


def test_checker_catches_missing_new_wording(tmp_path: Path) -> None:
    mutated = tmp_path / "progress.md"
    mutated.write_text("# 空文档\n", encoding="utf-8")
    result = run_checker("--skip-tests", "--doc", str(mutated))
    assert result.returncode == 1
    assert "缺少新口径" in result.stdout


def test_checker_reports_missing_doc() -> None:
    result = run_checker("--doc", "/tmp/definitely-not-here-kf.md")
    assert result.returncode == 1
    assert "找不到进度文档" in result.stdout


@requires_progress_doc
def test_checker_detects_scenario_drift(tmp_path: Path) -> None:
    """文档里的场景清单必须与 demo 脚本一致。"""
    mutated = tmp_path / "progress.md"
    text = PROGRESS_DOC.read_text(encoding="utf-8")
    mutated.write_text(text.replace("hash_dedup |", "", 1), encoding="utf-8")

    result = run_checker("--skip-tests", "--doc", str(mutated))
    assert result.returncode == 1
    assert "hash_dedup" in result.stdout


@pytest.mark.parametrize("fabricated", ["--mapreduce", "--transcribe", "--turbo"])
@requires_progress_doc
def test_checker_catches_invented_cli_options(tmp_path: Path, fabricated: str) -> None:
    """文档里写了个任何脚本都不支持的选项 → 必须报错。

    这条一度是**漏的**：老实现只拿「已知选项表」去核对脚本，
    文档里新冒出来的 ``--xxx`` 根本不进检查范围。
    """
    mutated = tmp_path / "progress.md"
    text = PROGRESS_DOC.read_text(encoding="utf-8")
    mutated.write_text(text.replace("--timeout 秒", f"--timeout 秒 {fabricated}", 1), encoding="utf-8")

    result = run_checker("--skip-tests", "--doc", str(mutated))
    assert result.returncode == 1
    assert f"文档里写了 {fabricated}" in result.stdout


@requires_progress_doc
def test_checker_catches_reverted_phase7_status(tmp_path: Path) -> None:
    """Phase 7 从「已完成」退回「待做」，或重新声称媒体下载没做 → 必须报错。"""
    text = PROGRESS_DOC.read_text(encoding="utf-8")
    # 不写死用例数：加测试会让它变，变异失败会静默变成「没变异 → 检查器当然放行」
    row = re.search(r"^\|\s*7\s*\|.*\|\s*\d+\s*\|\s*$", text, re.MULTILINE)
    assert row is not None, "进度文档里找不到 Phase 7 那一行"

    mutated = tmp_path / "progress.md"
    mutated.write_text(text.replace(row.group(0), "| 7 | ASR / OCR | **待做** | — |", 1), encoding="utf-8")
    assert "| 7 | ASR / OCR | **待做** | — |" in mutated.read_text(encoding="utf-8")

    result = run_checker("--skip-tests", "--doc", str(mutated))
    assert result.returncode == 1
    assert "Phase 7 状态未更新" in result.stdout


@requires_progress_doc
def test_checker_catches_reverted_phase8_status(tmp_path: Path) -> None:
    """Phase 8 退回「定稿没派阶段 / 缺失」→ 必须报错。"""
    text = PROGRESS_DOC.read_text(encoding="utf-8")
    row = re.search(r"^\|\s*8\s*\|.*\|\s*\d+\s*\|\s*$", text, re.MULTILINE)
    assert row is not None, "进度文档里找不到 Phase 8 那一行"

    reverted = (
        "| ? | API 层（FastAPI 路由） | **定稿没派阶段** | — |\n"
        "| ? | 启动入口（调用任务恢复） | **缺失** | — |"
    )
    mutated = tmp_path / "progress.md"
    mutated.write_text(text.replace(row.group(0), reverted, 1), encoding="utf-8")
    assert "**定稿没派阶段**" in mutated.read_text(encoding="utf-8"), "变异没生效"

    result = run_checker("--skip-tests", "--doc", str(mutated))
    assert result.returncode == 1
    assert "Phase 8 已完成" in result.stdout


def test_cli_option_parsing_ignores_prose_mentions() -> None:
    """写在注释 / docstring 里的选项名**不算**「脚本支持它」。

    踩过这个坑：docstring 里举例写了 ``--mapreduce``，子串匹配转身从
    这份源码里就找到了它，于是负向测试永远绿。改解析 argparse 声明才修好。
    """
    checker = load_checker()

    # 一个只出现在叙述文字里、绝不来自 add_argument 的选项
    assert "--nonsense-not-declared" not in checker.script_options(checker.all_script_source())["check_doc_consistency.py"]
    # 真实声明的必须认得出来
    assert "--skip-tests" in checker.script_options(checker.all_script_source())["check_doc_consistency.py"]


def test_checker_tables_have_no_duplicate_entries() -> None:
    """``STALE_PATTERNS`` / ``REQUIRED_IN_DOC`` 里不能有重复项。

    这两张表都被重复追加过：``"1465 项用例"`` 出现过四次、``"1455 项用例"`` 两次。
    重复项不会让检查出错，只会让「这张表里到底有几条规则」变成没人知道的数 ——
    而且漏删一条时，你没法从表长上看出来。
    """
    checker = load_checker()
    for name in ("STALE_PATTERNS", "REQUIRED_IN_DOC"):
        entries = [
            item[0] if isinstance(item, tuple) else item for item in getattr(checker, name)
        ]
        duplicates = sorted({entry for entry in entries if entries.count(entry) > 1})
        assert duplicates == [], f"{name} 里有重复项：{duplicates}"


def test_checker_does_not_pin_a_literal_test_count() -> None:
    """``REQUIRED_IN_DOC`` 里不该钉「用例总数」这个数字。

    早先那里有一条 ``"1466"``。它想守的性质（文档里印的合计 == 实际收集到的
    用例数）已经由 ``check_arithmetic`` 用**活值**核对了，再钉一个字面量
    只是多一个必然腐烂的副本 —— 实测它已经落后于真实用例数很久，而且没人发现，
    因为它永远是绿的（那条守卫只扫内部进度文档，而那份文档不随公开仓库发布）。
    """
    checker = load_checker()
    pinned = [item for item in checker.REQUIRED_IN_DOC if item.isdigit()]
    assert pinned == [], f"不要在 REQUIRED_IN_DOC 里钉裸数字：{pinned}"


def test_requires_progress_doc_decorator_is_applied_once() -> None:
    """``@requires_progress_doc`` 不该叠两遍 —— 那是复制粘贴留下的噪声。

    叠两遍和叠一遍行为完全一样（都是 skipif），所以**不会有任何测试因此变红**，
    正因如此它才会一直留着。判据写在这里，免得下次又粘一份。

    注意断言的写法：被查的字符串必须**拼**出来，不能整段写在这里。
    整段写的话，这条断言自己的源码就成了第一个命中 ——
    「检查器把自己的文本当证据」，本项目栽过五次的那个坑。
    """
    source = Path(__file__).read_text(encoding="utf-8")

    doubled = "@requires_progress_doc" + "\n" + "@requires_progress_doc"
    assert doubled not in source, "装饰器叠了两遍"

    definition = "requires_progress_doc" + " = pytest.mark.skipif("
    assert source.count(definition) == 1, "装饰器被重复定义"
