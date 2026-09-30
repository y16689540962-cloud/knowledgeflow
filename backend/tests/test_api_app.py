"""Phase 8 · FastAPI 应用层测试。

**真实 HTTP 往返**：``httpx.AsyncClient`` + ``ASGITransport`` 打在 app 上，
不占用真实端口、不需要起进程，但中间件 / 路由 / 异常处理器 / OpenAPI
全都是真的。

lifespan 也真的跑 —— 否则「启动恢复」这条第十七节的强制要求就没人验证。
``ASGITransport`` 自己不触发 lifespan，所以这里显式包一层。
"""

from __future__ import annotations

import dataclasses
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.api import create_app
from app.api.app import EXTENSION_ORIGIN_PATTERN
from app.api.errors import STATUS_BY_ERROR_TYPE
from app.config import Settings
from app.db.mappers import ContentInsert
from app.db.repository import ContentRepository
from app.db.session import Database
from app.errors import ErrorType
from app.ingestion import RequestBlockedError
from app.ingestion.manual import ManualPastePayload, ManualPasteSource
from app.normalization.service import normalize_content
from app.providers.mock import MockProvider
from app.schemas import RawContent

BASE_URL = "http://test"

#: 一条够 A 线用的正文。
TEXT = (
    "王小明在视频里说，中国的人工智能产业在 2026 年会继续保持增长，"
    "他判断算力成本会下降三成以上。这个判断基于他过去两年的观察。"
)


@asynccontextmanager
async def running(
    app: FastAPI, *, raise_app_exceptions: bool = True
) -> AsyncIterator[httpx.AsyncClient]:
    """带着 lifespan 起一个真 HTTP 客户端。

    ``raise_app_exceptions=False`` 只在测「未预期异常 → 500」时开：
    Starlette 的 ServerErrorMiddleware 就算已经发出 500 响应，也会把异常
    再抛一次（为了让 TestClient 看得见），而 ``ASGITransport`` 默认
    ``raise_app_exceptions=True`` 会把这次 re-raise 变成测试里的异常。
    关掉它才能看到**真实响应**。
    """
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions),
            base_url=BASE_URL,
        ) as client:
            yield client


def manual_payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "title": "接口测试内容",
        "author": "王小明",
        "raw_text": TEXT,
        "media_type": "text",
    }
    body.update(overrides)
    return body


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def settings_with_vault(settings_defaults: Settings, vault_root: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        obsidian_vault_path=str(vault_root),
    )


@pytest.fixture
def api_app(
    db: Database,
    settings_with_vault: Settings,
    vault_root: Path,
    valid_analysis_payload: dict,
) -> FastAPI:
    """一个接了临时库 + 临时 vault + Mock LLM 的完整应用。"""
    return create_app(
        settings=settings_with_vault,
        database=db,
        provider=MockProvider(payload=valid_analysis_payload),
        vault_root=vault_root,
        configure_logging=False,
        run_startup_recovery=True,
    )


@pytest_asyncio.fixture
async def client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with running(api_app) as client:
        yield client


# --------------------------------------------------------------------------- #
# 健康 / 元信息
# --------------------------------------------------------------------------- #
async def test_health_reports_capabilities(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/health")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ok"
    assert body["llm_provider"] == "mock"
    assert body["vault_configured"] is True
    assert body["recovery"] is not None  # 启动时真的跑过一次


async def test_health_never_leaks_secrets_or_paths(
    client: httpx.AsyncClient, vault_root: Path
) -> None:
    """健康检查不能变成「本机信息泄露接口」。"""
    text = (await client.get("/api/health")).text
    assert "sk-" not in text
    assert "api_key" not in text.lower()
    assert str(vault_root) not in text


# --------------------------------------------------------------------------- #
# 启动恢复（第十七节，强制）
# --------------------------------------------------------------------------- #
async def test_startup_recovery_resumes_stale_task(
    db: Database,
    repo: ContentRepository,
    settings_with_vault: Settings,
    vault_root: Path,
    raw_content: RawContent,
    valid_analysis_payload: dict,
) -> None:
    """一条卡在 ``processing`` 且早就超时的任务，必须在启动时被捞回来。"""
    normalized = normalize_content(raw_content)
    insert = ContentInsert.from_normalized(normalized, content_id="stale-001", status="processing")
    insert = dataclasses.replace(insert, created_at="2000-01-01T00:00:00Z")
    await repo.insert_content(insert)

    app = create_app(
        settings=settings_with_vault,
        database=db,
        provider=MockProvider(payload=valid_analysis_payload),
        vault_root=vault_root,
        configure_logging=False,
        run_startup_recovery=True,
    )
    async with running(app) as client:
        report = (await client.get("/api/tasks/recovery")).json()

    assert report["reset"] == ["stale-001"]
    assert report["resumed"] == ["stale-001"]
    assert report["resume_failures"] == []

    detail = (await repo.get_content("stale-001"))
    assert detail is not None and detail.status == "completed"


async def test_startup_recovery_can_be_disabled(
    db: Database,
    repo: ContentRepository,
    settings_with_vault: Settings,
    vault_root: Path,
    raw_content: RawContent,
) -> None:
    """``run_startup_recovery=False`` 时确实不碰任何任务。"""
    normalized = normalize_content(raw_content)
    insert = ContentInsert.from_normalized(normalized, content_id="stale-002", status="processing")
    await repo.insert_content(dataclasses.replace(insert, created_at="2000-01-01T00:00:00Z"))

    app = create_app(
        settings=settings_with_vault,
        database=db,
        provider=MockProvider(payload={}),
        vault_root=vault_root,
        configure_logging=False,
        run_startup_recovery=False,
    )
    async with running(app) as client:
        await client.get("/api/health")

    row = await repo.get_content("stale-002")
    assert row is not None and row.status == "processing"


async def test_startup_recovery_failure_does_not_block_boot(
    db: Database,
    settings_with_vault: Settings,
    vault_root: Path,
    valid_analysis_payload: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """恢复流程本身炸了，应用**照常起来** —— 错误留在 ``recovery_error`` 里。"""
    app = create_app(
        settings=settings_with_vault,
        database=db,
        provider=MockProvider(payload=valid_analysis_payload),
        vault_root=vault_root,
        configure_logging=False,
        run_startup_recovery=False,  # 手动控制，避免 lifespan 抢先跑
    )

    async def boom() -> object:
        raise RuntimeError("数据库被拔了")

    monkeypatch.setattr(app.state.container.pipeline, "recover_and_resume", boom)

    async with app.router.lifespan_context(app):
        # 启动恢复挂了，但 lifespan 没把它抛出来
        assert app.state.container.recovery_error is None

    from app.api.container import run_startup_recovery

    await run_startup_recovery(app.state.container)
    assert app.state.container.recovery_error is not None
    assert "数据库被拔了" in app.state.container.recovery_error


# --------------------------------------------------------------------------- #
# 手动粘贴采集
# --------------------------------------------------------------------------- #
async def test_ingest_manual_runs_full_pipeline(
    client: httpx.AsyncClient, vault_root: Path
) -> None:
    response = await client.post("/api/ingest/manual", json=manual_payload())
    assert response.status_code == 200

    body = response.json()
    result = body["result"]
    assert result["outcome"] == "completed", result
    assert result["content_id"]
    assert result["note_path"]

    # 13 个步骤一个不省 —— 前端要能看出卡在哪一步
    steps = [step["step"] for step in result["steps"]]
    assert "preflight" in steps and "write_obsidian" in steps and "complete" in steps
    assert all(step["status"] == "ok" for step in result["steps"])

    # 笔记真的落到了 vault 里
    note = vault_root / result["note_path"]
    assert note.is_file()


async def test_ingest_manual_twice_is_duplicate(client: httpx.AsyncClient) -> None:
    first = await client.post("/api/ingest/manual", json=manual_payload())
    assert first.json()["result"]["outcome"] == "completed"

    second = await client.post("/api/ingest/manual", json=manual_payload())
    assert second.status_code == 200  # duplicate 不是失败
    body = second.json()
    assert body["result"]["outcome"] == "duplicate"
    assert body["result"]["duplicate_of"] == first.json()["result"]["content_id"]
    assert body["result"]["deduplicated_by"] in ("source_id", "content_hash")


async def test_ingest_manual_without_process_only_collects(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/ingest/manual", json=manual_payload(process=False)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["result"] is None  # 不是「空的成功」，是没要求跑
    assert body["raw"]["source"] == "manual"


async def test_ingest_manual_empty_text_is_422(client: httpx.AsyncClient) -> None:
    """空白正文 → ``EMPTY_SOURCE_TEXT`` → 422（不产出空的成功）。"""
    response = await client.post(
        "/api/ingest/manual", json=manual_payload(title=None, author=None, raw_text="   ")
    )
    # 状态码负责「成败」；body 永远是完整结构（含 failed_step 与 13 个步骤），
    # 所以 error_type 在 result 里，不在顶层。
    assert response.status_code == 422
    result = response.json()["result"]
    assert result["error_type"] == ErrorType.EMPTY_SOURCE_TEXT.value
    assert result["failed_step"] is not None


async def test_ingest_manual_rejects_unknown_field(client: httpx.AsyncClient) -> None:
    """请求体拼错字段必须报出来，不能静默忽略。"""
    response = await client.post("/api/ingest/manual", json=manual_payload(oops=1))
    assert response.status_code == 422
    assert "detail" in response.json()  # FastAPI 标准校验错误格式


# --------------------------------------------------------------------------- #
# 抖音采集
# --------------------------------------------------------------------------- #
async def test_ingest_douyin_uses_injected_source(
    db: Database,
    settings_with_vault: Settings,
    vault_root: Path,
    valid_analysis_payload: dict,
) -> None:
    """采集源工厂可注入 —— 测试因此不必联网（定稿第二条原则）。"""

    class FakeDouyinSource:
        async def fetch(self, url: str) -> RawContent:
            assert url.startswith("https://www.douyin.com/video/")
            return await ManualPasteSource().fetch(
                ManualPastePayload(title="假抖音", raw_text=TEXT, media_type="video")
            )

        async def aclose(self) -> None:
            return None

    app = create_app(
        settings=settings_with_vault,
        database=db,
        provider=MockProvider(payload=valid_analysis_payload),
        vault_root=vault_root,
        configure_logging=False,
        run_startup_recovery=False,
        douyin_source_factory=lambda timeout, cookie=None: FakeDouyinSource(),  # type: ignore[arg-type,return-value]
    )
    async with running(app) as client:
        response = await client.post(
            "/api/ingest/douyin", json={"url": "https://www.douyin.com/video/1234567890"}
        )

    assert response.status_code == 200, response.text
    assert response.json()["result"]["outcome"] == "completed"


async def test_ingest_douyin_forwards_cookie_to_source(
    db: Database,
    settings_with_vault: Settings,
    vault_root: Path,
    valid_analysis_payload: dict,
) -> None:
    """用户自己的登录态要真的传到采集源里（第二十九条）。

    同时钉住「不回显」：请求体里有 Cookie，响应里不许出现它。
    """
    seen: dict[str, object] = {}
    secret = "sessionid=super-secret-value"

    class FakeDouyinSource:
        async def fetch(self, url: str) -> RawContent:
            return await ManualPasteSource().fetch(
                ManualPastePayload(title="假抖音", raw_text=TEXT, media_type="video")
            )

        async def aclose(self) -> None:
            return None

    def factory(timeout: int, cookie: str | None = None) -> FakeDouyinSource:
        seen["timeout"] = timeout
        seen["cookie"] = cookie
        return FakeDouyinSource()

    app = create_app(
        settings=settings_with_vault,
        database=db,
        provider=MockProvider(payload=valid_analysis_payload),
        vault_root=vault_root,
        configure_logging=False,
        run_startup_recovery=False,
        douyin_source_factory=factory,  # type: ignore[arg-type]
    )
    async with running(app) as client:
        response = await client.post(
            "/api/ingest/douyin",
            json={"url": "https://www.douyin.com/video/1234567890", "cookie": secret},
        )

    assert seen["cookie"] == secret
    assert secret not in response.text


async def test_ingest_douyin_blocked_is_502(
    db: Database,
    settings_with_vault: Settings,
    vault_root: Path,
    valid_analysis_payload: dict,
) -> None:
    """被平台挡住 → 502 + 真实 ``error_type``（不伪造成功，也不算 500 我方故障）。"""

    class BlockedSource:
        async def fetch(self, url: str) -> RawContent:
            raise RequestBlockedError("抖音返回了验证页")

        async def aclose(self) -> None:
            return None

    app = create_app(
        settings=settings_with_vault,
        database=db,
        provider=MockProvider(payload=valid_analysis_payload),
        vault_root=vault_root,
        configure_logging=False,
        run_startup_recovery=False,
        douyin_source_factory=lambda timeout, cookie=None: BlockedSource(),  # type: ignore[arg-type,return-value]
    )
    async with running(app) as client:
        response = await client.post(
            "/api/ingest/douyin", json={"url": "https://www.douyin.com/video/1234567890"}
        )

    assert response.status_code == 502
    assert response.json()["error_type"] == ErrorType.REQUEST_BLOCKED.value


@pytest.mark.parametrize(
    "url,expected_error",
    [
        ("not-a-url", ErrorType.INVALID_URL.value),
        ("https://example.com/video/1234567890", ErrorType.DOMAIN_NOT_ALLOWED.value),
    ],
)
async def test_ingest_douyin_rejects_bad_url_without_network(
    client: httpx.AsyncClient, url: str, expected_error: str
) -> None:
    """URL 校验发生在**发请求之前** —— 所以这条测试零网络。"""
    response = await client.post("/api/ingest/douyin", json={"url": url})
    assert response.status_code == 400
    assert response.json()["error_type"] == expected_error


# --------------------------------------------------------------------------- #
# 内容只读
# --------------------------------------------------------------------------- #
async def test_list_contents_and_pagination(client: httpx.AsyncClient) -> None:
    for index in range(3):
        await client.post(
            "/api/ingest/manual", json=manual_payload(title=f"内容 {index}")
        )

    listed = (await client.get("/api/contents", params={"limit": 2})).json()
    assert listed["total"] == 3
    assert len(listed["items"]) == 2
    assert listed["limit"] == 2 and listed["offset"] == 0

    second_page = (await client.get("/api/contents", params={"limit": 2, "offset": 2})).json()
    assert len(second_page["items"]) == 1
    # 分页不重叠
    assert {item["id"] for item in listed["items"]}.isdisjoint(
        {item["id"] for item in second_page["items"]}
    )


async def test_list_contents_filter_by_status(client: httpx.AsyncClient) -> None:
    await client.post("/api/ingest/manual", json=manual_payload())
    completed = (await client.get("/api/contents", params={"status": "completed"})).json()
    failed = (await client.get("/api/contents", params={"status": "failed"})).json()
    assert completed["total"] == 1
    assert failed["total"] == 0


async def test_list_contents_limit_is_capped(client: httpx.AsyncClient) -> None:
    """``?limit=1000000`` 不能把库读穿。"""
    response = await client.get("/api/contents", params={"limit": 100000})
    assert response.status_code == 422


async def test_get_content_detail(client: httpx.AsyncClient) -> None:
    created = (await client.post("/api/ingest/manual", json=manual_payload())).json()
    content_id = created["result"]["content_id"]

    detail = (await client.get(f"/api/contents/{content_id}")).json()
    assert detail["id"] == content_id
    assert detail["status"] == "completed"
    assert detail["analysis_count"] == 1
    assert detail["current_analysis"] is not None
    # 详情页要能显示原文
    assert TEXT in detail["raw_text"]


async def test_get_content_missing_is_404(client:httpx.AsyncClient) -> None:
    response = await client.get("/api/contents/does-not-exist")
    assert response.status_code == 404
    assert response.json()["error_type"] == ErrorType.CONTENT_NOT_FOUND.value


# --------------------------------------------------------------------------- #
# 重跑
# --------------------------------------------------------------------------- #
async def test_reprocess_adds_a_new_analysis_row(client: httpx.AsyncClient) -> None:
    created = (await client.post("/api/ingest/manual", json=manual_payload())).json()
    content_id = created["result"]["content_id"]
    first_analysis = created["result"]["analysis_id"]

    response = await client.post(f"/api/contents/{content_id}/reprocess")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "completed"
    # 新增一行 analyses，不是覆盖旧的
    assert body["analysis_id"] != first_analysis

    detail = (await client.get(f"/api/contents/{content_id}")).json()
    assert detail["analysis_count"] == 2


async def test_reprocess_missing_is_404(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/contents/does-not-exist/reprocess")
    assert response.status_code == 404
    assert response.json()["error_type"] == ErrorType.CONTENT_NOT_FOUND.value


# --------------------------------------------------------------------------- #
# 任务恢复接口
# --------------------------------------------------------------------------- #
async def test_manual_recover_endpoint(
    db: Database,
    repo: ContentRepository,
    settings_with_vault: Settings,
    vault_root: Path,
    raw_content: RawContent,
    valid_analysis_payload: dict,
) -> None:
    normalized = normalize_content(raw_content)
    insert = ContentInsert.from_normalized(normalized, content_id="stale-003", status="processing")
    await repo.insert_content(dataclasses.replace(insert, created_at="2000-01-01T00:00:00Z"))

    app = create_app(
        settings=settings_with_vault,
        database=db,
        provider=MockProvider(payload=valid_analysis_payload),
        vault_root=vault_root,
        configure_logging=False,
        run_startup_recovery=False,
    )
    async with running(app) as client:
        response = await client.post("/api/tasks/recover")

    assert response.status_code == 200
    assert response.json()["resumed"] == ["stale-003"]


# --------------------------------------------------------------------------- #
# 未预期异常
# --------------------------------------------------------------------------- #
async def test_unexpected_exception_is_500_not_swallowed(
    api_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """定稿禁止 ``except: pass`` —— 未预期异常必须变成可见的 500。

    断言 ``error_type`` 而不只是状态码：Starlette 自带的 500 响应 body 是
    ``{"detail": "Internal Server Error"}``，没有 ``error_type`` ——
    这样就能区分「我们的 handler 生效了」和「框架兜底了」。
    """

    async def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(api_app.state.container.pipeline, "process", boom)

    async with running(api_app, raise_app_exceptions=False) as client:
        response = await client.post("/api/ingest/manual", json=manual_payload())

    assert response.status_code == 500
    assert response.json()["error_type"] == ErrorType.UNKNOWN_ERROR.value


# --------------------------------------------------------------------------- #
# OpenAPI（第二十八节：前端从这里生成 TS 类型）
# --------------------------------------------------------------------------- #
def test_openapi_schema_is_exportable(api_app: FastAPI) -> None:
    spec = api_app.openapi()
    assert spec["info"]["version"] == "0.3.3"

    paths = spec["paths"]
    for expected in (
        "/api/health",
        "/api/contents",
        "/api/contents/{content_id}",
        "/api/contents/{content_id}/reprocess",
        "/api/ingest/manual",
        "/api/ingest/douyin",
        "/api/ingest/media",
        "/api/entities",
        "/api/entities/merge",
        "/api/tasks/recovery",
        "/api/tasks/recover",
    ):
        assert expected in paths, f"OpenAPI 里少了 {expected}"

    # openapi-typescript 靠 title 命名类型；没有 title 会退化成匿名结构。
    for name, schema in spec["components"]["schemas"].items():
        assert schema.get("title"), f"schema {name} 没有 title"

    for path, operations in paths.items():
        for method, operation in operations.items():
            assert operation.get("operationId"), f"{method.upper()} {path} 没有 operationId"


def test_every_error_type_has_a_status_code() -> None:
    """新增 ``error_type`` 忘了登记状态码时，这条会红。

    刻意用「全覆盖」而不是「抽查」：``status_for_error_type`` 的兜底值
    只在真的查不到时生效，不该成为常规路径。
    """
    missing = [member.value for member in ErrorType if member not in STATUS_BY_ERROR_TYPE]
    assert missing == [], f"这些 error_type 没映射 HTTP 状态码：{missing}"


# --------------------------------------------------------------------------- #
# CORS：只放行 Chrome 扩展（Phase 10）
# --------------------------------------------------------------------------- #
#: Chrome 扩展 id = 32 个 a-p 字母（不是任意字符串）。
EXTENSION_ORIGIN = "chrome-extension://" + "abcdefghijklmnopabcdefghijklmnop"
#: 长得像扩展来源，但字符集不合法（含 a-p 之外的字母）。
FAKE_EXTENSION_ORIGIN = "chrome-extension://evil-origin-not-a-real-id-xx"
WEB_ORIGIN = "https://evil.example.com"


async def test_extension_origin_gets_cors_headers(client: httpx.AsyncClient) -> None:
    """扩展 popup 的跨域请求必须拿到 CORS 头。

    没有它时浏览器会在拿到响应前就把结果丢掉 —— 服务端日志是 200，
    扩展里却是「网络错误」，这种错位很难查。
    """
    response = await client.get("/api/health", headers={"Origin": EXTENSION_ORIGIN})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == EXTENSION_ORIGIN


async def test_extension_origin_can_post(client: httpx.AsyncClient) -> None:
    """采集接口（POST + Content-Type）也要过 CORS。"""
    response = await client.post(
        "/api/ingest/manual", json=manual_payload(), headers={"Origin": EXTENSION_ORIGIN}
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == EXTENSION_ORIGIN


@pytest.mark.parametrize(
    "origin",
    [WEB_ORIGIN, FAKE_EXTENSION_ORIGIN, "http://127.0.0.1:9999", "null"],
)
async def test_other_origins_are_not_allowed(
    client: httpx.AsyncClient, origin: str
) -> None:
    """**只**放行扩展：任意网页来源一律不给 CORS 头。

    写 ``allow_origins=["*"]`` 图省事的话，本机跑着的任意网页都能调
    「写你 Obsidian 库 / 用你 LLM 额度」的接口 —— 这条测试就是防它的。
    """
    response = await client.get("/api/health", headers={"Origin": origin})
    assert response.status_code == 200  # 请求本身照常处理
    assert "access-control-allow-origin" not in response.headers


async def test_preflight_from_extension_is_allowed(client: httpx.AsyncClient) -> None:
    """带 Content-Type 的 POST 会先发 OPTIONS 预检 —— 预检不过就根本没有真正的请求。"""
    response = await client.options(
        "/api/ingest/douyin",
        headers={
            "Origin": EXTENSION_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type",
        },
    )
    assert response.status_code == 200
    assert "POST" in response.headers["access-control-allow-methods"]
    assert "Content-Type" in response.headers["access-control-allow-headers"]


def _cors_options(app: FastAPI) -> dict:
    """从**真实中间件配置**里取 CORS 选项 —— 不做源码子串匹配。

    本项目已经三次栽在「检查器把自己的文档当代码」上（禁词写在注释里，
    检查器在注释里找到它就判违规 / 放行）。所以这里查的是 Starlette
    真正装上去的那个中间件的入参，注释怎么写都不影响判定。
    """
    from starlette.middleware.cors import CORSMiddleware

    for middleware in app.user_middleware:
        if middleware.cls is CORSMiddleware:
            return dict(middleware.kwargs)
    raise AssertionError("没有装 CORSMiddleware —— 扩展会连不上")


def test_cors_never_allows_any_origin(api_app: FastAPI) -> None:
    """``allow_origins`` 必须是空 —— 放行任意来源等于把本地库开放给任意网页。"""
    options = _cors_options(api_app)
    assert not options.get("allow_origins")
    assert options.get("allow_origin_regex") == EXTENSION_ORIGIN_PATTERN


def test_cors_regex_rejects_non_extension_origins() -> None:
    """正则要真的挡得住：任意网页 / 假扩展 id / 通配，一个都不能过。"""
    pattern = re.compile(EXTENSION_ORIGIN_PATTERN)
    assert pattern.match("chrome-extension://" + "a" * 32)
    for origin in (
        "https://evil.example.com",
        "http://127.0.0.1:9999",
        "chrome-extension://",
        "chrome-extension://" + "a" * 31,
        "chrome-extension://" + "a" * 33,
        "chrome-extension://" + "a" * 31 + "q",
        "*",
    ):
        assert pattern.match(origin) is None, f"不该放行 {origin}"


async def test_cors_can_be_turned_off(api_app: FastAPI) -> None:
    """``enable_extension_cors=False`` 时不装中间件（纯本机网页 UI 场景）。"""
    from app.api import create_app

    app = create_app(
        settings=api_app.state.container.settings,
        database=api_app.state.container.database,
        provider=api_app.state.container.provider,
        vault_root=api_app.state.container.pipeline.vault_root,
        configure_logging=False,
        run_startup_recovery=False,
        enable_extension_cors=False,
    )
    with pytest.raises(AssertionError):
        _cors_options(app)
