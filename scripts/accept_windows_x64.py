"""便携包验收：把 ``dist`` 里的 ZIP 解压到干净目录，在「干净机器」条件下真跑一遍。

**为什么需要它**（不是重复劳动，是补一个没人覆盖的缺口）：

``backend/tests/test_windows_packaging.py`` 的端到端用例走的是**开发布局**
（``KF_DEV_RUNTIME`` 指向当前 venv），从没验过「便携包靠自己的标记找到安装目录 +
用内置 runtime 起后端」这条**真实用户路径**；构建脚本第 7 步的冒烟测试也只跑
``--check``，不真的起服务。

而这个脚本第一次跑就抓到一个真缺陷：生成的 ``KnowledgeFlow.cmd`` 是 LF 行尾，
cmd.exe 把 ``rem`` 注释当命令执行，用户每次跑都会看到几行
「'xxx' 不是内部或外部命令」。82 条打包测试 + 构建脚本自己的冒烟测试**全都
没发现** —— 因为它只在「解压出来的包」上才暴露。

**另一条教训**：第一次跑的时候 26 项断言全过，噪音就夹在中间几行。所以这个脚本
把每条命令的**原始输出整段打出来** —— 别只看 ``[OK]``，断言绿不等于用户看到的
输出是干净的。

用法（在 Windows 上，仓库根目录）：

    python scripts/accept_windows_x64.py                 # 验收最新的 ZIP
    python scripts/accept_windows_x64.py --zip <路径>     # 指定 ZIP
    python scripts/accept_windows_x64.py --keep          # 保留解压目录，便于排查

全程离线：非交互向导只校验 Vault 可写，不发起任何模型调用。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DIST_DIR = REPO_ROOT / "dist"

#: 起后端最多等多久（首次要 import fastapi / uvicorn，冷启动会慢一点）
START_TIMEOUT_SECONDS = 180.0
#: 单条命令的超时
COMMAND_TIMEOUT_SECONDS = 240


class Report:
    """收集断言结果，最后统一汇报 —— 不要跑到一半就退出，能一次看到全貌。"""

    def __init__(self) -> None:
        self.failures: list[str] = []

    def say(self, text: str = "") -> None:
        print(text, flush=True)

    def check(self, label: str, ok: bool, detail: str = "") -> bool:
        print(f"  [{'OK  ' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""), flush=True)
        if not ok:
            self.failures.append(label)
        return ok


def decode(raw: bytes) -> str:
    """解子进程输出。中文 Windows 的管道是 GBK，别只按 UTF-8 解（会得到乱码）。"""
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def http_status(url: str, timeout: float = 3.0) -> int | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except Exception:
        return None


def clean_env(fake_home: Path, vault: Path) -> dict[str, str]:
    """模拟一台干净的 Windows：只留必要变量。

    **必须清掉 ``PYTHONIOENCODING`` / ``PYTHONUTF8``**：构建机上顺手设的 ``utf-8``
    会让内嵌解释器绕过 GBK 输出问题 —— 于是「中文 Windows 上 ``--check`` 抛
    ``UnicodeEncodeError``」这个真实缺陷在这里永远测不出来（构建脚本第 7 步踩过
    同一个坑，那里也做了同样的清理）。
    """
    env = dict(os.environ)
    for key in (
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PYTHONPATH",
        "PYTHONHOME",
        "KF_DEV_RUNTIME",
        "OBSIDIAN_VAULT_PATH",
        "DATABASE_URL",
        "DOWNLOAD_DIR",
    ):
        env.pop(key, None)
    env.update(
        {
            # 三个一起设：Windows 上 Path.home() 只认 USERPROFILE，
            # 而数据目录优先读 LOCALAPPDATA。少任何一个都会写到真实用户目录。
            "HOME": str(fake_home),
            "USERPROFILE": str(fake_home),
            "LOCALAPPDATA": str(fake_home / "AppData" / "Local"),
            "KF_SETUP_NONINTERACTIVE": "1",
            "KF_VAULT_PATH": str(vault),
            # 假 Key：非交互向导只校验 Vault，不发起真实模型调用
            "KF_LLM_API_KEY": "sk-not-a-real-key-for-acceptance",
            "KF_LLM_PROVIDER": "deepseek",
            "KF_LLM_MODEL": "deepseek-flash",
        }
    )
    return env


def newest_zip() -> Path | None:
    candidates = sorted(DIST_DIR.glob("KnowledgeFlow-*-windows-x64.zip"))
    return candidates[-1] if candidates else None


def run_command(
    report: Report, label: str, argv: list[str], env: dict[str, str], cwd: Path
) -> tuple[int, str]:
    """跑一条命令，并把**原始输出整段**打出来。

    打原始输出是刻意的：第一次跑这个脚本时 26 项断言全过，噪音就夹在中间几行 ——
    只看 ``[OK]`` 会漏掉「用户看到的输出是脏的」。
    """
    report.say(f"\n--- {label} ---")
    report.say("  $ " + " ".join(argv))
    try:
        proc = subprocess.run(
            argv, capture_output=True, env=env, cwd=str(cwd), timeout=COMMAND_TIMEOUT_SECONDS
        )
    except (OSError, subprocess.SubprocessError) as exc:
        report.say(f"  （起不来：{exc}）")
        return -1, ""
    text = decode(proc.stdout + proc.stderr)
    for line in text.splitlines():
        report.say(f"  | {line}")
    report.say(f"  → 退出码 {proc.returncode}")
    return proc.returncode, text


def entry_command(app: Path, *args: str) -> list[str]:
    """``KnowledgeFlow.cmd`` 的调用方式。

    走 ``cmd.exe /c``：批处理必须由 cmd 解释（顺带也就测到了它的行尾 ——
    LF 行尾会在这里冒出「不是内部或外部命令」）。
    """
    return ["cmd.exe", "/c", str(app / "KnowledgeFlow.cmd"), *args]


def check_cmd_file(report: Report, app: Path) -> None:
    """``.cmd`` 的行尾与字符集。

    cmd.exe **不认 LF-only 的批处理**：它按错误的边界切行，把 ``rem`` 后面的片段
    当命令去执行。而且批处理是**按字节**解析的，多字节字符的次字节可能落在 cmd 的
    特殊字符上（``|`` 0x7C、``&`` 0x26、``<`` 0x3C、``>`` 0x3E、``^`` 0x5E）——
    这跟控制台码页无关。
    """
    report.say("\n[2] KnowledgeFlow.cmd 的行尾与字符集")
    raw = (app / "KnowledgeFlow.cmd").read_bytes()
    crlf = raw.count(b"\r\n")
    bare_lf = raw.count(b"\n") - crlf
    non_ascii = sum(1 for byte in raw if byte > 127)
    report.check("行尾全是 CRLF（没有裸 LF）", bare_lf == 0, f"CRLF={crlf} 裸LF={bare_lf}")
    report.check("内容是纯 ASCII", non_ascii == 0, f"非 ASCII 字节={non_ascii}")


def start_service(
    report: Report, argv: list[str], env: dict[str, str], cwd: Path, base: str
) -> subprocess.Popen[bytes] | None:
    report.say("\n  $ " + " ".join(argv) + "   （后台启动，等 /api/health）")
    proc = subprocess.Popen(
        argv, env=env, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if http_status(f"{base}/api/health") == 200:
            report.check("服务就绪（/api/health == 200）", True)
            return proc
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    raw = proc.stdout.read() if proc.stdout else b""
    report.check("服务就绪（/api/health == 200）", False, decode(raw)[-600:] or "超时")
    return None


def wait_until_gone(base: str, seconds: float = 30.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if http_status(f"{base}/api/health") is None:
            return True
        time.sleep(0.4)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="KnowledgeFlow Windows 便携包验收")
    parser.add_argument("--zip", type=Path, default=None, help="要验收的 ZIP（默认取 dist 下最新的）")
    parser.add_argument("--keep", action="store_true", help="保留解压目录，便于排查")
    args = parser.parse_args()

    report = Report()
    report.say("=" * 72)
    report.say("KnowledgeFlow Windows 便携包验收（解压 ZIP → 干净机器 → 真起后端）")
    report.say("=" * 72)

    if os.name != "nt":
        report.say("\n这个脚本只在 Windows 上跑 —— 它验的是 Windows 便携包。")
        return 1

    zip_path = args.zip or newest_zip()
    if zip_path is None or not zip_path.is_file():
        report.say(f"\n找不到 ZIP：{zip_path or DIST_DIR / 'KnowledgeFlow-*-windows-x64.zip'}")
        report.say("先跑 scripts/build_windows_x64.ps1 生成。")
        return 1

    work = Path(tempfile.mkdtemp(prefix="kf-acceptance-"))
    app_root = work / "app"
    fake_home = work / "home"
    vault = work / "vault"
    report.say(f"\n[1] 解压到干净目录\n  ZIP  ：{zip_path}（{zip_path.stat().st_size / 1048576:.1f} MB）")
    report.say(f"  工作区：{work}")

    exit_code = 1
    try:
        app_root.mkdir(parents=True)
        (fake_home / "AppData" / "Local").mkdir(parents=True)
        vault.mkdir(parents=True)
        started = time.monotonic()
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(app_root)
        report.say(f"  解压完成，用时 {time.monotonic() - started:.0f}s")

        app = app_root / zip_path.stem
        report.check("解压出程序目录", app.is_dir(), str(app.name))
        for marker in ("KnowledgeFlow.exe", "KnowledgeFlow.cmd", "runtime/python.exe",
                       "backend/scripts/serve.py", "launcher/run.py"):
            report.check(f"存在 {marker}", (app / marker).is_file())

        env = clean_env(fake_home, vault)
        app_home = Path(env["LOCALAPPDATA"]) / "KnowledgeFlow"
        report.say(f"  模拟用户数据目录：{app_home}")

        # ---- 1. 冻结 exe 的 --check：便携识别 + GBK 输出 ----
        report.say("\n[1.5] KnowledgeFlow.exe --check（真 exe；stdout 是管道 = GBK）")
        code, out = run_command(report, "exe --check", [str(app / "KnowledgeFlow.exe"), "--check"], env, app)
        report.check("退出码为 0", code == 0, f"实际 {code}")
        report.check("没有 traceback", "Traceback" not in out and "UnicodeEncodeError" not in out)
        report.check("识别到内置 runtime（不是「缺少」）", "内置运行环境 —— 已就绪" in out)
        console_log = app_home / "logs" / "console.log"
        report.check("产出 console.log（双击启动时唯一的线索）", console_log.is_file())
        if console_log.is_file():
            logged = console_log.read_text(encoding="utf-8", errors="replace")
            report.check(
                "console.log 是完整 UTF-8（含 ✓ 或 [OK]）",
                "内置运行环境" in logged and ("✓" in logged or "[OK]" in logged),
            )

        # ---- 2. .cmd 体检 ----
        check_cmd_file(report, app)

        # ---- 3. .cmd --check ----
        report.say("\n[3] KnowledgeFlow.cmd --check（走内嵌 runtime\\python.exe）")
        code, out = run_command(report, ".cmd --check", entry_command(app, "--check"), env, app)
        report.check("退出码为 0", code == 0, f"实际 {code}")
        report.check("输出含「内置运行环境」", "内置运行环境" in out)
        report.check("没有 traceback", "Traceback" not in out)
        report.check(
            "没有 cmd.exe 的解析噪音（LF 行尾的症状）",
            "不是内部或外部命令" not in out,
            "，".join(out.strip().splitlines()[:3])[:160],
        )

        # ---- 4. 真起后端 ----
        report.say("\n[4] 真起一次后端（非交互向导 → 内置 runtime → uvicorn）")
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        proc = start_service(
            report, entry_command(app, "--port", str(port), "--no-browser", "--no-window"),
            env, app, base,
        )
        if proc is None:
            report.say("\n服务没起来，后面的用例跳过。")
            return 1

        try:
            with urllib.request.urlopen(f"{base}/api/health", timeout=5) as response:
                health = json.loads(response.read().decode("utf-8"))
            report.check("健康响应是「自己人」（带版本号）", bool(health.get("version")), str(health)[:120])
            report.check("Web UI 可访问（/ == 200）", http_status(f"{base}/") == 200)
            report.check("数据库落在模拟用户目录", (app_home / "data" / "knowledgeflow.db").is_file())
            report.check(
                "解压目录里没有 data/（用户数据没混进程序目录）",
                not (app / "data").exists() and not (app / "backend" / "data").exists(),
            )

            # ---- 5. --status / --stop ----
            report.say("\n[5] --status / --stop")
            code, out = run_command(
                report, ".cmd --status", entry_command(app, "--port", str(port), "--status"), env, app
            )
            report.check("--status 退出码 0", code == 0, f"实际 {code}")
            report.check("--status 报「运行中」", "运行中" in out)

            code, out = run_command(
                report, ".cmd --stop", entry_command(app, "--port", str(port), "--stop"), env, app
            )
            report.check("--stop 退出码 0", code == 0, f"实际 {code}")
            report.check("--stop 报「已停止」", "已停止" in out)
            report.check("端口已释放（不留孤儿进程）", wait_until_gone(base))

            try:
                proc.wait(timeout=60)
                report.check("启动器自己退出了", True, f"退出码 {proc.returncode}")
            except subprocess.TimeoutExpired:
                report.check("启动器自己退出了", False, "60 秒没退")
                proc.kill()
        finally:
            if proc.poll() is None:
                proc.kill()

        # ---- 6. 重启复用同一个数据库 ----
        report.say("\n[6] 再起一次：应当复用同一个数据库")
        proc2 = start_service(
            report, entry_command(app, "--port", str(port), "--no-browser", "--no-window"),
            env, app, base,
        )
        if proc2 is not None:
            try:
                report.check("数据库文件还在（没被重建）", (app_home / "data" / "knowledgeflow.db").is_file())
                code, _ = run_command(
                    report, ".cmd --stop（第二次）",
                    entry_command(app, "--port", str(port), "--stop"), env, app,
                )
                report.check("收干净", code == 0, f"实际 {code}")
            finally:
                if proc2.poll() is None:
                    try:
                        proc2.wait(timeout=60)
                    except subprocess.TimeoutExpired:
                        proc2.kill()

        exit_code = 1 if report.failures else 0
    finally:
        if args.keep:
            report.say(f"\n（--keep：保留 {work}）")
        else:
            shutil.rmtree(work, ignore_errors=True)

    report.say("\n" + "=" * 72)
    if report.failures:
        report.say(f"结论：{len(report.failures)} 项未通过")
        for item in report.failures:
            report.say(f"  - {item}")
    else:
        report.say("结论：全部通过 —— 便携包在「干净机器」上确实能用")
    report.say("=" * 72)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
