"""启动器入口。

``KnowledgeFlow.app`` 的 ``Contents/MacOS/KnowledgeFlow`` 最终就是跑这个文件。

设计原则（定稿第十一、十二、十三节）：

* 双击 .app 时：**已在运行就复用、没在运行就启动** —— 永远不出现第二个实例
* 默认只监听 ``127.0.0.1``
* 服务就绪后自动打开浏览器
* 退出时把后端收干净；被 ``kill -9`` 过也能再次启动
* 所有失败都是**一句人话 + 日志路径**，不是 traceback
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from pathlib import Path

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
        handler: logging.Handler = logging.FileHandler(paths.logs / "launcher.log", encoding="utf-8")
    except OSError:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def _notify(text: str) -> None:
    """把消息告诉用户。有图形界面就弹窗，没有就打到 stderr。"""
    print(text, file=sys.stderr)
    if wizard.noninteractive() or not sys.platform == "darwin":
        return
    try:
        wizard.say(text)
    except wizard.DialogUnavailable:
        pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="KnowledgeFlow",
        description="KnowledgeFlow 桌面启动器（v0.4）",
    )
    parser.add_argument("--port", type=int, default=None, help="覆盖端口（默认 8000）")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--setup", action="store_true", help="强制重跑首次配置向导")
    parser.add_argument("--status", action="store_true", help="只报告服务状态")
    parser.add_argument("--stop", action="store_true", help="停掉正在运行的后端服务")
    parser.add_argument("--check", action="store_true", help="打印环境检查结果后退出")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    paths = find_app_paths()
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
        elif svc.health(port) is not None:
            print(f"端口 {port} 上仍有服务在响应，但没能停止它（可能不是本应用启动的）")
            return EXIT_ERROR
        else:
            print("KnowledgeFlow 服务没有在运行")
        return EXIT_OK

    if args.status:
        health = svc.health(port)
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
    if svc.port_busy(port) and svc.health(port) is None:
        _notify(
            f"端口 {port} 被其它程序占用了。\n\n"
            "可以在终端里用 --port 换一个端口，或先关掉占用它的程序。"
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
    handle = svc.start(paths, env, port, log_path=paths.app_log)

    try:
        if not svc.wait_healthy(handle):
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

        _wait_until_exit(handle)
        return EXIT_OK
    finally:
        if handle.owned:
            log.info("关闭后端（pid=%s）", handle.pid)
            svc.terminate(handle)
            svc.clear_state(paths)
            log.info("已退出")


def _wait_until_exit(handle: svc.ServiceHandle) -> None:
    """等用户关掉应用，或者后端起不来自己退了。

    同时处理 SIGTERM / SIGINT：Dock 图标右键「退出」发的是 SIGTERM，
    命令行 Ctrl-C 是 SIGINT —— 两条路都必须把子进程收干净。
    """
    stop = {"flag": False}

    def _on_signal(signum: int, _frame: object) -> None:
        stop["flag"] = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _on_signal)
        except ValueError:  # 非主线程
            pass

    process = handle.process
    while not stop["flag"]:
        if process is not None and process.poll() is not None:
            return  # 后端自己退了
        time.sleep(0.3)


if __name__ == "__main__":  # pragma: no cover - 由 .app 调用
    sys.exit(main())
