"""首次启动向导 —— 用 Python 自带的 ``tkinter`` 弹窗，不引入第三方 GUI 依赖。

为什么用 tkinter 而不是 WPF / WinForms / Electron：

* 目标是「装上就能用」，**不是**做一个漂亮的安装器界面
  （先能安装 → 再能启动 → 再能配置 → 再能第一次成功使用 → 最后优化体验）。
* ``tkinter`` 随 Python 一起来，**零额外依赖、零额外体积**；文件夹选择器
  用的是系统原生控件，观感上不比自绘的差。
* 对比 macOS 版用 ``osascript``：那边是为了「不引入 GUI 依赖」才借系统脚本，
  Windows 上 tkinter 本身就是标准库，没必要再绕一层 PowerShell/WinForms。

**绝不把 traceback 给用户**：所有失败都翻译成「XX 未检测到 / 请检查 YY」这种句子，
外加日志路径。
"""

from __future__ import annotations

import functools
import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from kf_app.paths import (
    MIN_WINDOWS_VERSION,
    AppPaths,
    is_windows_x64,
    windows_version,
)
from kf_app.settings import KNOWN_BASE_URLS, PROVIDER_LABELS, Settings, probe_llm, probe_vault

APP_TITLE = "KnowledgeFlow"


# --------------------------------------------------------------------------- #
# tkinter 封装
# --------------------------------------------------------------------------- #
class DialogUnavailable(RuntimeError):
    """没有图形界面 / 用户取消 / tkinter 不可用。"""


_ROOT: Any = None
_GUI_STATE: bool | None = None


def _probe_gui() -> bool:
    """真的能建出一个 Tk 吗。**不能只看 import 成不成功** ——

    ``_tkinter`` 在部分精简运行环境里能 import，但 ``Tk()`` 会抛
    ``TclError: Can't find a usable init.tcl``；无图形会话（Windows Server Core、
    某些 CI）上也会失败。探测必须落到「建得出来」这一步。
    """
    try:
        import tkinter
    except ImportError:
        return False
    try:
        probe = tkinter.Tk()
        probe.withdraw()
        probe.destroy()
    except Exception:  # noqa: BLE001 - tkinter 抛的是 TclError，各版本类型不一
        return False
    return True


def gui_available() -> bool:
    """能不能弹窗。结果缓存 —— 探测本身要建一次 Tk，不该每次问都建。"""
    global _GUI_STATE
    if _GUI_STATE is None:
        _GUI_STATE = _probe_gui()
    return _GUI_STATE


def _root() -> Any:
    """取（并按需创建）常驻的隐藏根窗口。"""
    global _ROOT
    if _ROOT is not None:
        return _ROOT
    if not gui_available():
        raise DialogUnavailable("这台机器上没有可用的图形界面（tkinter 起不来）")
    import tkinter

    root = tkinter.Tk()
    root.withdraw()
    root.title(APP_TITLE)
    _ROOT = root
    return root


def _center(dialog: Any, root: Any) -> None:
    """把对话框摆到屏幕中央。tkinter 默认左上角，看着像野窗口。"""
    dialog.update_idletasks()
    width = dialog.winfo_width()
    height = dialog.winfo_height()
    x = max(0, (dialog.winfo_screenwidth() - width) // 2)
    y = max(0, (dialog.winfo_screenheight() - height) // 3)
    dialog.geometry(f"+{x}+{y}")


def say(text: str, *, title: str = APP_TITLE) -> None:
    """只显示一段话。"""
    from tkinter import messagebox

    messagebox.showinfo(title, text, parent=_root())


def ask(text: str, *, default: str = "", title: str = APP_TITLE, hidden: bool = False) -> str:
    """要一个文本输入。``hidden=True`` 用于 API Key。"""
    from tkinter import simpledialog

    answer = simpledialog.askstring(
        title,
        text,
        initialvalue=default,
        show="*" if hidden else None,
        parent=_root(),
    )
    if answer is None:
        raise DialogUnavailable("用户取消了输入")
    return answer


def pick_folder(prompt: str, *, title: str = APP_TITLE) -> str:
    """系统文件夹选择器 —— 用户看到的是 Windows 原生控件。"""
    from tkinter import filedialog

    chosen = filedialog.askdirectory(title=f"{title} · {prompt}", mustexist=True, parent=_root())
    if not chosen:
        raise DialogUnavailable("用户取消了选择")
    return str(Path(chosen))


def choose(text: str, options: list[str], *, title: str = APP_TITLE, default: int = 1) -> str:
    """单选。用户点「取消」/ 关窗口会抛 ``DialogUnavailable``。

    ``messagebox`` 只支持固定几种按钮组合，做不到「三个自定义选项」，
    所以这里自己搭一个 Toplevel —— 但只用标准控件，不引入任何依赖。
    """
    import tkinter

    root = _root()
    picked: dict[str, str] = {}

    dialog = tkinter.Toplevel(root)
    dialog.title(title)
    dialog.resizable(False, False)
    dialog.attributes("-topmost", True)

    tkinter.Label(
        dialog, text=text, justify="left", wraplength=460, padx=18, pady=14
    ).pack()

    row = tkinter.Frame(dialog, padx=14, pady=(0, 14))
    row.pack()

    def pick(value: str) -> None:
        picked["value"] = value
        dialog.destroy()

    for index, option in enumerate(options, start=1):
        button = tkinter.Button(row, text=option, width=18, command=lambda o=option: pick(o))
        button.pack(side="left", padx=4)
        if index == default:
            button.focus_set()
            dialog.bind("<Return>", lambda _event, o=option: pick(o))

    dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)  # 关窗口 = 取消
    _center(dialog, root)
    dialog.wait_window()

    if not picked.get("value"):
        raise DialogUnavailable("用户取消了操作")
    return picked["value"]


# --------------------------------------------------------------------------- #
# 环境检查
# --------------------------------------------------------------------------- #
def check_environment(paths: AppPaths) -> list[tuple[str, bool, str]]:
    """返回 ``[(检查项, 通过?, 人话说明)]``。**缺东西不等于失败**，只是如实列出。"""
    checks: list[tuple[str, bool, str]] = []

    version = windows_version()
    if version is None:
        checks.append(("Windows 版本", False, "无法识别系统版本（不是 Windows？）"))
    else:
        ok = (version[0], version[1]) >= MIN_WINDOWS_VERSION
        label = f"Windows {version[0]}.{version[1]}（build {version[2]}）"
        checks.append(
            (
                label,
                ok,
                "满足最低要求"
                if ok
                else f"需要 Windows {MIN_WINDOWS_VERSION[0]} 或更高",
            )
        )

    x64 = is_windows_x64()
    checks.append(
        (
            "64 位（x64）",
            x64,
            "已确认" if x64 else "这个安装包只支持 64 位 Windows",
        )
    )

    runtime = paths.runtime_python
    checks.append(
        (
            "内置运行环境",
            runtime.is_file(),
            "已就绪" if runtime.is_file() else f"缺少 {runtime} —— 这个包可能不完整，请重新解压",
        )
    )

    backend = paths.backend / "scripts" / "serve.py"
    checks.append(
        ("后端程序", backend.is_file(), "已就绪" if backend.is_file() else "包内缺少后端文件，请重新解压")
    )

    checks.append(("SQLite", True, "Python 内置，无需额外安装"))

    # OCR：需要系统里的 tesseract（可选能力）
    tesseract = shutil.which("tesseract")
    checks.append(
        (
            "OCR（Tesseract）",
            tesseract is not None,
            "已检测到" if tesseract else "未安装 —— 图片识别不可用，其它功能不受影响",
        )
    )

    # 音视频：本项目用 PyAV 解码（随包内置），**不需要系统装 ffmpeg**
    checks.append(("音视频解码", True, "已内置（PyAV），无需系统安装 ffmpeg"))

    return checks


# --------------------------------------------------------------------------- #
# 输出编码：GBK 控制台装不下 ✓ / ✗
# --------------------------------------------------------------------------- #
#: 好看的标记。UTF-8 控制台、日志文件、tkinter 对话框都能显示
_MARKS_UNICODE = ("✓", "✗")
#: 降级标记。GBK / cp1252 这类码页里没有 U+2713，只能退回纯 ASCII
_MARKS_ASCII = ("[OK]", "[--]")


def console_marks(encoding: str | None = None) -> tuple[str, str]:
    """挑一组**当前输出编码装得下**的标记，返回 ``(通过, 未通过)``。

    ``✓``（U+2713）不在 GBK 码表里，而中文 Windows 的 stdout 就是 GBK ——
    于是 ``print("✓ …")`` 抛 ``UnicodeEncodeError``，把 ``--check`` 这条
    **纯诊断**命令变成一段 traceback：退出码 1、stdout 一个字节都没有，
    用户拿到的唯一线索是 ``'gbk' codec can't encode character '\\u2713'``。
    诊断信息宁可显示成 ``[OK]``，也不能以 traceback 收场。

    ``encoding`` 留空时读 ``sys.stdout``。**注意冻结后的 exe 里
    ``PYTHONIOENCODING`` 是无效的**（PyInstaller 会按 ANSI 码页重开 std 句柄），
    所以这里只能看 ``sys.stdout.encoding``，不能假设它是 UTF-8。
    """
    if encoding is None:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        "".join(_MARKS_UNICODE).encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return _MARKS_ASCII
    return _MARKS_UNICODE


def environment_summary(paths: AppPaths) -> str:
    """把检查结果拼成一段人类可读的文字，用于向导展示。

    标记按**输出编码**挑（见 ``console_marks``）：这句话既会被 ``--check``
    打到控制台，也会被向导弹窗用到，而控制台的编码不一定是 UTF-8。
    """
    ok_mark, fail_mark = console_marks()
    lines = [
        f"{ok_mark if ok else fail_mark} {name} —— {detail}"
        for name, ok, detail in check_environment(paths)
    ]
    return "\n".join(lines)


def blocking_problems(paths: AppPaths) -> list[str]:
    """真正会**阻止启动**的问题（可选能力缺失不算）。

    白名单式判断：只有「内置运行环境 / 后端程序」缺失、或系统版本过低才阻止启动，
    其余（OCR、tesseract、抖音登录态…）一律降级运行。
    """
    problems: list[str] = []
    if not paths.runtime_python.is_file():
        problems.append("内置运行环境缺失")
    if not (paths.backend / "scripts" / "serve.py").is_file():
        problems.append("后端程序缺失")
    version = windows_version()
    if version is not None and (version[0], version[1]) < MIN_WINDOWS_VERSION:
        problems.append(f"Windows 版本过低（需要 {MIN_WINDOWS_VERSION[0]}+）")
    return problems


# --------------------------------------------------------------------------- #
# 向导主流程
# --------------------------------------------------------------------------- #
#: 无人值守模式：从这些环境变量读配置（用于端到端测试与批量安装）。
NONINTERACTIVE_FLAG = "KF_SETUP_NONINTERACTIVE"
ENV_VAULT = "KF_VAULT_PATH"
ENV_API_KEY = "KF_LLM_API_KEY"
ENV_PROVIDER = "KF_LLM_PROVIDER"
ENV_MODEL = "KF_LLM_MODEL"
ENV_BASE_URL = "KF_LLM_BASE_URL"


def noninteractive() -> bool:
    return os.environ.get(NONINTERACTIVE_FLAG, "") == "1"


def run_wizard(paths: AppPaths, current: Settings | None = None) -> Settings | None:
    """跑一遍向导。返回 ``None`` 表示用户放弃了。

    ``KF_SETUP_NONINTERACTIVE=1`` 时完全不弹窗，只读环境变量 ——
    这条路径是给测试和「我自己装机」用的，也是唯一能自动化验证的路径。
    """
    settings = current or Settings()

    if noninteractive() or not gui_available():
        return _wizard_from_env(paths, settings)

    try:
        return _wizard_interactive(paths, settings)
    except DialogUnavailable:
        # 图形界面中途不可用（用户取消、Tk 崩了）：若环境变量齐全就退回非交互，
        # 否则如实返回「没配成」。
        if any(os.environ.get(k) for k in (ENV_VAULT, ENV_API_KEY)):
            return _wizard_from_env(paths, settings)
        return None


def _wizard_from_env(paths: AppPaths, settings: Settings) -> Settings | None:
    vault = os.environ.get(ENV_VAULT, "").strip()
    api_key = os.environ.get(ENV_API_KEY, "").strip()
    if not vault or not api_key:
        return None
    settings = replace(
        settings,
        obsidian_vault_path=vault,
        llm_api_key=api_key,
        llm_provider=os.environ.get(ENV_PROVIDER, settings.llm_provider),
        llm_model=os.environ.get(ENV_MODEL, settings.llm_model),
        llm_base_url=os.environ.get(ENV_BASE_URL, settings.llm_base_url),
    )
    ok, message = probe_vault(settings.obsidian_vault_path)
    if not ok:
        raise DialogUnavailable(f"Vault 不可写：{message}")
    return settings


def _wizard_interactive(paths: AppPaths, settings: Settings) -> Settings | None:
    say(
        "欢迎使用 KnowledgeFlow\n\n"
        "把互联网上的碎片信息，变成有出处、可追踪的个人知识资产，\n"
        "并写进你的 Obsidian。\n\n"
        "接下来三步：选 Vault → 填 AI Key → 启动。"
    )

    # ---- Step 0：环境检查 ----
    problems = blocking_problems(paths)
    if problems:
        say("无法继续：\n\n" + "\n".join(f"· {p}" for p in problems) + "\n\n请重新解压安装包。")
        return None

    # ---- Step 1：Obsidian Vault ----
    while True:
        vault = settings.obsidian_vault_path
        if vault:
            keep = choose(f"当前 Vault：\n{vault}\n\n要改吗？", ["就用这个", "重新选择"], default=1)
            if keep != "重新选择":
                ok, message = probe_vault(vault)
                if ok:
                    break
                say(f"这个 Vault 有问题：\n{message}\n\n请重新选择。")
        vault = pick_folder("选择你的 Obsidian Vault 文件夹")
        ok, message = probe_vault(vault)
        if ok:
            settings = replace(settings, obsidian_vault_path=vault)
            break
        say(f"这个文件夹不能用：\n{message}")

    # ---- Step 2：AI 配置 ----
    while True:
        provider_label = choose(
            "用哪个 AI 服务？\n\nDeepSeek 便宜、国内直连；\n"
            "OpenAI 或其他兼容端点请选第二项（可以自定义 Base URL）。",
            ["DeepSeek", "OpenAI / 自定义兼容端点"],
            default=1,
        )
        provider = "deepseek" if provider_label == "DeepSeek" else "openai"

        default_model = (
            settings.llm_model
            if settings.llm_provider == provider
            else ("deepseek-flash" if provider == "deepseek" else "")
        )
        model = ask("模型名（例如 deepseek-flash）：", default=default_model)
        if not model.strip():
            continue

        base_url = KNOWN_BASE_URLS[provider]
        if provider == "openai":
            base_url = ask(
                "Base URL（保持默认即官方 OpenAI，用第三方兼容服务请替换）：",
                default=settings.llm_base_url or KNOWN_BASE_URLS["openai"],
            )

        api_key = ask("API Key（输入时不显示）：", default="", hidden=True)
        if not api_key.strip():
            continue

        testing = replace(
            settings,
            llm_provider=provider,
            llm_model=model.strip(),
            llm_base_url=base_url.strip(),
            llm_api_key=api_key.strip(),
        )
        ok, message = probe_llm(testing.llm_base_url, testing.llm_api_key, testing.llm_model)
        if ok:
            say("✓ AI 连接成功。")
            settings = testing
            break
        retry = choose(
            f"✗ AI 连接失败\n\n{message}\n\n请检查 API Key / Base URL / 网络 / 模型名。",
            ["重新填写", "仍然保存并继续"],
            default=1,
        )
        if retry == "仍然保存并继续":
            settings = testing
            break

    say(
        "配置完成。\n\n"
        f"Vault：{settings.obsidian_vault_path}\n"
        f"AI：{PROVIDER_LABELS.get(settings.llm_provider, settings.llm_provider)} / {settings.llm_model}\n\n"
        "点「确定」后会自动启动服务并打开界面。"
    )
    return settings


def destroy_root() -> None:
    """收掉常驻的隐藏根窗口。退出前调用，免得进程挂着一个看不见的 Tk。"""
    global _ROOT
    if _ROOT is None:
        return
    try:
        _ROOT.destroy()
    except Exception:  # noqa: BLE001 - 关窗口失败不该影响退出
        pass
    _ROOT = None


__all__ = [
    "APP_TITLE",
    "DialogUnavailable",
    "ENV_API_KEY",
    "ENV_BASE_URL",
    "ENV_MODEL",
    "ENV_PROVIDER",
    "ENV_VAULT",
    "NONINTERACTIVE_FLAG",
    "ask",
    "blocking_problems",
    "check_environment",
    "choose",
    "console_marks",
    "destroy_root",
    "environment_summary",
    "gui_available",
    "noninteractive",
    "pick_folder",
    "run_wizard",
    "say",
]
