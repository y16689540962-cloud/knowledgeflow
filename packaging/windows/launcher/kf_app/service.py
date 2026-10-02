"""后端进程的生命周期：启动 / 探活 / 复用 / 关闭。

四条硬要求（与 macOS 版一致）：

1. 只监听 ``127.0.0.1``，**绝不用 ``0.0.0.0``**
2. **单实例**：已经有服务在跑就复用它，不再起第二个（否则 ``Address already in use``）
3. 退出时**收干净**，不留孤儿进程
4. 被强杀之后再次启动也能恢复（靠「探活优先于状态文件」实现）

第 4 条的实现关键：**不信状态文件，信端口**。状态文件只是辅助信息；
判断「是否已在运行」永远以 ``/api/health`` 的实际应答为准。

**Windows 上有三个陷阱，都在这一个文件里绕开**：

* ``os.kill(pid, 0)`` **不是探活**。POSIX 上它是「发 0 号信号」，Windows 上
  CPython 会把它落到 ``TerminateProcess`` —— 拿它探活等于**把进程杀掉**，
  然后报告「进程不在了」。改用 ``OpenProcess`` + ``GetExitCodeProcess``。
* 没有 ``SIGTERM``，也没有 ``os.killpg``。收进程树要用 ``taskkill /T``。
* 子进程会弹出一个黑色控制台窗口。用 ``CREATE_NO_WINDOW`` 掐掉 ——
  否则用户双击图标后会莫名多一个 cmd 窗口，而且关掉它就会连带杀掉后端。
"""

from __future__ import annotations

import json
import os
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
#: 优雅关闭等待时间；超时再强杀进程树。
TERMINATE_TIMEOUT_SECONDS = 10.0

#: ``subprocess`` 的创建标志。非 Windows 上取不到这两个常量，
#: 用字面量兜底 —— 这样本模块在 POSIX 上也**能 import**（便于跑 lint 与静态检查），
#: 只是不会被真正调用。
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)

#: ``GetExitCodeProcess`` 的「仍在运行」哨兵值。
STILL_ACTIVE = 259
#: ``OpenProcess`` 的访问权限：只查状态，不要终止权限。
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

#: 外部命令（taskkill / netstat）的超时。
EXTERNAL_TOOL_TIMEOUT_SECONDS = 15.0


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


#: 单次探活的重试次数。**为什么必须重试**：机器忙的时候（比如同时在跑测试套件、
#: 或杀毒软件正在扫这个刚解压的目录）一次 1.5 秒的探活可能超时，于是启动器会误判
#: 「服务没在运行」—— 轻则把 ``--status`` 报成「未运行」，重则把自己的服务当成
#: 「端口被其他程序占用」，于是拒绝启动、还给出错误的排查方向。
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

    **判定只信 ``/api/health``，不信状态文件**：用户从任务管理器强杀启动器之后
    后端可能还活着，这时状态文件已经没意义了，但服务确实还在跑 ——
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
# 状态文件（辅助信息，不是判定依据）
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
    """进程是否还活着。

    **绝对不能用 ``os.kill(pid, 0)``**：POSIX 上那是「发 0 号信号」（探活），
    但 CPython 在 Windows 上会把任何非 CTRL_* 的信号落到 ``TerminateProcess`` ——
    于是「探活」变成了「杀掉它」，紧接着报告「进程已退出」。
    这条错误极其隐蔽：调用方看到的结果永远自洽（问了就说没了），
    只有被探的那个进程莫名其妙消失时才暴露。

    改用 ``OpenProcess`` + ``GetExitCodeProcess``：只申请查询权限，
    不申请 ``PROCESS_TERMINATE``，从权限层面就不可能误杀。
    """
    if pid <= 0:
        return False
    if sys.platform != "win32":  # pragma: no cover - 便于在 POSIX 上跑 lint
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # 打不开：进程不存在，或者我们没有权限看它（后者按「存在」处理更保守）。
        return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


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
    """拉起后端。``cwd`` 固定为 ``backend``，与下面两件事强相关：

    * 后端模块要靠 ``serve.py`` 自己把 backend 根加进 ``sys.path``
    * 我们用**绝对路径**喂 ``DATABASE_URL`` / ``DOWNLOAD_DIR``，所以 cwd 影响不到数据位置

    ``spawn`` 是留给测试的接缝：注入一个假的进程工厂，就能断言真实的 argv 与
    creationflags。为什么不 patch ``subprocess.Popen``：那是**全局**打补丁，
    会把同一进程里所有用到 Popen 的代码一起换掉。
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
        # CREATE_NO_WINDOW：不给后端弹一个黑色控制台窗口
        # CREATE_NEW_PROCESS_GROUP：让它自成一组，``taskkill /T`` 能整组收掉
        creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
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
    启动期最长可以等 60 秒，而这段时间里用户完全可能已经关掉应用了。
    没有它，调用方就得白等满一轮超时，用户看到的是「点了退出没反应」。
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
    """**只关自己拉起来的那个**。别人的服务不动。

    Windows 没有 ``SIGTERM``，``Popen.terminate()`` 就是 ``TerminateProcess`` ——
    后端拿不到任何「该收尾了」的信号，SQLite 也不会走优雅关闭。这是平台差异，
    不是可以绕开的：SQLite 靠回滚日志/WAL 保证崩溃安全，强杀不会损坏数据库，
    但**进程树**必须自己收干净（后端可能起过子进程），所以退一步用 ``taskkill /T``。
    """
    process = handle.process
    if process is None or process.poll() is not None:
        return

    try:
        process.terminate()
    except OSError:
        pass
    if _wait_gone(process, timeout):
        return

    # 还没走（可能卡在 C 扩展里，TerminateProcess 也要等它响应）→ 整棵树强收
    taskkill(process.pid, force=True)
    _wait_gone(process, 5.0)


def _wait_gone(process: subprocess.Popen[bytes], timeout: float) -> bool:
    try:
        process.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def taskkill(pid: int, *, force: bool = True) -> bool:
    """用 ``taskkill`` 收掉进程**树**（``/T``）。返回是否成功。

    **不收文本输出**：``taskkill`` 在中文 Windows 上按 GBK 写输出，
    而 ``text=True`` 会按 UTF-8 解码 → ``subprocess`` 的读取线程抛
    ``UnicodeDecodeError``。这里只要返回码，收字节即可，与平台码页无关。
    """
    args = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        args.append("/F")
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            timeout=EXTERNAL_TOOL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _listener_pid(port: int) -> int | None:
    """用 ``netstat -ano`` 找出监听该端口的进程。

    为什么需要它：状态文件可能丢了（用户从任务管理器强杀了启动器，或者删了
    runtime 目录），但服务还在跑。这时只有端口上的监听者才是真相。

    macOS 版这里用 ``lsof``，Windows 对应物是 ``netstat -ano``（``-o`` 给出 PID）。
    输出形如::

        TCP    127.0.0.1:8000    0.0.0.0:0    LISTENING    12345

    只认 ``127.0.0.1:<port>`` / ``[::1]:<port>`` 的 LISTENING 行，
    避免把「某个连到该端口的客户端」当成监听者。

    解码用 ``errors="replace"``：``netstat`` 在中文 Windows 上按 GBK 输出，
    而 ``text=True`` 默认按 UTF-8 解 —— 直接抛 ``UnicodeDecodeError``。
    要解析的字段（协议名、IP、端口、PID）全是 ASCII，
    把解不出的字节替成 ``?`` 完全不影响判断。
    """
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=EXTERNAL_TOOL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    wanted = {f"127.0.0.1:{port}", f"[::1]:{port}"}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[0].upper() != "TCP":
            continue
        if fields[1] not in wanted or fields[3].upper() != "LISTENING":
            continue
        if fields[4].isdigit():
            return int(fields[4])
    return None


def stop_by_state(paths: AppPaths, port: int) -> bool:
    """停掉后台服务。返回**是否真的停掉了一个**。

    以前的 macOS 实现有个谎报：状态文件不在时它直接 ``return True``（"已停止"），
    可服务明明还在跑。契约是：状态文件 → 退回端口查监听者 → 杀完**复查**
    确实没了，才敢说停掉了。
    """
    state = read_state(paths)
    pid = int(state.get("pid") or 0) if state else 0
    if not pid_alive(pid):
        pid = _listener_pid(port) or 0  # 状态文件不可信 → 问端口

    if not pid or pid == os.getpid():
        clear_state(paths)
        return False

    taskkill(pid, force=False)  # 先温和：不 /F，让它有机会自己收尾
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and pid_alive(pid):
        time.sleep(0.2)
    if pid_alive(pid):
        taskkill(pid, force=True)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and pid_alive(pid):
            time.sleep(0.2)

    clear_state(paths)
    return not pid_alive(pid)


def open_browser(url: str) -> None:
    """用系统默认浏览器打开。失败不抛 —— 打不开也不该让应用崩。"""
    try:
        if sys.platform == "win32":
            os.startfile(url)  # type: ignore[attr-defined]  # noqa: S606 - 只开自己的回环地址
        else:  # pragma: no cover - 便于在 POSIX 上跑逻辑测试
            import webbrowser

            webbrowser.open(url)
    except OSError:
        pass


__all__ = [
    "CREATE_NEW_PROCESS_GROUP",
    "CREATE_NO_WINDOW",
    "PROBE_ATTEMPTS",
    "PROBE_TIMEOUT_SECONDS",
    "STARTUP_TIMEOUT_SECONDS",
    "TERMINATE_TIMEOUT_SECONDS",
    "ServiceHandle",
    "clear_state",
    "find_our_service",
    "health",
    "health_with_retry",
    "open_browser",
    "pid_alive",
    "port_busy",
    "read_state",
    "start",
    "stop_by_state",
    "tail_log",
    "taskkill",
    "terminate",
    "wait_healthy",
    "write_state",
]
