"""首次启动向导 —— 用 macOS 原生的 ``osascript`` 弹窗，不引入 GUI 依赖。

为什么用 osascript 而不是做个 Cocoa/Tauri 界面：

* v0.4 的目标是「装上就能用」，**不是**做一个漂亮的安装器界面（定稿第二十五节：
  先能安装 → 再能启动 → 再能配置 → 再能第一次成功使用 → 最后优化体验）。
* 本机只有 CommandLineTools、没有 Xcode，写个真 Cocoa 应用会引入一堆构建依赖。
* ``choose folder`` / ``display dialog`` 就是系统原生控件，用户看到的是 macOS
  自己的文件夹选择器 —— 这比自绘的更像「正规软件」。

**绝不把 traceback 给用户**（定稿第八节）：所有失败都翻译成
「XX 未检测到 / 请检查 YY」这种句子，外加日志路径。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

from kf_app.paths import AppPaths, is_macos_apple_silicon, macos_version, MIN_MACOS_VERSION
from kf_app.settings import KNOWN_BASE_URLS, PROVIDER_LABELS, Settings, probe_llm, probe_vault

APP_TITLE = "KnowledgeFlow"
#: osascript 弹窗超时：用户走开时会挂着，给个上限免得永远等。
OSASCRIPT_TIMEOUT_SECONDS = 600.0


# --------------------------------------------------------------------------- #
# osascript 封装
# --------------------------------------------------------------------------- #
class DialogUnavailable(RuntimeError):
    """没有图形界面 / 用户取消 / osascript 不可用。"""


def _run_osascript(script: str, *args: str) -> str:
    if shutil.which("osascript") is None:
        raise DialogUnavailable("这台机器上找不到 osascript")
    result = subprocess.run(
        ["osascript", "-e", script, *args],
        capture_output=True,
        text=True,
        timeout=OSASCRIPT_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        # osascript 在用户点「取消」时返回 -128
        raise DialogUnavailable(result.stderr.strip() or "用户取消了操作")
    return result.stdout.strip()


def say(text: str, *, title: str = APP_TITLE) -> None:
    """只显示一段话。"""
    _run_osascript(f'display dialog {_q(text)} with title {_q(title)} buttons {{"继续"}} default button 1')


def ask(text: str, *, default: str = "", title: str = APP_TITLE, hidden: bool = False) -> str:
    """要一个文本输入。``hidden=True`` 用于 API Key。"""
    hidden_clause = " with hidden answer" if hidden else ""
    script = (
        f'display dialog {_q(text)} with title {_q(title)} '
        f'default answer {_q(default)}{hidden_clause} buttons {{"取消", "确定"}} default button 2'
    )
    out = _run_osascript(script)
    # 输出形如 "button returned:确定, text returned:xxx"
    for chunk in out.split(", "):
        if chunk.startswith("text returned:"):
            return chunk[len("text returned:"):]
    return ""


def pick_folder(prompt: str, *, title: str = APP_TITLE) -> str:
    """系统文件夹选择器 —— 用户看到的是 macOS 原生控件。"""
    out = _run_osascript(
        f'POSIX path of (choose folder with prompt {_q(prompt)} with title {_q(title)})'
    )
    return out.rstrip("/")


def choose(text: str, options: list[str], *, title: str = APP_TITLE, default: int = 1) -> str:
    """单选。用户点「取消」会抛 ``DialogUnavailable``。"""
    buttons = ", ".join(_q(o) for o in options)
    script = (
        f'display dialog {_q(text)} with title {_q(title)} '
        f'buttons {{{buttons}}} default button {default}'
    )
    out = _run_osascript(script)
    return out.split(":")[-1]


def _q(value: str) -> str:
    """把字符串安全地塞进 AppleScript 字符串字面量。"""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


# --------------------------------------------------------------------------- #
# 环境检查（定稿第八节的 Step 1）
# --------------------------------------------------------------------------- #
def check_environment(paths: AppPaths) -> list[tuple[str, bool, str]]:
    """返回 ``[(检查项, 通过?, 人话说明)]``。**缺东西不等于失败**，只是如实列出。"""
    checks: list[tuple[str, bool, str]] = []

    silicon = is_macos_apple_silicon()
    checks.append(
        (
            "Apple Silicon",
            silicon,
            "已确认" if silicon else "这台机器不是 Apple Silicon —— v0.4 的安装包只支持 arm64",
        )
    )

    version = macos_version()
    if version is None:
        checks.append(("macOS 版本", False, "无法识别系统版本"))
    else:
        ok = version >= MIN_MACOS_VERSION
        checks.append(
            (
                f"macOS {version[0]}.{version[1]}",
                ok,
                "满足最低要求" if ok else f"需要 macOS {MIN_MACOS_VERSION[0]}.{MIN_MACOS_VERSION[1]} 或更高",
            )
        )

    runtime = paths.runtime_python
    checks.append(
        (
            "内置运行环境",
            runtime.is_file(),
            "已就绪" if runtime.is_file() else f"缺少 {runtime} —— 这个安装包可能不完整，请重新下载",
        )
    )

    backend = paths.backend / "scripts" / "serve.py"
    checks.append(
        ("后端程序", backend.is_file(), "已就绪" if backend.is_file() else "安装包内缺少后端文件，请重新下载")
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


def environment_summary(paths: AppPaths) -> str:
    """把检查结果拼成一段人类可读的文字，用于向导展示。"""
    lines = []
    for name, ok, detail in check_environment(paths):
        lines.append(f"{'✓' if ok else '✗'} {name} —— {detail}")
    return "\n".join(lines)


def blocking_problems(paths: AppPaths) -> list[str]:
    """真正会**阻止启动**的问题（可选能力缺失不算）。

    这里刻意用白名单式判断：只有「内置运行环境 / 后端程序」缺失才阻止启动，
    其余（OCR、tesseract、抖音登录态…）一律降级运行。
    """
    problems: list[str] = []
    if not paths.runtime_python.is_file():
        problems.append("内置运行环境缺失")
    if not (paths.backend / "scripts" / "serve.py").is_file():
        problems.append("后端程序缺失")
    runtime_version = macos_version()
    if runtime_version is not None and runtime_version < MIN_MACOS_VERSION:
        problems.append(f"macOS 版本过低（需要 {MIN_MACOS_VERSION[0]}.{MIN_MACOS_VERSION[1]}+）")
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

    if noninteractive():
        return _wizard_from_env(paths, settings)

    try:
        return _wizard_interactive(paths, settings)
    except DialogUnavailable:
        # 没有图形界面（例如 ssh / CI）：退回环境变量，并把说明写进日志
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
        say("无法继续：\n\n" + "\n".join(f"· {p}" for p in problems) + "\n\n请重新下载安装包。")
        return None

    # ---- Step 1：Obsidian Vault ----
    while True:
        vault = settings.obsidian_vault_path
        if vault:
            keep = choose(
                f"当前 Vault：\n{vault}\n\n要改吗？", ["就用这个", "重新选择"], default=1
            )
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

        default_model = settings.llm_model if settings.llm_provider == provider else (
            "deepseek-flash" if provider == "deepseek" else ""
        )
        model = ask(f"模型名（例如 deepseek-flash）：", default=default_model)
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
        "点「继续」后会自动启动服务并打开界面。"
    )
    return settings
