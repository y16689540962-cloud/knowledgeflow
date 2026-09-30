"""桌面配置层：``settings.json`` ↔ 后端环境变量。

**关键约束（定稿第三、七节）**：不发明第二套配置机制。
后端已经用环境变量 + pydantic-settings 管配置，这里只是：

    settings.json（桌面可写、0600、人可读）
        ↓ 映射
    后端现有的 18 个环境变量
        ↓
    原样启动后端

所以后端一行都不用改，也不会出现「桌面配置文件」和「.env」两套真相。

**一个必须在这里修掉的坑**：后端默认 ``DATABASE_URL=sqlite:///./data/...``
是**相对 cwd** 的。Finder 启动的 .app 工作目录是 ``/``，那条相对路径会落到
``/data/`` 上（必然写不进去）。所以这里一律生成**绝对路径**。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final

from kf_app.paths import AppPaths

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


@dataclass
class Settings:
    """桌面端的配置。写入 ``config/settings.json``。"""

    obsidian_vault_path: str = ""
    llm_provider: str = "deepseek"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = "deepseek-flash"
    port: int = DEFAULT_PORT
    #: 可选：抖音采集用的登录态（第二十九条：只填使用者自己的）
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
    """原子写 + ``0600``。API Key 在里面，必须是只有本人可读。"""
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
        _os.chmod(tmp, 0o600)
        _os.replace(tmp, file)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return file


# --------------------------------------------------------------------------- #
# 映射到后端环境变量
# --------------------------------------------------------------------------- #
def build_env(paths: AppPaths, settings: Settings) -> dict[str, str]:
    """把桌面配置翻译成后端认识的环境变量。

    **绝对路径是这里的重点**：数据库、下载目录、缓存目录全部显式给出绝对值，
    这样无论 .app 从哪启动、cwd 是什么，行为都一致。
    """
    env = dict(os.environ)

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

    # 存储：绝对路径（见模块 docstring）
    env["DATABASE_URL"] = f"sqlite:///{paths.database_file}"
    env["DOWNLOAD_DIR"] = str(paths.media_dir)

    env["OBSIDIAN_VAULT_PATH"] = settings.obsidian_vault_path
    if settings.douyin_cookie:
        env["DOUYIN_COOKIE"] = settings.douyin_cookie

    # 模型缓存也放进应用数据目录：不污染 ~/.cache，卸载时能一起清掉
    env["HF_HOME"] = str(paths.cache / "huggingface")

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
