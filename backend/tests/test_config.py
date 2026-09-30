"""Config：默认值、格式校验、以及「不在加载期报缺失能力」的强制要求。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import (
    ENV_TEMPLATE_PATH,
    Settings,
    env_template_keys,
    load_settings,
    validate_runtime_requirements,
)
from app.errors import ErrorType, RuntimeRequirementError


def make(**overrides: object) -> Settings:
    """不读真实 .env 的 Settings。"""
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 默认值 / 环境隔离
# --------------------------------------------------------------------------- #
def test_defaults_match_env_example_values() -> None:
    settings = make()
    assert settings.llm_provider == "openai"
    assert settings.llm_timeout_seconds == 120
    assert settings.max_llm_attempts == 3
    assert settings.max_transcript_chars == 12000
    assert settings.max_ocr_chars == 6000
    assert settings.max_total_input_chars == 24000
    assert settings.database_url == "sqlite:///./data/knowledgeflow.db"
    assert settings.download_dir == "./data/media"
    assert settings.obsidian_vault_path == ""
    assert settings.task_reset_timeout_seconds == 900
    assert settings.log_level == "INFO"
    assert settings.log_verbose_content == 0


def test_settings_loads_without_any_api_key() -> None:
    """第二十五节强制：Settings() 不得因外部能力缺失而失败，否则 Phase 1 无法测。"""
    settings = make()
    assert settings.llm_api_key == ""
    assert settings.obsidian_vault is None


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-key")
    monkeypatch.setenv("MAX_TRANSCRIPT_CHARS", "999")
    settings = make()
    assert settings.llm_provider == "deepseek"
    assert settings.llm_api_key == "unit-test-key"
    assert settings.max_transcript_chars == 999


def test_env_file_is_used(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_MODEL=unit-test-model\nLOG_LEVEL=debug\n", encoding="utf-8")
    settings = Settings(_env_file=env_file)  # type: ignore[arg-type]
    assert settings.llm_model == "unit-test-model"
    assert settings.log_level == "DEBUG"


# --------------------------------------------------------------------------- #
# 格式校验
# --------------------------------------------------------------------------- #
def test_invalid_provider_rejected() -> None:
    with pytest.raises(ValidationError):
        make(llm_provider="anthropic")


def test_lowercase_log_level_is_normalized() -> None:
    assert make(log_level="debug").log_level == "DEBUG"


def test_invalid_log_level_rejected() -> None:
    with pytest.raises(ValidationError):
        make(log_level="chatty")


def test_non_positive_budget_rejected() -> None:
    with pytest.raises(ValidationError):
        make(max_total_input_chars=0)


def test_empty_database_url_rejected() -> None:
    with pytest.raises(ValidationError):
        make(database_url="   ")


def test_non_sqlite_database_url_rejected() -> None:
    with pytest.raises(ValidationError):
        make(database_url="postgresql://localhost/kf")


def test_base_url_must_have_scheme() -> None:
    with pytest.raises(ValidationError):
        make(openai_base_url="api.openai.com/v1")


def test_base_url_trailing_slash_stripped() -> None:
    assert make(openai_base_url="https://api.deepseek.com/v1/").openai_base_url == (
        "https://api.deepseek.com/v1"
    )


def test_log_verbose_content_range() -> None:
    assert make(log_verbose_content=1).log_verbose_content == 1
    with pytest.raises(ValidationError):
        make(log_verbose_content=2)


# --------------------------------------------------------------------------- #
# 派生属性
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("provider", "expected"),
    [("openai", "openai-key"), ("deepseek", "deepseek-key")],
)
def test_llm_api_key_follows_provider(provider: str, expected: str) -> None:
    settings = make(llm_provider=provider, openai_api_key="openai-key", deepseek_api_key="deepseek-key")
    assert settings.llm_api_key == expected


def test_content_logging_requires_debug_and_flag() -> None:
    assert make(log_level="DEBUG", log_verbose_content=1).content_logging_enabled is True
    assert make(log_level="DEBUG", log_verbose_content=0).content_logging_enabled is False
    assert make(log_level="INFO", log_verbose_content=1).content_logging_enabled is False


def test_obsidian_vault_expands_and_resolves(tmp_path: Path) -> None:
    settings = make(obsidian_vault_path=str(tmp_path))
    assert settings.obsidian_vault == tmp_path.resolve()
    assert make(obsidian_vault_path="  ").obsidian_vault is None


# --------------------------------------------------------------------------- #
# validate_runtime_requirements
# --------------------------------------------------------------------------- #
def test_runtime_llm_requirement_raises_without_key() -> None:
    with pytest.raises(RuntimeRequirementError) as excinfo:
        validate_runtime_requirements(make(), require_llm=True)
    assert excinfo.value.error_type is ErrorType.CONFIG_LLM_API_KEY_MISSING


def test_runtime_llm_requirement_passes_with_key() -> None:
    validate_runtime_requirements(make(openai_api_key="sk-unit-test"), require_llm=True)


def test_runtime_obsidian_requirement_missing_path() -> None:
    with pytest.raises(RuntimeRequirementError) as excinfo:
        validate_runtime_requirements(make(), require_obsidian=True)
    assert excinfo.value.error_type is ErrorType.CONFIG_OBSIDIAN_VAULT_MISSING


def test_runtime_obsidian_requirement_path_not_found(tmp_path: Path) -> None:
    settings = make(obsidian_vault_path=str(tmp_path / "nope"))
    with pytest.raises(RuntimeRequirementError) as excinfo:
        validate_runtime_requirements(settings, require_obsidian=True)
    assert excinfo.value.error_type is ErrorType.CONFIG_OBSIDIAN_VAULT_NOT_FOUND


def test_runtime_obsidian_requirement_passes(tmp_path: Path) -> None:
    validate_runtime_requirements(make(obsidian_vault_path=str(tmp_path)), require_obsidian=True)


def test_no_requirement_check_by_default() -> None:
    """不传任何 require_* 时必须是 no-op。"""
    validate_runtime_requirements(make())


# --------------------------------------------------------------------------- #
# .env.example 对齐
# --------------------------------------------------------------------------- #
def test_env_example_exists() -> None:
    assert ENV_TEMPLATE_PATH.is_file(), ENV_TEMPLATE_PATH


def test_env_example_keys_are_exactly_settings_fields() -> None:
    keys = env_template_keys()
    assert len(keys) == len(set(keys)), "重复键"
    assert set(keys) == {name.upper() for name in Settings.model_fields}


#: 定稿第二十五节**之外**的补充键。
#: 每加一项，都必须同时在进度清单「五、对定稿的补充与自定项」里登记 ——
#: 否则这个测试会红，逼着人写清楚为什么超出定稿。
EXTRA_ENV_KEYS: frozenset[str] = frozenset({"DOUYIN_COOKIE"})


def test_env_example_matches_full_spec() -> None:
    """第二十五节列出的键一个都不能少；多出来的只允许是**已登记**的补充项。"""
    expected = {
        "LLM_PROVIDER",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "LLM_MODEL",
        "LLM_TIMEOUT_SECONDS",
        "MAX_LLM_ATTEMPTS",
        "MAX_TRANSCRIPT_CHARS",
        "MAX_OCR_CHARS",
        "MAX_TOTAL_INPUT_CHARS",
        "DATABASE_URL",
        "DOWNLOAD_DIR",
        "OBSIDIAN_VAULT_PATH",
        "TASK_RESET_TIMEOUT_SECONDS",
        "LOG_LEVEL",
        "LOG_VERBOSE_CONTENT",
    }
    keys = set(env_template_keys())
    assert expected <= keys, f"少了定稿规定的键：{sorted(expected - keys)}"
    assert keys - expected == EXTRA_ENV_KEYS, (
        f"出现了未登记的补充键：{sorted(keys - expected - EXTRA_ENV_KEYS)}"
    )


def test_env_example_has_no_secret_values() -> None:
    """示例文件里绝不能出现真实密钥。"""
    text = ENV_TEMPLATE_PATH.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        assert value == "" or not any(
            marker in value.lower() for marker in ("sk-", "secret", "token")
        ), f"{key} 疑似写入真实密钥"


def test_load_settings_returns_settings() -> None:
    assert isinstance(load_settings(_env_file=None), Settings)
