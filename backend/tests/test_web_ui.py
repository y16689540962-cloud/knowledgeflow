"""Phase 9 · 最小可用 Web UI（`app/web/` + FastAPI 挂载）。

定稿第二十八节说「不开发 React UI」，所以这里是**零构建原生静态页**。
这份测试守住三件容易悄悄坏掉的事：

1. **静态页不能吃掉 ``/api/*``、``/docs``、``/openapi.json``**
   —— Starlette 按路由注册顺序匹配，mount 早一个位置就全完，而且不会报错。
2. **HTML 里引用的资源必须真的存在**
   —— 少个文件浏览器只会静默 404，页面样式全丢但没人发现。
3. **不许出现 ``innerHTML``**
   —— 数据要过 LLM，「用 innerHTML 图省事」迟早有人干。这条纪律用测试钉死。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.api import WEB_DIR, create_app
from app.config import Settings
from app.db.session import Database
from app.providers.mock import MockProvider

INDEX = WEB_DIR / "index.html"
SCRIPT = WEB_DIR / "app.js"
STYLE = WEB_DIR / "style.css"

#: ``<link href=...>`` / ``<script src=...>`` 里写的**静态资源**路径。
#: 只认资源类扩展名 —— 用「任意带点的路径」会把 footer 里的 ``/openapi.json``
#: 也算进来，而它是 API 路由不是磁盘文件。
ASSET_REF = re.compile(r'(?:href|src)="(/[A-Za-z0-9._-]+\.(?:css|js|ico|png|svg|woff2?))"')


def strip_js_comments(source: str) -> str:
    """剥掉 JS 注释**与字符串字面量**。

    不能直接对源码做子串匹配：注释里写「禁止用 innerHTML」就会被判违规，
    检查器把自己的文档当成了证据 —— 这个坑本项目已经踩过一次。
    """
    out: list[str] = []
    i, n = 0, len(source)
    in_line = in_block = in_str = False
    quote = ""
    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if in_line:
            if ch == "\n":
                in_line = False
                out.append(ch)
            i += 1
        elif in_block:
            if ch == "*" and nxt == "/":
                in_block = False
                i += 2
            else:
                if ch == "\n":
                    out.append(ch)
                i += 1
        elif in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(nxt)
                i += 2
            else:
                if ch == quote:
                    in_str = False
                i += 1
        elif ch in "\"'`":
            in_str, quote = True, ch
            out.append(ch)
            i += 1
        elif ch == "/" and nxt == "/":
            in_line = True
            i += 2
        elif ch == "/" and nxt == "*":
            in_block = True
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


@pytest_asyncio.fixture
async def web_client(web_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with web_app.router.lifespan_context(web_app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app), base_url="http://test"
        ) as client:
            yield client


# --------------------------------------------------------------------------- #
# 静态资源
# --------------------------------------------------------------------------- #
def test_web_dir_contains_all_assets() -> None:
    for path in (INDEX, SCRIPT, STYLE):
        assert path.is_file(), f"静态资源缺失：{path}"


async def test_index_is_served(web_client: httpx.AsyncClient) -> None:
    response = await web_client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "KnowledgeFlow" in response.text


@pytest.mark.parametrize("asset", ["/app.js", "/style.css"])
async def test_assets_are_served(web_client: httpx.AsyncClient, asset: str) -> None:
    response = await web_client.get(asset)
    assert response.status_code == 200, f"{asset} 拿不到（浏览器只会静默 404）"


def test_every_asset_reference_exists() -> None:
    """HTML 里写了什么，磁盘上就得有什么。"""
    refs = ASSET_REF.findall(INDEX.read_text(encoding="utf-8"))
    assert refs, "index.html 里没找到任何资源引用（正则可能失效了）"
    for ref in refs:
        assert (WEB_DIR / ref.lstrip("/")).is_file(), f"index.html 引用了不存在的 {ref}"


# --------------------------------------------------------------------------- #
# 挂载顺序（最容易静默坏掉的一条）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ["/api/health", "/docs", "/openapi.json"])
async def test_mount_does_not_shadow_api(web_client: httpx.AsyncClient, path: str) -> None:
    """``app.mount("/")`` 挂在 API 路由之前的话，这些全会 404 —— 且没有任何报错。"""
    assert (await web_client.get(path)).status_code == 200


async def test_api_routes_still_work_with_web_mounted(web_client: httpx.AsyncClient) -> None:
    body = (await web_client.get("/api/health")).json()
    assert body["llm_provider"] == "mock"


async def test_web_can_be_turned_off(
    db: Database, settings_defaults: Settings, vault_root: Path
) -> None:
    """``mount_web=False`` → 纯 JSON API 模式。"""
    app = create_app(
        settings=settings_defaults,
        database=db,
        provider=MockProvider(payload={}),
        vault_root=vault_root,
        configure_logging=False,
        run_startup_recovery=False,
        mount_web=False,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/")).status_code == 404
        assert (await client.get("/api/health")).status_code == 200


def test_mount_missing_dir_is_silent(
    db: Database, settings_defaults: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """少了静态资源不该让后端起不来 —— 只记日志 + 返回 False。"""
    from app.api import mount_web_ui

    monkeypatch.setattr("app.api.app.WEB_DIR", Path("/tmp/definitely-not-here-kf-web"))
    app = create_app(
        settings=settings_defaults,
        database=db,
        provider=MockProvider(payload={}),
        configure_logging=False,
        run_startup_recovery=False,
        mount_web=False,
    )
    assert mount_web_ui(app) is False


# --------------------------------------------------------------------------- #
# 前端代码纪律
# --------------------------------------------------------------------------- #
def test_no_inner_html_anywhere() -> None:
    """**不许用 innerHTML** —— 数据要经 LLM，谁也没法断言内容里有什么。

    这条纪律一旦破掉，危害是静默的（看起来功能全正常）。所以用测试钉死。
    """
    # 注释 / 字符串里提到 innerHTML 不算违规 —— 先剥掉它们再查真正的代码。
    source = strip_js_comments(SCRIPT.read_text(encoding="utf-8"))
    offenders = [
        line.strip()
        for line in source.splitlines()
        if re.search(r"\.(?:inner|outer)HTML\b|insertAdjacentHTML\s*\(", line)
    ]
    assert offenders == [], f"用了 innerHTML（XSS 风险）：{offenders}"


def test_javascript_syntax_is_valid() -> None:
    """用 node 真校验 JS 语法 —— Python 侧完全看不出 JS 写错了。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("本机没有 node，跳过 JS 语法校验")

    result = subprocess.run(
        [node, "--check", str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, f"JS 语法错误：\n{result.stderr}"


def test_html_has_no_inline_event_handlers() -> None:
    """``onclick=`` 这类内联处理器一旦开口，CSP 就得彻底放开。"""
    html = INDEX.read_text(encoding="utf-8")
    offenders = re.findall(r"\son[a-z]+\s*=", html)
    assert offenders == [], f"HTML 里有内联事件处理器：{offenders}"


# --------------------------------------------------------------------------- #
# 输入模式（手动粘贴 / 抖音链接）
# --------------------------------------------------------------------------- #
GET_ID = re.compile(r'getElementById\("([^"]+)"\)')


def test_every_get_element_by_id_exists_in_html() -> None:
    """JS 里引用的 id 必须真的在 HTML 里。

    少一个不会报错 —— 只会在运行时静默失灵（点了没反应 / 结果面板空的）。
    切输入模式这种「改了 HTML 忘了改 JS」的活最容易踩，所以用一条通用守卫钉死。
    """
    html = INDEX.read_text(encoding="utf-8")
    ids = set(re.findall(r'id="([^"]+)"', html))
    used = set(GET_ID.findall(strip_js_comments(SCRIPT.read_text(encoding="utf-8"))))
    assert used, "一个 getElementById 都没解析出来（正则可能失效了）"
    missing = sorted(used - ids)
    assert missing == [], f"app.js 引用了 HTML 里不存在的 id：{missing}"


def test_douyin_mode_is_wired_to_the_ingest_api() -> None:
    """抖音链接必须有真的 API 调用 —— 只在文案里提一句不算「能用」。"""
    source = strip_js_comments(SCRIPT.read_text(encoding="utf-8"))
    assert '"/api/ingest/douyin"' in source, "UI 没有调抖音采集接口"
    assert "process: true" in source, "采集完不接着跑 A 线的话，用户拿不到笔记"


def test_index_offers_both_modes() -> None:
    html = INDEX.read_text(encoding="utf-8")
    for needed in (
        'id="mode-manual"',
        'id="mode-douyin"',
        'id="pane-manual"',
        'id="pane-douyin"',
        'id="f-douyin"',
    ):
        assert needed in html, f"index.html 少了 {needed}"


def test_mode_buttons_are_not_submit_buttons() -> None:
    """模式切换按钮必须是 ``type=\"button\"``，否则回车/点击会误提交表单。"""
    html = INDEX.read_text(encoding="utf-8")
    for button_id in ("mode-manual", "mode-douyin"):
        pattern = rf'<button[^>]*id="{button_id}"[^>]*>'
        match = re.search(pattern, html)
        assert match is not None, f"找不到 {button_id}"
        assert 'type="button"' in match.group(0), f'{button_id} 不是 type="button"'
