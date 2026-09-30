"""`python scripts/demo_pipeline.py` 必须真的能跑（定稿文档第二十八节）。

前端暂缓，所以这个脚本就是 A 线的可执行验收入口之一。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = BACKEND_ROOT / "scripts" / "demo_pipeline.py"

TIMEOUT_SECONDS = 240


def run_demo(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(BACKEND_ROOT),
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )


def test_script_exists() -> None:
    assert SCRIPT.is_file()


def test_help_works() -> None:
    result = run_demo("--help")
    assert result.returncode == 0
    assert "--vault" in result.stdout
    assert "--db-dir" in result.stdout


def test_all_scenarios_match_expectations() -> None:
    result = run_demo("--quiet")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "全部符合期望" in result.stdout
    assert "与期望不符" not in result.stdout


def test_demo_uses_a_temp_vault_by_default() -> None:
    """默认不得碰用户真实 Vault。"""
    result = run_demo("--quiet")
    assert "knowledgeflow-demo-" in result.stdout


def test_demo_is_isolated_per_scenario() -> None:
    """每个场景一个库、一个 vault 子目录 —— 否则去重会把后面的场景短路掉。"""
    result = run_demo("--quiet")
    assert "manual/KnowledgeFlow/Processed/" in result.stdout
    assert "duplicate/KnowledgeFlow/Processed/" in result.stdout


def test_demo_shows_both_dedup_reasons() -> None:
    result = run_demo("--quiet")
    assert "source_id" in result.stdout
    assert "content_hash" in result.stdout


def test_demo_writes_processed_and_failed_notes() -> None:
    result = run_demo("--quiet")
    assert "KnowledgeFlow/Processed/" in result.stdout
    # invalid 场景按预期失败 → 落 Failed/
    assert "KnowledgeFlow/Failed/" in result.stdout


def test_single_scenario_selection() -> None:
    result = run_demo("invalid", "--quiet")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "LLM_INVALID_OUTPUT" in result.stdout
    assert "douyin" not in result.stdout


def test_fenced_scenario_reports_repair_path() -> None:
    result = run_demo("fenced", "--quiet")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "completed" in result.stdout


def test_custom_vault_and_db_dir_are_used(tmp_path: Path) -> None:
    vault = tmp_path / "myvault"
    db_dir = tmp_path / "mydb"
    result = run_demo(
        "manual", "--quiet", "--vault", str(vault), "--db-dir", str(db_dir)
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (db_dir / "manual.db").is_file()
    notes = sorted((vault / "manual" / "KnowledgeFlow" / "Processed").glob("*.md"))
    assert len(notes) == 1


def test_show_note_prints_markdown(tmp_path: Path) -> None:
    vault = tmp_path / "vault2"
    result = run_demo("manual", "--quiet", "--show-note", "--vault", str(vault))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "## 相关实体" in result.stdout
    assert "[[中国]]" in result.stdout


def test_script_is_importable_without_side_effects() -> None:
    """脚本可以被 import（例如被别的自动化调用），且 import 时不会跑 main。"""
    sys.path.insert(0, str(BACKEND_ROOT / "scripts"))
    try:
        import demo_pipeline  # noqa: PLC0415

        assert demo_pipeline.ALL == "all"
        assert set(demo_pipeline.SCENARIOS) >= {
            "manual",
            "duplicate",
            "hash_dedup",
            "invalid",
            "long",
        }
        assert demo_pipeline.SCENARIOS["hash_dedup"].expect_dedup_by == "content_hash"
    finally:
        sys.path.remove(str(BACKEND_ROOT / "scripts"))
        sys.modules.pop("demo_pipeline", None)
