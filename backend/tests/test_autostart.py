"""``scripts/install_autostart.sh``（Phase 10 的开机自启）。

**不真的装、不真的卸载** —— 那会动用户 ``~/Library/LaunchAgents``。
这里只验「生成的 plist 内容对不对」，行为对错靠 ``--print`` 就能看出来：

* 绑的是 127.0.0.1（**没有** ``--expose``）
* RunAtLoad + KeepAlive 都在（少一个就做不到「开机就有、挂了自拉」）
* 用的是仓库里的 venv python，不是系统 python
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = BACKEND_ROOT / "scripts" / "install_autostart.sh"
LABEL = "cn.knowledgeflow.serve"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=str(BACKEND_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="module")
def plist() -> dict:
    result = run("--print")
    assert result.returncode == 0, result.stderr
    return plistlib.loads(result.stdout.encode("utf-8"))


def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.is_file()
    if os.name == "nt":
        # Windows 没有 Unix 可执行位（st_mode 恒为 0o666），这条断言无意义。
        # 本脚本是 macOS 的 launchd 配置，Windows 上由 install_autostart.ps1 承担。
        pytest.skip("Windows 没有 Unix 可执行位概念")
    assert SCRIPT.stat().st_mode & 0o111, "脚本没有可执行位"


def test_print_emits_a_valid_plist(plist: dict) -> None:
    """``--print`` 在**没有 venv 的全新 clone** 上也得能用（CI 就是那个状态）。"""
    assert plist["Label"] == LABEL


def test_runs_the_serve_script_from_the_repo(plist: dict) -> None:
    args = plist["ProgramArguments"]
    assert args[0].endswith("python") or args[0].endswith("python3"), f"不是 python：{args[0]}"
    assert args[1].endswith("/backend/scripts/serve.py"), f"入口不对：{args[1]}"
    # 有 venv 时**必须**用它（依赖装在那儿）；全新 clone 上退到系统 python3
    venv = BACKEND_ROOT.parent / ".venv" / "bin" / "python"
    if venv.exists():
        assert args[0] == str(venv), f"有 venv 却没用它：{args[0]}"


#: ``$VAR`` 后面紧跟非 ASCII 字符的写法。
BARE_VAR_BEFORE_MULTIBYTE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7F]")


def strip_shell_comments(source: str) -> str:
    """剥掉整行注释再匹配。

    **必须剥**：这条守卫的用法说明就写在脚本的注释里（`$VAR（` 本身就是
    要举例的坏写法），不剥的话检查器会把自己的文档当证据 ——
    本项目已经栽过好几次了。
    """
    return "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )


def test_no_bare_variable_before_multibyte() -> None:
    """变量一律写 ``${VAR}``：``$VAR（`` 会把中文字节当成变量名的一部分。

    实测（在干净 clone、没有 .venv 的情况下）报
    ``VENV_PYTHON\xef: unbound variable`` —— 而且那行只在错误分支才展开，
    本地有 .venv 时永远走不到，等发布出去才炸。所以用静态守卫钉死写法。
    """
    source = strip_shell_comments(SCRIPT.read_text(encoding="utf-8"))
    offenders = [
        line.strip() for line in source.splitlines() if BARE_VAR_BEFORE_MULTIBYTE.search(line)
    ]
    assert offenders == [], f"变量后面紧跟非 ASCII，请写成 ${{VAR}}：{offenders}"


@pytest.mark.parametrize("key", ["RunAtLoad", "KeepAlive"])
def test_survives_reboot_and_crashes(plist: dict, key: str) -> None:
    """开机就有（RunAtLoad）+ 挂了自拉（KeepAlive）—— 少了任何一个都不算自启。"""
    assert plist[key] is True


def test_never_exposes_beyond_localhost(plist: dict) -> None:
    """服务没有鉴权，暴露出去等于开放「写 Obsidian 库 / 用 LLM 额度」。"""
    args = plist["ProgramArguments"]
    assert "--expose" not in args, "自启配置里不许出现 --expose"
    assert "--host" not in args, "自启配置不许改监听地址"


def test_logs_go_to_a_file(plist: dict) -> None:
    """挂了要能查：stdout / stderr 必须落盘（后台进程没人看终端）。"""
    assert plist["StandardOutPath"].endswith("serve.log")
    assert plist["StandardErrorPath"].endswith("serve.err")


def test_status_reports_not_installed() -> None:
    """没装时 ``--status`` 要明确说「未运行」，而不是假装成功。"""
    result = run("--status")
    # 装了的话它会返回 0（运行中）—— 那时跳过这条断言
    if result.returncode != 0:
        assert "未运行" in result.stdout, result.stdout


def test_unknown_flag_is_a_usage_error() -> None:
    assert run("--nope").returncode == 2
