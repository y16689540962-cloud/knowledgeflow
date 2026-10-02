"""桌面配置层：``settings.json`` ↔ 后端环境变量。

**关键约束（与 macOS 版一致）**：不发明第二套配置机制。
后端已经用环境变量 + pydantic-settings 管配置，这里只是：

    settings.json（桌面可写、ACL 收紧到仅当前用户、人可读）
        ↓ 映射
    后端现有的那组环境变量
        ↓
    原样启动后端

所以后端一行都不用改，也不会出现「桌面配置文件」和 ``.env`` 两套真相。

**三个 Windows 专属的坑，都在这里修掉**：

1. ``DATABASE_URL`` 的斜杠方向。后端默认是相对 cwd 的
   ``sqlite:///./data/...``，而双击启动的进程 cwd 是安装目录（甚至 ``C:\\Windows\\System32``）。
   这里一律喂**绝对路径**；并且用 ``Path.as_posix()`` 把 ``\\`` 换成 ``/`` ——
   SQLAlchemy 的 SQLite URL 里反斜杠是转义字符，``sqlite:///C:\\Users\\...``
   在不同版本上解析结果不一致，``sqlite:///C:/Users/...`` 才是稳定写法。
2. ``0600`` 在 Windows 上不存在。``os.chmod`` 只能切「只读」位，碰不到 ACL ——
   于是「只有我能读这个 API Key」这个性质**根本没被保护**。这里改用
   ``icacls`` 断掉继承、只留当前用户，这才是 Windows 上的等价物。
3. 中文环境的控制台默认码页是 GBK，后端一 print 中文就 ``UnicodeEncodeError``。
   用 ``PYTHONUTF8=1`` 把子进程钉在 UTF-8 上。

``probe_llm`` / ``probe_vault`` 与 macOS 版逐行同源。两份是有意分开的：
``packaging/macos`` 与 ``packaging/windows`` 各自独立可发布，互不 import ——
少一层耦合，代价是这两段纯函数要同步维护。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final

from kf_app.paths import AppPaths

_LOG = logging.getLogger("knowledgeflow.launcher.settings")

#: 配置 schema 版本：以后字段变了靠它做迁移，而不是猜。
SETTINGS_SCHEMA_VERSION: Final[int] = 1

DEFAULT_PORT: Final[int] = 8000

#: 与后端 ``app/providers/openai_compatible.py`` 的 DEFAULT_BASE_URLS 保持一致。
#: 这里**只用来给向导预填**，真正的默认值仍然后端说了算。
KNOWN_BASE_URLS: Final[dict[str, str]] = {
    "deepseek": "https://api.deepseek.com/v1",
    "openai": "https://api.openai.com/v1",
}
PROVIDER_LABELS: Final[dict[str, str]] = {
    "deepseek": "DeepSeek",
    "openai": "OpenAI 或任何 OpenAI 兼容端点",
}

#: ``icacls`` 的超时（秒）。它是系统自带工具，正常几十毫秒就返回。
ICACLS_TIMEOUT_SECONDS: Final[float] = 15.0


@dataclass
class Settings:
    """桌面端的配置。写入 ``config\\settings.json``。"""

    obsidian_vault_path: str = ""
    llm_provider: str = "deepseek"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = "deepseek-flash"
    port: int = DEFAULT_PORT
    #: 可选：抖音采集用的登录态（只填使用者自己的）
    douyin_cookie: str = ""
    schema_version: int = SETTINGS_SCHEMA_VERSION
    created_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    # ---------- 校验 ----------
    def missing(self) -> list[str]:
        """还缺哪些必填项（向导据此决定问什么）。返回人话，不是字段名。"""
        gaps: list[str] = []
        if not self.obsidian_vault_path:
            gaps.append("Obsidian Vault")
        if not self.llm_api_key:
            gaps.append("AI API Key")
        if not self.llm_model:
            gaps.append("AI 模型名")
        return gaps

    def is_complete(self) -> bool:
        return not self.missing()


# --------------------------------------------------------------------------- #
# 读写
# --------------------------------------------------------------------------- #
def load(paths: AppPaths) -> Settings | None:
    """读 settings.json。文件不存在 → ``None``（= 首次启动）。损坏 → 也返回 None。

    损坏时不崩：向导会重新问一遍，比弹 traceback 强。
    """
    file = paths.settings_file
    if not file.is_file():
        return None
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    known = {f for f in Settings.__dataclass_fields__ if f != "extra"}
    kwargs = {k: v for k, v in raw.items() if k in known}
    extra = {k: v for k, v in raw.items() if k not in known}
    # 老 schema 或缺字段：用默认值补齐，不炸
    try:
        settings = Settings(**kwargs)
    except TypeError:
        return None
    settings.extra = extra
    return settings


def save(paths: AppPaths, settings: Settings) -> Path:
    """原子写 + 收紧 ACL。API Key 在里面，必须是只有本人可读。"""
    import os as _os
    import tempfile
    from datetime import datetime, timezone

    paths.config.mkdir(parents=True, exist_ok=True)
    if not settings.created_at:
        settings.created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    payload = asdict(settings)
    extra = payload.pop("extra", {}) or {}
    payload.update(extra)
    payload.pop("extra", None)

    file = paths.settings_file
    fd, tmp = tempfile.mkstemp(dir=str(paths.config), prefix=".settings-", suffix=".json")
    try:
        with _os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            _os.fsync(handle.fileno())
        _os.replace(tmp, file)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise

    # **收紧权限必须在 replace 之后**：先设 ACL 再改名，等于给一个临时文件上锁，
    # 而最终文件拿到的仍是父目录的继承 ACE。
    #
    # 收紧失败**不阻断保存**：配置存不下去比权限宽松更糟（用户会卡在向导里出不来）。
    # 但必须留下痕迹 —— 文件里有 API Key，静默放过等于假装保护过了。
    if not restrict_to_current_user(file):
        _LOG.warning(
            "无法收紧 %s 的访问控制列表（ACL）：文件内含 API Key，请手动检查权限",
            file,
        )
    return file


def current_user_principal() -> str:
    """``DOMAIN\\user`` 形式的当前用户主体名；取不到返回空串。"""
    user = (os.environ.get("USERNAME") or "").strip()
    if not user:
        return ""
    domain = (os.environ.get("USERDOMAIN") or "").strip()
    return f"{domain}\\{user}" if domain else user


def restrict_to_current_user(path: Path) -> bool:
    """把文件的 ACL 收紧到「仅当前用户」，等价于 POSIX 的 ``0600``。

    ``/inheritance:r`` 断掉从父目录继承来的 ACE（否则 ``Users`` 组照样能读），
    ``/grant:r`` 只留当前用户的完全控制。返回**是否成功**，不抛异常。

    为什么不用 ``os.chmod(0o600)``：在 Windows 上它只切「只读」位，
    改不动 DACL —— 文件仍然是「本机所有用户可读」，而里面装着 API Key。
    这个差别很容易被「代码里有 chmod 就以为安全了」骗过去。

    为什么**不加** ``text=True``：``icacls`` 在中文 Windows 上按 GBK 输出，
    而 ``text=True`` 会用 UTF-8 解码，于是 ``subprocess`` 的读取线程直接抛
    ``UnicodeDecodeError``（实测：进程没崩，但 stderr 里多一段吓人的 traceback，
    而且拿不到输出）。这里只要返回码，收字节就够了 —— 平台码页是什么都不影响。
    """
    principal = current_user_principal()
    if not principal:
        return False
    try:
        result = subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"{principal}:(F)",
            ],
            capture_output=True,
            timeout=ICACLS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


# --------------------------------------------------------------------------- #
# 映射到后端环境变量
# --------------------------------------------------------------------------- #
def build_env(paths: AppPaths, settings: Settings) -> dict[str, str]:
    """把桌面配置翻译成后端认识的环境变量。

    **绝对路径是这里的重点**：数据库、下载目录、模型缓存全部显式给出绝对值，
    这样无论从哪启动、cwd 是什么，行为都一致。

    另一个重点是**把会污染内嵌解释器的 Python 环境变量清掉**（见下面的注释）——
    后端跑的是包里那份 runtime，环境却是从用户 shell 继承来的，两者不该混。
    """
    env = dict(os.environ)

    # 用户 shell 里只要设了 PYTHONPATH，它就会插进**内嵌 runtime** 的 sys.path，
    # 而且路径上任何叫 sitecustomize.py 的文件都会被自动执行。这不是假想问题：
    # 本项目的构建机上就带着一个 PYTHONPATH，它一度让 venv 创建失败。
    # PYTHONHOME 更狠 —— 它会把内嵌 runtime 的 prefix 整个改掉，直接找不到标准库。
    # 用户级 site-packages 同理：用户 profile 里恰好装过的包会**遮蔽**包里那份，
    # 于是「我这儿好好的」变成用户机器上的一堆 ImportError。
    for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONEXECUTABLE"):
        env.pop(name, None)
    env["PYTHONNOUSERSITE"] = "1"

    # LLM：provider 只认 openai / deepseek（后端是 Literal），
    # 第三方兼容端点的做法是选 openai + 自定义 BASE_URL —— 这是既有能力，不改后端。
    provider = settings.llm_provider if settings.llm_provider in KNOWN_BASE_URLS else "openai"
    env["LLM_PROVIDER"] = provider
    env["LLM_MODEL"] = settings.llm_model
    if provider == "openai":
        env["OPENAI_API_KEY"] = settings.llm_api_key
        env["OPENAI_BASE_URL"] = settings.llm_base_url or KNOWN_BASE_URLS["openai"]
    else:
        env["DEEPSEEK_API_KEY"] = settings.llm_api_key
        env["DEEPSEEK_BASE_URL"] = settings.llm_base_url or KNOWN_BASE_URLS["deepseek"]

    # 存储：绝对路径 + 正斜杠（见模块 docstring 第 1 条）
    env["DATABASE_URL"] = f"sqlite:///{paths.database_file.as_posix()}"
    env["DOWNLOAD_DIR"] = str(paths.media_dir)

    env["OBSIDIAN_VAULT_PATH"] = settings.obsidian_vault_path
    if settings.douyin_cookie:
        env["DOUYIN_COOKIE"] = settings.douyin_cookie

    # 模型缓存也放进应用数据目录：不污染用户 profile，卸载时能一起清掉
    env["HF_HOME"] = str(paths.cache / "huggingface")

    # 中文 Windows 的默认码页是 GBK：不钉住的话，后端一 print 中文就
    # UnicodeEncodeError（而且是子进程崩、日志里只留一行 traceback，很难查）。
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    # 让后端把日志写细一点（文件里有，界面上看不到也不吵）
    env.setdefault("LOG_LEVEL", "INFO")
    return env


# --------------------------------------------------------------------------- #
# 连接测试
# --------------------------------------------------------------------------- #
def probe_llm(base_url: str, api_key: str, model: str, *, timeout: float = 20.0) -> tuple[bool, str]:
    """向 ``{base}/chat/completions`` 发一次最小请求。

    返回 ``(成功?, 人话说明)``。**不抛异常** —— 向导只负责把这句话显示出来。

    刻意发一次真实 completion（而不是 ``/models``）：有些兼容端点不实现
    ``/models``，却完全能聊天；而我们要验证的正是「能不能聊天」。
    """
    url = base_url.rstrip("/") + "/chat/completions"
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status == 200:
                return True, "连接成功"
            return False, f"服务返回 HTTP {response.status}"
    except urllib.error.HTTPError as exc:  # 4xx / 5xx
        if exc.code == 401:
            return False, "API Key 无效（服务返回 401）"
        if exc.code == 404:
            return False, "端点或模型不存在（服务返回 404）—— 检查 Base URL 与模型名"
        if exc.code == 429:
            return False, "被限流或余额不足（服务返回 429）"
        return False, f"服务返回 HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return False, f"连不上这个地址：{exc.reason}"
    except TimeoutError:
        return False, "请求超时（网络或代理问题）"
    except OSError as exc:
        return False, f"网络错误：{exc}"


def probe_vault(path: str) -> tuple[bool, str]:
    """验证 Vault 能真写入 —— 建一个测试文件再删掉。

    **只判定、不清理别人的东西**：创建的文件名带固定前缀，失败也只在
    明确是「自己刚建的那个」时才删。
    """
    import uuid

    target = Path(path).expanduser()
    if not target.exists():
        return False, "这个文件夹不存在"
    if not target.is_dir():
        return False, "这是一个文件，不是文件夹"
    probe = target / f".knowledgeflow-write-test-{uuid.uuid4().hex[:8]}"
    try:
        probe.write_text("ok", encoding="utf-8")
    except OSError as exc:
        return False, f"写不进去（{exc.strerror or exc}）—— 检查文件夹权限"
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
    return True, "可以写入"


__all__ = [
    "DEFAULT_PORT",
    "KNOWN_BASE_URLS",
    "PROVIDER_LABELS",
    "SETTINGS_SCHEMA_VERSION",
    "Settings",
    "build_env",
    "current_user_principal",
    "load",
    "probe_llm",
    "probe_vault",
    "restrict_to_current_user",
    "save",
]
