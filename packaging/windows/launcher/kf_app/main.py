"""启动器入口。

``KnowledgeFlow.exe``（双击）与 ``KnowledgeFlow.cmd``（命令行）最终都跑这个文件。

设计原则（与 macOS 版一致）：

* 双击时：**已在运行就复用、没在运行就启动** —— 永远不出现第二个实例
* 默认只监听 ``127.0.0.1``
* 服务就绪后自动打开浏览器
* 退出时把后端收干净；被强杀过也能再次启动
* 所有失败都是**一句人话 + 日志路径**，不是 traceback

**Windows 特有的三处**：

1. 没有 ``SIGTERM``。用户「关掉应用」的路径是：控制窗口的「停止并退出」按钮，
   或 ``KnowledgeFlow.cmd --stop``。信号只兜底 ``Ctrl+C``（``SIGINT``）与
   ``Ctrl+Break``（``SIGBREAK``）。
2. ``sys.stdout`` / ``sys.stderr`` 有两种坏法，都要挡：
   无控制台启动时它们是 ``None``，``print()`` 抛 ``AttributeError``；
   从控制台启动时它们是**按 ANSI 码页重开**的真流（中文 Windows 上是 GBK），
   ``print("✓")`` 抛 ``UnicodeEncodeError`` —— 而且此时 ``PYTHONIOENCODING``
   是无效的。两种都炸在错误处理路径上，于是真故障被盖住。开头就接好。
3. 双击后必须留下**看得见的东西**（控制窗口），否则用户关不掉服务。
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from pathlib import Path

from kf_app import control
from kf_app import service as svc
from kf_app import settings as cfg
from kf_app import wizard
from kf_app.paths import AppPaths, ensure_layout, find_app_paths

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_CANCELLED = 3


def _setup_logging(paths: AppPaths) -> logging.Logger:
    logger = logging.getLogger("knowledgeflow.launcher")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    try:
        paths.logs.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.FileHandler(
            paths.logs / "launcher.log", encoding="utf-8"
        )
    except OSError:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


CONSOLE_LOG_NAME = "console.log"


class _Tee:
    """把写入同时送到原始流和 ``console.log``。

    **为什么不只在 ``sys.stdout is None`` 时接管**：PyInstaller 的 ``--noconsole``
    产物里，``sys.stdout`` 既可能真的是 ``None``（双击启动，进程没有控制台），
    也可能是一个**按 ANSI 码页重开**的真流（从 cmd / PowerShell 里启动会继承
    控制台）。后者实测有两个后果：

    1. 写出去的字节按 GBK 编码 —— ``print("✓")`` 抛 ``UnicodeEncodeError``；
    2. ``PYTHONIOENCODING`` 对它**无效**（设成 ``utf-8`` 依旧按 GBK 写）。

    只靠 ``is None`` 判断，等于**默认**「输出有人看得见」—— 而双击启动时恰恰
    没人看得见。所以这里两件事一起做：把流改成「编码装不下就降级」，
    并且**永远**再写一份 ``console.log``，用户把日志发过来就能看到启动器
    当时打印了什么。
    """

    __slots__ = ("_primary", "_log", "encoding", "errors")

    def __init__(self, primary: object | None, log: object) -> None:
        self._primary = primary
        self._log = log
        # print / logging 会读这两个属性来编码；照抄主流的，缺了就用宽松的默认值
        self.encoding = getattr(primary, "encoding", None) or "utf-8"
        self.errors = getattr(primary, "errors", None) or "replace"

    def write(self, text: str) -> int:
        for stream in (self._primary, self._log):
            if stream is None:
                continue
            try:
                stream.write(text)  # type: ignore[attr-defined]
            except (OSError, ValueError, UnicodeError):
                # 主控制台写不进去（句柄失效、编码装不下）不该影响记日志；
                # 日志写不进去（磁盘满、被别的进程占着）也不该让启动器崩掉。
                pass
        return len(text)

    def flush(self) -> None:
        for stream in (self._primary, self._log):
            if stream is None:
                continue
            try:
                stream.flush()  # type: ignore[attr-defined]
            except (OSError, ValueError):
                pass

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        if self._primary is None:
            raise OSError("没有可用的文件描述符（这个进程没有控制台）")
        return self._primary.fileno()  # type: ignore[attr-defined]

    def close(self) -> None:
        """只关日志，**不动用户的控制台** —— 关掉 stdout 会让后续 print 抛异常。"""
        try:
            self._log.close()  # type: ignore[attr-defined]
        except (OSError, ValueError):
            pass


def _make_stream_forgiving(stream: object | None) -> None:
    """让流在「当前编码装不下的字符」上降级，而不是抛异常。

    中文 Windows 的 stdout 是 GBK，``✓``（U+2713）不在码表里。默认的
    ``errors="strict"`` 会把一次 ``print`` 变成致命错误 —— 而这恰恰发生在
    ``--check`` 这种**诊断**路径上：命令越是想告诉用户「环境哪里不对」，
    越容易自己先死掉（实测：退出码 1，stdout 一个字节都没有）。
    ``errors="replace"`` 让它显示成 ``?``：信息少一点，但用户拿到的是一行
    可读的输出，而不是 traceback。
    """
    if stream is None:
        return
    reconfigure = getattr(stream, "reconfigure", None)
    if not callable(reconfigure):
        return
    try:
        reconfigure(errors="replace")
    except (ValueError, OSError, LookupError):
        # 已经被 detach / 关掉的流改不了编码，不是致命问题
        pass


def _attach_std_streams(paths: AppPaths) -> None:
    """把 ``stdout`` / ``stderr`` 变成「永远不丢、永远不炸」。

    两件事：

    1. **无控制台启动时 ``sys.stdout`` 是 ``None``**，``print()`` 会抛
       ``AttributeError: 'NoneType' object has no attribute 'write'`` ——
       而 ``print`` 大多出现在**错误处理路径**上（"启动失败，日志在…"），
       于是真正的故障被一个 AttributeError 盖住，查起来非常费劲。
    2. **有控制台时编码是 ANSI 码页**（中文 Windows 上是 GBK），
       ``print("✓")`` 会抛 ``UnicodeEncodeError``。见 ``_make_stream_forgiving``。

    接到文件上而不是 ``os.devnull``：信息不该丢。这条在双击启动时尤其重要 ——
    那种情况下用户**看不到任何输出**，日志是唯一的线索。所以这里不判断
    「输出是不是没人看得见」（判不准），而是**无条件**记一份。
    """
    try:
        paths.logs.mkdir(parents=True, exist_ok=True)
        log = open(  # noqa: SIM115 - 进程生命周期内常驻，由解释器收尾
            paths.logs / CONSOLE_LOG_NAME, "a", encoding="utf-8", buffering=1
        )
    except OSError:
        # 连日志都开不出来（磁盘满 / 权限异常）：至少把编码问题处理掉，
        # 别让 print 把启动器打死。
        _make_stream_forgiving(sys.stdout)
        _make_stream_forgiving(sys.stderr)
        return

    if sys.stdout is None and sys.stderr is None:
        # 两个都没了：同一份流给两边，用户看到的顺序与真实顺序一致
        sys.stdout = log
        sys.stderr = log
        return

    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        _make_stream_forgiving(stream)
        setattr(sys, name, _Tee(stream, log))


def _notify(text: str) -> None:
    """把消息告诉用户。有图形界面就弹窗，没有就打到 stderr。"""
    print(text, file=sys.stderr)
    if not wizard.gui_available():
        return
    try:
        wizard.say(text)
    except wizard.DialogUnavailable:
        pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="KnowledgeFlow",
        description="KnowledgeFlow 桌面启动器（Windows）",
    )
    parser.add_argument("--port", type=int, default=None, help="覆盖端口（默认 8000）")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument(
        "--no-window", action="store_true", help="不显示控制窗口（后台运行，用于自启/脚本）"
    )
    parser.add_argument("--setup", action="store_true", help="强制重跑首次配置向导")
    parser.add_argument("--status", action="store_true", help="只报告服务状态")
    parser.add_argument("--stop", action="store_true", help="停掉正在运行的后端服务")
    parser.add_argument("--check", action="store_true", help="打印环境检查结果后退出")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    paths = find_app_paths()
    # **先接管 std 流，再干别的**：后面每一步都可能失败，而失败时要能 print。
    # 顺带把「中文 Windows 的 GBK 控制台装不下 ✓」这个致命问题挡在最前面。
    _attach_std_streams(paths)
    ensure_layout(paths)
    log = _setup_logging(paths)
    log.info("启动器启动：%s", " ".join(argv if argv is not None else sys.argv[1:]))

    if args.check:
        print(wizard.environment_summary(paths))
        return EXIT_OK

    settings = cfg.load(paths)
    port = args.port or (settings.port if settings else cfg.DEFAULT_PORT)

    if args.stop:
        # 诚实报告：停掉了才说「已停止」，没停掉就说清是哪种没停掉
        stopped = svc.stop_by_state(paths, port)
        if stopped:
            print("已停止 KnowledgeFlow 服务")
        elif svc.health_with_retry(port) is not None:
            print(f"端口 {port} 上仍有服务在响应，但没能停止它（可能不是本应用启动的）")
            return EXIT_ERROR
        else:
            print("KnowledgeFlow 服务没有在运行")
        return EXIT_OK

    if args.status:
        health = svc.health_with_retry(port)
        if health is None:
            print(f"未运行（端口 {port}）")
            return EXIT_ERROR
        print(
            f"运行中：http://127.0.0.1:{port}\n"
            f"  版本 {health.get('version')} · 模型 {health.get('llm_provider')}/{health.get('llm_model')}"
            f" · Vault {'已配置' if health.get('vault_configured') else '未配置'}"
        )
        return EXIT_OK

    # ---- 单实例：端口上已经有我们的服务就直接复用 ----
    existing = svc.find_our_service(port, paths)
    if existing is not None:
        log.info("检测到已有服务在端口 %s，复用", port)
        if not args.no_browser:
            svc.open_browser(existing.base_url)
        print(f"KnowledgeFlow 已经在运行：{existing.base_url}")
        return EXIT_OK

    # 端口被别的程序占着 → 说清楚，而不是让它去撞 "Address already in use"
    if svc.port_busy(port) and svc.health_with_retry(port) is None:
        _notify(
            f"端口 {port} 被其它程序占用了。\n\n"
            "可以在命令行里用 --port 换一个端口，或先关掉占用它的程序。"
        )
        return EXIT_ERROR

    # ---- 配置 ----
    if settings is None or not settings.is_complete() or args.setup:
        if args.setup and settings is not None:
            settings = None if wizard.noninteractive() else settings
        new_settings = wizard.run_wizard(paths, settings)
        if new_settings is None:
            if not wizard.noninteractive() and settings is None:
                _notify("配置没有完成，KnowledgeFlow 未启动。\n下次双击图标可以继续配置。")
            return EXIT_CANCELLED
        settings = new_settings
        cfg.save(paths, settings)
        log.info("配置已保存到 %s", paths.settings_file)

    # ---- 启动 ----
    env = cfg.build_env(paths, settings)
    log.info("启动后端：port=%s db=%s", port, paths.database_file)

    # **信号处理器必须在拉子进程之前装好**，不能等后端健康了再装。
    #
    # 以前是装在 _wait_until_exit() 里的 —— 于是「后端正在启动、用户就退出了」
    # 这条路径上，退出信号走**默认动作**把启动器当场打死：finally 不执行、
    # terminate() 不执行，已经 Popen 出来的后端变成**孤儿进程**，还占着端口。
    # 启动期最长 60 秒，这个窗口一点都不窄。
    stop = _Stop()
    _install_signal_handlers(stop)

    handle = svc.start(paths, env, port, log_path=paths.app_log)

    try:
        if not svc.wait_healthy(handle, should_stop=lambda: stop.requested):
            if stop.requested:
                # 用户自己关的，不是故障 —— 说成「启动失败」会让人以为出了问题
                log.info("启动过程中收到退出信号，放弃启动")
                return EXIT_OK
            tail = svc.tail_log(paths.app_log)
            svc.terminate(handle)
            svc.clear_state(paths)
            _notify(
                "KnowledgeFlow 启动失败。\n\n"
                f"日志：{paths.app_log}\n\n"
                "最后几行：\n" + ("\n".join(tail.splitlines()[-8:]) or "（日志为空）")
            )
            return EXIT_ERROR

        base_url = f"http://127.0.0.1:{port}"
        log.info("服务就绪：%s", base_url)
        print(f"KnowledgeFlow is ready. {base_url}")
        if not args.no_browser:
            svc.open_browser(base_url)

        _wait_until_exit(handle, stop, args=args, log_path=paths.app_log)
        return EXIT_OK
    finally:
        if handle.owned:
            log.info("关闭后端（pid=%s）", handle.pid)
            svc.terminate(handle)
            svc.clear_state(paths)
            log.info("已退出")
        wizard.destroy_root()


class _Stop:
    """退出信号标志。

    为什么不直接用 ``signal.signal`` 的默认行为：默认动作是**当场终止进程**，
    而我们需要先把拉起来的后端收掉 —— 收尾逻辑在 ``main()`` 的 ``finally`` 里，
    进程被打死就永远走不到。
    """

    __slots__ = ("requested",)

    def __init__(self) -> None:
        self.requested = False


def _install_signal_handlers(stop: _Stop) -> None:
    """装 ``SIGINT`` / ``SIGTERM`` / ``SIGBREAK`` 处理器。非主线程装不上，安静跳过。

    Windows 上 ``SIGTERM`` 只能由 ``os.kill`` 主动发，操作系统自己不会发它；
    控制台窗口关闭走的是 ``CTRL_CLOSE_EVENT``，而它**只给 5 秒**且不能取消 ——
    所以「可靠地停掉服务」的正式入口是 ``--stop`` 与控制窗口的按钮，不是关窗口。
    """
    candidates = [signal.SIGINT, signal.SIGTERM]
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        candidates.append(sigbreak)

    def _on_signal(_signum: int, _frame: object) -> None:
        stop.requested = True

    for sig in candidates:
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError, AttributeError):
            pass


def _wait_until_exit(
    handle: svc.ServiceHandle, stop: _Stop, *, args: argparse.Namespace, log_path: Path
) -> None:
    """等用户关掉应用，或者后端起不来自己退了。

    优先走**控制窗口**（Windows 上这是用户唯一看得见、点得着的东西）；
    没有图形界面、或显式 ``--no-window`` 时退化成轮询 + 信号。
    """
    if not args.no_window and wizard.gui_available():
        try:
            control.run(handle, stop, log_path=log_path)
            return
        except Exception:  # noqa: BLE001 - 窗口起不来不该让服务跟着挂
            logging.getLogger("knowledgeflow.launcher").exception(
                "控制窗口启动失败，退回后台模式"
            )

    process = handle.process
    while not stop.requested:
        if process is not None and process.poll() is not None:
            return  # 后端自己退了
        time.sleep(0.3)


if __name__ == "__main__":  # pragma: no cover - 由 exe / cmd 调用
    sys.exit(main())
