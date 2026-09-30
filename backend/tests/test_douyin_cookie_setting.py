"""抖音登录态从 ``.env`` 来（第二十九条）。

为什么要有这条路：Cookie 放在请求体或命令行上都会留下痕迹（shell 历史 / 日志），
而它是**账号级凭据** —— 比 API Key 还敏感。写进 ``.env``（已被 gitignore 覆盖）
让人填一次就长期生效，且全程只判断「有没有」，从不回显值。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from app.api.container import build_container, make_douyin_factory
from app.config import Settings, env_template_keys
from app.providers.mock import MockProvider
from tests.test_api_app import running

COOKIE = "sessionid=测试用的假值; ttwid=also-fake"


def _settings(tmp_path: Path, cookie: str = "") -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        obsidian_vault_path=str(tmp_path),
        douyin_cookie=cookie,
    )


# --------------------------------------------------------------------------- #
# 配置本身
# --------------------------------------------------------------------------- #
def test_default_cookie_is_empty(tmp_path: Path) -> None:
    assert _settings(tmp_path).douyin_cookie == ""


def test_cookie_is_read_from_env_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(f"DOUYIN_COOKIE={COOKIE}\n", encoding="utf-8")

    loaded = Settings(_env_file=str(env))  # type: ignore[call-arg]

    assert loaded.douyin_cookie == COOKIE


def test_env_example_lists_the_cookie_key() -> None:
    """``.env.example`` 与 ``Settings`` 逐键对齐：加了配置项就要在模板里出现。"""
    assert "DOUYIN_COOKIE" in env_template_keys()
    assert "DOUYIN_COOKIE" in {name.upper() for name in Settings.model_fields}


# --------------------------------------------------------------------------- #
# 工厂回落
# --------------------------------------------------------------------------- #
def test_factory_uses_settings_cookie_when_request_gives_none(tmp_path: Path) -> None:
    factory = make_douyin_factory(_settings(tmp_path, COOKIE))
    source = factory(15, None)
    # 只读私有属性做断言：这里关心的正是「请求没给时会不会用配置里的那份」。
    assert source._cookie == COOKIE


def test_factory_explicit_cookie_wins(tmp_path: Path) -> None:
    factory = make_douyin_factory(_settings(tmp_path, COOKIE))
    source = factory(15, "sessionid=请求里带的")
    assert source._cookie == "sessionid=请求里带的"


def test_factory_blank_request_cookie_falls_back(tmp_path: Path) -> None:
    """请求里给了空串（前端没填时常见）→ 回落到配置，而不是把空串当登录态。"""
    factory = make_douyin_factory(_settings(tmp_path, COOKIE))
    assert factory(15, "   ")._cookie == COOKIE


def test_factory_without_any_cookie_is_anonymous(tmp_path: Path) -> None:
    source = make_douyin_factory(_settings(tmp_path))(15, None)
    assert source._cookie is None


def test_build_container_wires_the_settings_cookie(tmp_path: Path) -> None:
    container = build_container(
        settings=_settings(tmp_path, COOKIE),
        provider=MockProvider(payload={}),
    )
    assert container.douyin_source_factory(15, None)._cookie == COOKIE


# --------------------------------------------------------------------------- #
# 不回显
# --------------------------------------------------------------------------- #
async def test_health_never_leaks_the_cookie(tmp_path: Path) -> None:
    from app.api import create_app

    app: FastAPI = create_app(
        settings=_settings(tmp_path, COOKIE),
        provider=MockProvider(payload={}),
        vault_root=tmp_path,
        configure_logging=False,
        run_startup_recovery=False,
    )

    async with running(app) as client:
        body = (await client.get("/api/health")).text

    assert COOKIE not in body
    assert "sessionid" not in body
    assert "cookie" not in body.lower()
