"""后端进程的生命周期：启动 / 探活 / 复用 / 关闭。

四条硬要求（来自定稿第十一、十二、十三节）：

1. 只监听 ``127.0.0.1``，**绝不用 ``0.0.0.0``**
2. **单实例**：已经有服务在跑就复用它，不再起第二个（否则 ``Address already in use``）
3. 退出时**收干净**，不留孤儿进程
4. 被 ``kill -9`` 之后再次启动也能恢复（靠「探活优先于 pid 文件」实现）

第 4 条的实现关键：**不信 pid 文件，信端口**。
pid 文件只是辅助信息；判断「是否已在运行」永远以 ``/api/health`` 的实际应答为准。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Callable

from kf_app.paths import AppPaths

#: 探活超时（秒）。冷启动要建表 + 可能跑启动恢复，给足 60 秒。
STARTUP_TIMEOUT_SECONDS = 60.0
#: 单次探活请求超时。
PROBE_TIMEOUT_SECONDS = 1.5
#: 优雅关闭等待时间；超时再 SIGKILL。
TERMINATE_TIMEOUT_SECONDS = 10.0


@dataclass
class ServiceHandle:
    """一个正在运行（或发现已在运行）的后端服务。"""

    port: int
    pid: int | None = None
    process: subprocess.Popen[bytes] | None = None
    #: 是不是本次启动拉起来的（决定退出时要不要关它）
    owned: bool = False

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


# --------------------------------------------------------------------------- #
# 探活
# --------------------------------------------------------------------------- #
def health(port: int, *, timeout: float = PROBE_TIMEOUT_SECONDS) -> dict[str, Any] | None:
    """取 ``/api/health``。拿不到或不是我们的服务 → ``None``。"""
    url = f"http://127.0.0.1:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if response.status != 200:
                return None
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError, ValueError):
        return None
    # 端口上可能有**别的**程序也在跑 HTTP —— 必须确认是我们的
    if not isinstance(payload, dict) or "llm_provider" not in payload or "version" not in payload:
        return None
    return payload


#: 单次探活的重试次数。**为什么必须重试**：机器忙的时候（比如同时在跑测试套件）
#: 一次 1.5 秒的探活可能超时，于是启动器会误判「服务没在运行」——
#: 轻则把 ``--status`` 报成「未运行」，重则把自己的服务当成「端口被其他程序占用」，
#: 于是拒绝启动、还给出错误的排查方向。实测这让端到端测试时过时不过。
PROBE_ATTEMPTS = 3


def health_with_retry(
    port: int,
    *,
    attempts: int = PROBE_ATTEMPTS,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> dict[str, Any] | None:
    """带重试的探活。判定「服务在不在」一律走这条路径。"""
    for attempt in range(max(1, attempts)):
        payload = health(port, timeout=timeout)
        if payload is not None:
            return payload
        if attempt < attempts - 1:
            time.sleep(0.3)
    return None


def port_busy(port: int) -> bool:
    """端口是否被占用（不管是不是我们的服务）。"""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def find_our_service(port: int, paths: AppPaths | None = None) -> ServiceHandle | None:
    """端口上已经有 KnowledgeFlow 在跑 → 返回它的句柄（``owned=False``）。

    **判定只信 ``/api/health``，不信 pid 文件**：用户 ``kill -9`` 掉启动器之后
    后端可能还活着，这时 pid 文件已经没意义了，但服务确实还在跑 ——
    以探活为准，才能既不复用错、也不起第二个。
    """
    if health_with_retry(port) is None:
        return None
    pid: int | None = None
    if paths is not None:
        state = read_state(paths)
        if state:
            pid = int(state.get("pid") or 0) or None
    return ServiceHandle(port=port, pid=pid, owned=False)


# --------------------------------------------------------------------------- #
# pid 状态文件（辅助信息，不是判定依据）
# --------------------------------------------------------------------------- #
def _state_file(paths: AppPaths) -> Path:
    return paths.pid_file


def write_state(paths: AppPaths, *, pid: int, port: int) -> None:
    try:
        paths.run.mkdir(parents=True, exist_ok=True)
        _state_file(paths).write_text(
            json.dumps({"pid": pid, "port": port, "started_at": time.time()}),
            encoding="utf-8",
        )
    except OSError:
        pass  # 状态文件写不了不该阻止服务运行


def read_state(paths: AppPaths) -> dict[str, Any] | None:
    try:
        return json.loads(_state_file(paths).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def clear_state(paths: AppPaths) -> None:
    try:
        _state_file(paths).unlink(missing_ok=True)
    except OSError:
        pass


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在，只是不是我们的
    return True


# --------------------------------------------------------------------------- #
# 启动 / 关闭
# --------------------------------------------------------------------------- #
def start(
    paths: AppPaths,
    env: dict[str, str],
    port: int,
    *,
    log_path: Path,
    spawn: Callable[..., subprocess.Popen[bytes]] | None = None,
) -> ServiceHandle:
    """拉起后端。``cwd`` 固定为 ``Resources/backend``，与下面两件事强相关：

    * 后端模块要靠 ``serve.py`` 自己把 backend 根加进 ``sys.path``
    * 我们用**绝对路径**喂 ``DATABASE_URL`` / ``DOWNLOAD_DIR``，所以 cwd 影响不到数据位置

    ``spawn`` 是留给测试的接缝：注入一个假的进程工厂，就能断言真实的 argv。

    为什么不直接在测试里 patch ``subprocess.Popen``：那是**全局**打补丁，
    会把同一进程里所有用到 Popen 的代码一起换掉 —— 实测把运行环境的
    Python 垫片（它内部也要起子进程）搞崩了，报
    ``'_FakeProcess' object does not support the context manager protocol``。
    留一个显式参数，测试与生产都不受影响。
    """
    launcher = spawn or subprocess.Popen
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle: IO[bytes] = open(log_path, "ab", buffering=0)

    banner = f"\n===== KnowledgeFlow 后端启动 {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n"
    log_handle.write(banner.encode("utf-8"))

    process = launcher(
        [str(paths.runtime_python), str(paths.backend / "scripts" / "serve.py"), "--port", str(port)],
        cwd=str(paths.backend),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # 自成进程组：退出时能整组收掉，不留孤儿
    )
    write_state(paths, pid=process.pid, port=port)
    return ServiceHandle(port=port, pid=process.pid, process=process, owned=True)


def wait_healthy(
    handle: ServiceHandle,
    *,
    timeout: float = STARTUP_TIMEOUT_SECONDS,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """等后端真正可用。进程中途退出 → 立刻放弃（省得白等一分钟）。

    ``should_stop`` 是「启动过程中用户要求退出」的探针。**这个参数不是可选的装饰**：
    启动期最长可以等 60 秒，而这段时间里用户完全可能已经关掉应用了
    （Dock 右键「退出」= SIGTERM）。没有它，调用方就得白等满一轮超时，
    用户看到的是「点了退出没反应」。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if should_stop is not None and should_stop():
            return False  # 用户要求退出
        if handle.process is not None and handle.process.poll() is not None:
            return False  # 进程已经死了
        if health(handle.port) is not None:
            return True
        time.sleep(0.4)
    return False


def tail_log(log_path: Path, *, lines: int = 25) -> str:
    """取日志尾部，用于把人话错误显示给用户（而不是 traceback）。"""
    try:
        content = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(content[-lines:])


def terminate(handle: ServiceHandle, *, timeout: float = TERMINATE_TIMEOUT_SECONDS) -> None:
    """**只关自己拉起来的那个**。别人的服务不动。"""
    process = handle.process
    if process is None or process.poll() is not None:
        return

    _signal_group(process, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return
        time.sleep(0.2)
    _signal_group(process, signal.SIGKILL)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _signal_group(process: subprocess.Popen[bytes], sig: int) -> None:
    """优先给**进程组**发信号（后端可能自己 fork 子进程）。"""
    try:
        os.killpg(os.getpgid(process.pid), sig)
    except (ProcessLookupError, PermissionError):
        try:
            process.send_signal(sig)
        except ProcessLookupError:
            pass


def _listener_pid(port: int) -> int | None:
    """用 ``lsof`` 找出监听该端口的进程。

    为什么需要它：状态文件可能丢了（用户 ``kill -9`` 了启动器，或者删了 runtime
    目录），但服务还在跑。这时只有端口上的监听者才是真相。
    """
    try:
        result = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    first = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    return int(first) if first.isdigit() else None


def stop_by_state(paths: AppPaths, port: int) -> bool:
    """停掉后台服务。返回**是否真的停掉了一个**。

    以前的实现有个谎报：状态文件不在时它直接 ``return True``（"已停止"），
    可服务明明还在跑。现在改成：状态文件 → 退回 ``lsof`` 找监听者，
    杀完还要**复查**确实没了，才敢说停掉了。
    """
    state = read_state(paths)
    pid = int(state.get("pid") or 0) if state else 0
    if not pid_alive(pid):
        pid = _listener_pid(port) or 0  # 状态文件不可信 → 问端口

    if not pid or pid == os.getpid():
        clear_state(paths)
        return False

    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                break
        deadline = time.monotonic() + (10 if sig == signal.SIGTERM else 5)
        while time.monotonic() < deadline:
            if not pid_alive(pid):
                break
            time.sleep(0.2)
        if not pid_alive(pid):
            break

    clear_state(paths)
    return not pid_alive(pid)


def open_browser(url: str) -> None:
    """用系统默认浏览器打开。失败不抛 —— 打不开也不该让应用崩。"""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", url], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:  # 便于在 Linux CI 上跑逻辑测试
            import webbrowser

            webbrowser.open(url)
    except OSError:
        pass
