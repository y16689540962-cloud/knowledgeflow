"""配置加载与运行时能力校验（分离，强制）。

定稿文档第二十五节要求：

* ``Settings()`` 只做「读 .env / 类型校验 / 默认值 / 格式校验」，
  **不得因为尚未使用的外部能力缺失而阻塞 Phase 1 测试**。
* ``validate_runtime_requirements()`` 只在真正启动应用、或执行需要对应能力的
  命令时调用。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.errors import ErrorType, RuntimeRequirementError

LLMProviderName = Literal["openai", "deepseek"]
LogLevelName = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

LLM_PROVIDERS: tuple[str, ...] = ("openai", "deepseek")

#: ``error_type`` 之外，``.env`` 里允许出现的全部键（用于与 Settings 字段对齐）。
ENV_TEMPLATE_PATH = Path(__file__).resolve().parents[1] / ".env.example"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- LLM ---
    llm_provider: LLMProviderName = "openai"
    openai_api_key: str = ""
    openai_base_url: str = ""
    deepseek_api_key: str = ""
    deepseek_base_url: str = ""
    llm_model: str = ""
    llm_timeout_seconds: int = Field(default=120, gt=0)
    max_llm_attempts: int = Field(default=3, gt=0)

    # --- 文本预算 ---
    max_transcript_chars: int = Field(default=12000, gt=0)
    max_ocr_chars: int = Field(default=6000, gt=0)
    max_total_input_chars: int = Field(default=24000, gt=0)

    # --- 存储 ---
    database_url: str = "sqlite:///./data/knowledgeflow.db"
    download_dir: str = "./data/media"

    # --- Obsidian ---
    obsidian_vault_path: str = ""

    # --- 抖音采集（第二十九条：只用用户自己的登录态） ---
    #: 留空 = 匿名访问（抖音大概率返回验证页 → 如实报 ``REQUEST_BLOCKED``）。
    #: **敏感串**：不回显、不进日志。
    douyin_cookie: str = ""

    # --- 任务 ---
    task_reset_timeout_seconds: int = Field(default=900, gt=0)

    # --- 日志 ---
    log_level: LogLevelName = "INFO"
    log_verbose_content: int = Field(default=0, ge=0, le=1)

    # ------------------------------------------------------------------ #
    # 格式校验
    # ------------------------------------------------------------------ #
    @field_validator("openai_base_url", "deepseek_base_url", mode="after")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        if not value.startswith(("http://", "https://")):
            raise ValueError("BASE_URL 必须以 http:// 或 https:// 开头")
        return value.rstrip("/")

    @field_validator("database_url", mode="after")
    @classmethod
    def _check_database_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("DATABASE_URL 不能为空")
        if not value.startswith("sqlite"):
            raise ValueError("V0.3.3 只支持 sqlite DATABASE_URL")
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    # ------------------------------------------------------------------ #
    # 派生属性
    # ------------------------------------------------------------------ #
    @property
    def llm_api_key(self) -> str:
        return self.openai_api_key if self.llm_provider == "openai" else self.deepseek_api_key

    @property
    def llm_base_url(self) -> str:
        return self.openai_base_url if self.llm_provider == "openai" else self.deepseek_base_url

    @property
    def obsidian_vault(self) -> Path | None:
        """返回 vault 绝对路径；未配置时返回 ``None``。"""
        raw = self.obsidian_vault_path.strip()
        if not raw:
            return None
        return Path(raw).expanduser().resolve()

    @property
    def content_logging_enabled(self) -> bool:
        """正文内容只有 DEBUG + LOG_VERBOSE_CONTENT=1 同时满足才允许输出。"""
        return self.log_level == "DEBUG" and self.log_verbose_content == 1


def load_settings(**overrides: object) -> Settings:
    """加载配置。任何缺失的外部能力都不在这里报错。"""
    return Settings(**overrides)  # type: ignore[arg-type]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def validate_runtime_requirements(
    settings: Settings,
    *,
    require_llm: bool = False,
    require_obsidian: bool = False,
) -> None:
    """在真正需要对应能力时显式失败，而不是等到深层代码崩溃。

    Phase 1 的 ``pytest`` 不应调用本函数。
    """
    if require_llm and not settings.llm_api_key.strip():
        raise RuntimeRequirementError(
            f"LLM_PROVIDER={settings.llm_provider} 但对应的 API Key 为空，无法执行 LLM 能力",
            error_type=ErrorType.CONFIG_LLM_API_KEY_MISSING,
            context={"provider": settings.llm_provider},
        )

    if require_obsidian:
        raw = settings.obsidian_vault_path.strip()
        if not raw:
            raise RuntimeRequirementError(
                "需要写入 Obsidian 但 OBSIDIAN_VAULT_PATH 未配置",
                error_type=ErrorType.CONFIG_OBSIDIAN_VAULT_MISSING,
            )
        vault = Path(raw).expanduser()
        if not vault.is_dir():
            raise RuntimeRequirementError(
                f"OBSIDIAN_VAULT_PATH 指向的目录不存在：{vault}",
                error_type=ErrorType.CONFIG_OBSIDIAN_VAULT_NOT_FOUND,
                context={"path": str(vault)},
            )


def env_template_keys(path: Path | None = None) -> list[str]:
    """解析 ``.env.example``，返回其中出现的键名（保持文件顺序）。"""
    target = path or ENV_TEMPLATE_PATH
    keys: list[str] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key:
            keys.append(key)
    return keys


__all__ = [
    "Settings",
    "LLMProviderName",
    "LogLevelName",
    "LLM_PROVIDERS",
    "ENV_TEMPLATE_PATH",
    "load_settings",
    "get_settings",
    "validate_runtime_requirements",
    "env_template_keys",
]
