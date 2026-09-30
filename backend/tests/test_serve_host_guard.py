"""``scripts/serve.py`` 的「无鉴权不得对外暴露」守卫。

API 没有任何鉴权，绑到 ``0.0.0.0`` 等于把「读全库 + 触发 reprocess（会写
用户的 vault）」送给任何能访问到该地址的人。这条守卫的作用是让**默认**安全：
想暴露必须显式加 ``--expose``，不能靠人记住。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SERVE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "serve.py"

_SPEC = importlib.util.spec_from_file_location("knowledgeflow_serve", SERVE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
serve = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(serve)


# --------------------------------------------------------------------------- #
# is_loopback
@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", " LOCALHOST "])
def test_loopback_hosts(host: str) -> None:
    assert serve.is_loopback(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "10.0.0.5", "::", "example.com"])
def test_non_loopback_hosts(host: str) -> None:
    """``0.0.0.0`` 监听所有网卡 —— 它**不是**「只本机」，这是最容易搞混的一个。"""
    assert serve.is_loopback(host) is False


# --------------------------------------------------------------------------- #
# 启动拦截
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10"])
def test_exposed_host_is_refused_without_expose(host: str, capsys) -> None:
    code = serve.main(["--host", host])
    assert code == serve.EXIT_EXPOSE_REFUSED
    err = capsys.readouterr().err
    assert "拒绝启动" in err
    assert "--expose" in err
    assert serve.EXPOSE_WARNING in err


def test_expose_flag_is_accepted(monkeypatch, capsys) -> None:
    """加了 ``--expose`` 就不再拦 —— 但要真的把警告打出来（不能静默放行）。"""
    captured: dict[str, object] = {}

    def fake_run(app, **kwargs):  # noqa: ANN001 - 只关心传进来的 host
        captured.update(kwargs)

    monkeypatch.setattr("uvicorn.run", fake_run, raising=False)
    code = serve.main(["--host", "0.0.0.0", "--expose", "--mock", "--no-web", "--port", "0"])
    assert code == serve.EXIT_OK
    assert captured["host"] == "0.0.0.0"
    assert serve.EXPOSE_WARNING in capsys.readouterr().out


def test_default_host_is_loopback() -> None:
    """默认参数必须是只本机 —— 这条挂了等于把守卫拆了。"""
    args = serve.parse_args([])
    assert serve.is_loopback(args.host) is True
    assert args.expose is False
