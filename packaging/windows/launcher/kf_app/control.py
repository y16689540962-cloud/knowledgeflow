"""控制窗口：让用户**看得见**服务在跑，并且有地方点「停止」。

macOS 版不需要这个 —— 它在 Dock 里有图标（``Info.plist`` 的
``LSUIElement=False``），用户右键「退出」就是退出，退出信号（SIGTERM）
能到达启动器，后端被收干净。

Windows 上双击一个无控制台的 exe **不会留下任何可见的东西**：进程在后台跑，
用户既看不到它、也关不掉它，只能去任务管理器里翻。这不叫「装上就能用」。
所以补一个最小的控制窗口：状态 + 「打开界面」+「停止并退出」。
只用标准库的 tkinter，**不引入托盘图标依赖**（pystray 之类会带来一个
只在 Windows 上才有意义的第三方包，与「零额外依赖」的取向冲突）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

APP_TITLE: Final[str] = "KnowledgeFlow"

#: 轮询后端进程的间隔（毫秒）。600ms 够快，又不至于空转。
POLL_INTERVAL_MS: Final[int] = 600

_MUTED: Final[str] = "#555555"
_WARN: Final[str] = "#a33a3a"


def run(
    handle: Any,
    stop: Any,
    *,
    log_path: Path,
    poll_interval_ms: int = POLL_INTERVAL_MS,
) -> None:
    """显示控制窗口并阻塞到用户关掉它。

    ``handle`` 是 ``service.ServiceHandle``，``stop`` 是 ``main._Stop``。
    两个都按鸭子类型用（只取 ``.process`` / ``.base_url`` 与 ``.requested``），
    这样本模块不必 import ``main``，避免循环依赖。
    """
    import tkinter

    from kf_app import service as svc

    root = tkinter.Tk()
    root.title(f"{APP_TITLE} · 正在运行")
    root.resizable(False, False)

    url = handle.base_url
    status = tkinter.StringVar(value=f"服务运行中：{url}")
    hint = tkinter.StringVar(value="关闭这个窗口，或点「停止并退出」，服务就会被停掉。")

    frame = tkinter.Frame(root, padx=20, pady=16)
    frame.pack()

    tkinter.Label(frame, text=APP_TITLE, font=("Segoe UI", 13, "bold")).pack(anchor="w")
    status_label = tkinter.Label(frame, textvariable=status, font=("Segoe UI", 10))
    status_label.pack(anchor="w", pady=(8, 0))
    tkinter.Label(
        frame, textvariable=hint, fg=_MUTED, justify="left", wraplength=380
    ).pack(anchor="w", pady=(4, 14))

    row = tkinter.Frame(frame)
    row.pack(anchor="w")
    tkinter.Button(row, text="打开界面", width=14, command=lambda: svc.open_browser(url)).pack(
        side="left", padx=(0, 8)
    )
    tkinter.Button(row, text="停止并退出", width=14, command=lambda: _shutdown(root, stop)).pack(
        side="left"
    )

    root.protocol("WM_DELETE_WINDOW", lambda: _shutdown(root, stop))

    def poll() -> None:
        process = handle.process
        if process is not None and process.poll() is not None:
            # 后端自己退了 —— 说清楚，并给出日志位置，别让用户对着一个死窗口发呆。
            status.set("后端已经退出。")
            status_label.configure(fg=_WARN)
            tail = svc.tail_log(log_path, lines=4)
            hint.set(
                f"最后几行日志：\n{tail or '（日志为空）'}\n\n日志文件：{log_path}"
            )
            tkinter.Button(row, text="关闭", width=14, command=root.destroy).pack(
                side="left", padx=(8, 0)
            )
            return
        root.after(poll_interval_ms, poll)

    root.after(poll_interval_ms, poll)
    try:
        root.mainloop()
    except Exception:  # noqa: BLE001 - 窗口挂掉不该让收尾逻辑（finally）不执行
        stop.requested = True
        raise


def _shutdown(root: Any, stop: Any) -> None:
    """用户要求退出：置标志位，然后关窗口。

    真正的收尾（``terminate`` 后端、清状态文件）在 ``main.main()`` 的 ``finally``
    里 —— 这里只负责把控制流还给它。**不在这里直接杀后端**：那条路径会让
    「收尾」出现两份实现，早晚不一致。
    """
    stop.requested = True
    root.destroy()


__all__ = ["APP_TITLE", "POLL_INTERVAL_MS", "run"]
