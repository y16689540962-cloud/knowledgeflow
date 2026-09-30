"""用**真实浏览器**验一遍 UI（默认跳过）。

httpx 打 ASGI 能证明路由通，但证明不了「页面真的渲染出来了」：
JS 报错、资源 404、选择器写错，服务端测试全是绿的。

为什么默认跳过：浏览器不是本机必有依赖（定稿第二十七节的精神 ——
不让环境依赖变成前置要求）。显式开启：

```bash
KNOWLEDGEFLOW_BROWSER_TEST=1 ../.venv/bin/python -m pytest tests/test_web_ui_browser.py -v -s
```

想顺手存一张截图：`KNOWLEDGEFLOW_UI_SHOT=/tmp/ui.png`（PNG）。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI

pytest.importorskip("playwright", reason="没装 playwright，跳过真实浏览器测试")

SHOT_ENV = "KNOWLEDGEFLOW_UI_SHOT"


@pytest_asyncio.fixture
async def server(web_app: FastAPI) -> AsyncIterator[str]:
    """把 app 跑在**真实端口**上（随机端口，避免撞冲突）。"""
    uvicorn = pytest.importorskip("uvicorn", reason="没装 uvicorn，起不了真实服务")

    config = uvicorn.Config(web_app, host="127.0.0.1", port=0, log_level="warning")
    uvi_server = uvicorn.Server(config)
    task = asyncio.create_task(uvi_server.serve())
    try:
        while not uvi_server.started:
            await asyncio.sleep(0.05)
        socket = uvi_server.servers[0].sockets[0]
        yield f"http://127.0.0.1:{socket.getsockname()[1]}"
    finally:
        uvi_server.should_exit = True
        await task


@pytest_asyncio.fixture
async def page(server: str) -> AsyncIterator[object]:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        # 用本机已装的 Chrome（channel="chrome"），避开 playwright 自带
        # chromium 的 revision 匹配问题。
        browser = await pw.chromium.launch(channel="chrome", headless=True)
        handler = await browser.new_page()
        try:
            yield handler
        finally:
            await browser.close()


pytestmark = pytest.mark.skipif(
    os.environ.get("KNOWLEDGEFLOW_BROWSER_TEST") != "1",
    reason="真实浏览器测试默认跳过；设 KNOWLEDGEFLOW_BROWSER_TEST=1 开启",
)


async def maybe_shot(page: object, name: str = "ui.png") -> Path | None:
    target = os.environ.get(SHOT_ENV)
    path = Path(target) if target else Path("/tmp") / name
    path.parent.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=str(path), full_page=True)  # type: ignore[attr-defined]
    return path


TEXT = (
    "王小明在视频里说，中国的人工智能产业在 2026 年会继续保持增长，"
    "他判断算力成本会下降三成以上。这个判断基于他过去两年的观察。"
)


async def test_douyin_mode_toggles_panes_without_network(page: object, server: str) -> None:
    """切到「抖音链接」模式必须真的换出对应输入框。

    **不提交表单** —— 提交就会联网打抖音，而定稿第二条要求测试不依赖网络。
    这里只验渲染与切换（服务端逻辑已由 MockTransport 的单测覆盖）。
    """
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))  # type: ignore[attr-defined]

    await page.goto(server, wait_until="networkidle")  # type: ignore[attr-defined]

    manual = page.locator("#pane-manual")  # type: ignore[attr-defined]
    douyin = page.locator("#pane-douyin")  # type: ignore[attr-defined]
    assert await manual.is_visible(), "默认应当是手动粘贴"
    assert not await douyin.is_visible()

    await page.click("#mode-douyin")  # type: ignore[attr-defined]
    assert await douyin.is_visible(), "切到抖音模式后输入框没出来"
    assert not await manual.is_visible()
    assert await page.locator("#f-douyin").is_visible()  # type: ignore[attr-defined]
    assert "采集" in await page.inner_text("#btn-submit")  # type: ignore[attr-defined]

    shot = await maybe_shot(page, "ui-douyin.png")
    print(f"\n  截图：{shot}")

    await page.click("#mode-manual")  # type: ignore[attr-defined]
    assert await manual.is_visible(), "切回手动模式失败"
    assert not await douyin.is_visible()

    assert errors == [], f"浏览器控制台报错了：{errors}"


async def test_ui_renders_and_processes(page: object, server: str) -> None:
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))  # type: ignore[attr-defined]
    # 任何资源 404 都要报出 **URL** —— 浏览器只会静默丢样式，
    # 不记 URL 的话「到底哪个文件没加载」永远查不出来。
    bad_responses: list[str] = []
    page.on(  # type: ignore[attr-defined]
        "response",
        lambda r: bad_responses.append(f"{r.status} {r.url}") if r.status >= 400 else None,
    )

    await page.goto(server, wait_until="networkidle")  # type: ignore[attr-defined]

    # 1. 首屏渲染
    assert "KnowledgeFlow" in await page.title()  # type: ignore[attr-defined]

    # 2. 健康徽章真的从 API 拿到了数据
    health = page.locator("#health")  # type: ignore[attr-defined]
    await health.wait_for(state="visible")
    text = await health.inner_text()
    assert "vault 已配置" in text, f"健康徽章没渲染出来：{text!r}"

    # 3. 走一遍完整处理：填 → 提交 → 出结果
    await page.click("#btn-sample")  # type: ignore[attr-defined]
    await page.fill("#f-text", TEXT)  # type: ignore[attr-defined]
    await page.click("#btn-submit")  # type: ignore[attr-defined]

    result = page.locator("#result")  # type: ignore[attr-defined]
    await result.wait_for(state="visible", timeout=30_000)
    badge = result.locator(".badge")  # type: ignore[attr-defined]
    await badge.wait_for(state="visible")
    outcome = (await badge.inner_text()).strip()
    assert outcome == "completed", f"期望 completed，实际 {outcome}"

    shown = await result.inner_text()
    assert "note_path" in shown, f"结果面板没显示 note_path：{shown[:300]!r}"
    # 13 个步骤真的画出来了
    steps = result.locator(".steps li")  # type: ignore[attr-defined]
    assert await steps.count() >= 12, f"步骤条数不对：{await steps.count()}"

    # 4. 内容库自动刷新出这一条
    rows = page.locator("#list .row")  # type: ignore[attr-defined]
    await rows.first.wait_for(state="visible", timeout=10_000)
    assert await rows.count() >= 1

    # 5. 点详情 → 显示原文
    await rows.first.locator("button", has_text="详情").click()  # type: ignore[attr-defined]
    detail = page.locator("#detail")  # type: ignore[attr-defined]
    await detail.wait_for(state="visible")
    # 等**内容**到位而不是等容器可见 —— 容器在请求发出前就显示了（加载态），
    # 只等 visible 会读到空面板。UI 因此补了加载态，这里等真正的数据。
    await page.wait_for_selector("#detail dt, #detail .error-box", timeout=15_000)  # type: ignore[attr-defined]
    await page.wait_for_selector("#detail pre", timeout=15_000)  # type: ignore[attr-defined]
    detail_text = await detail.inner_text()
    assert "算力成本" in detail_text, f"详情里没有原文：{detail_text[:300]!r}"

    shot = await maybe_shot(page)
    print(f"\n  截图：{shot}")

    assert bad_responses == [], f"有资源加载失败：{bad_responses}"
    assert errors == [], f"浏览器控制台报错了：{errors}"
