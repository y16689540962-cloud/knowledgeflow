"""``create_app()`` —— FastAPI 应用工厂（Phase 8）。

设计要点：

* **工厂而不是单例**：所有依赖显式传入。测试因此能用「临时库 + Mock LLM +
  临时 vault」起一个完整应用，不碰真实 ``.env``。
* **lifespan 里做两件事**：建表（仅当 database 是自己建的）+ 启动恢复。
  启动恢复**失败也不阻止应用起来**（报告留在 ``/api/tasks/recovery``）。
* **不接 BackgroundTasks**：写入接口同步 await。
* **CORS 只放行 Chrome 扩展**（Phase 10）：``chrome-extension://<id>`` 一种来源，
  **绝不放 ``*``**。``<id>`` 在「加载已解压的扩展」时会变，所以只能按前缀正则匹配。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Final

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.container import (
    AppContainer,
    DouyinSourceFactory,
    build_container,
    # 必须起别名：create_app 有个同名 bool 参数，直接 import 会被参数名遮住，
    # lifespan 里就变成「调用一个 bool」。
    run_startup_recovery as execute_startup_recovery,
)
from app.api.errors import ErrorResponse, install_exception_handlers
from app.api.routes import router
from app.capabilities.service import MediaCapabilityService
from app.config import Settings
from app.db.session import Database
from app.logging_config import log_event, setup_logging
from app.providers.base import LLMProvider

API_TITLE = "KnowledgeFlow API"
API_VERSION = "0.3.3"

#: Chrome 扩展的来源形如 ``chrome-extension://<32 个 a-p 字母>``。
#: **只**放行这一种 —— 未打包扩展的 id 每次加载都可能变，所以不能写死，
#: 只能按前缀 + 字符集匹配。**绝不用 ``*``**：那等于把本地库开放给任意网页。
EXTENSION_ORIGIN_PATTERN: Final[str] = r"^chrome-extension://[a-p]{32}$"
#: 扩展只需要这两个方法：探活 GET + 采集 POST。
CORS_ALLOWED_METHODS: Final[list[str]] = ["GET", "POST", "OPTIONS"]
CORS_ALLOWED_HEADERS: Final[list[str]] = ["Content-Type"]

#: 零构建静态页目录。``parents[1]`` 是 ``app/`` —— 刻意放在 API 包**外面**，
#: 让静态资源和不相关的 Python 模块分开。
WEB_DIR = Path(__file__).resolve().parents[1] / "web"

TAGS_METADATA: list[dict[str, str]] = [
    {"name": "meta", "description": "健康检查与元信息（不回显任何密钥 / 本机路径）"},
    {"name": "contents", "description": "内容只读查询与重跑"},
    {"name": "ingest", "description": "采集入口：手动粘贴（零网络）/ 抖音 URL / 本地媒体文件"},
    {"name": "entities", "description": "实体归一化与合并（定稿第二十一节）：冲突不抢不改"},
    {"name": "tasks", "description": "任务状态恢复（定稿第十七节）"},
]


def create_app(
    *,
    settings: Settings | None = None,
    database: Database | None = None,
    provider: LLMProvider | None = None,
    vault_root: str | Path | None = None,
    run_startup_recovery: bool = True,
    configure_logging: bool = True,
    logger: logging.Logger | None = None,
    douyin_source_factory: DouyinSourceFactory | None = None,
    media_capability_service: MediaCapabilityService | None = None,
    mount_web: bool = True,
    enable_extension_cors: bool = True,
    title: str = API_TITLE,
    version: str = API_VERSION,
) -> FastAPI:
    """构造一个 KnowledgeFlow API 应用。

    Args:
        settings: 不传则读 ``.env``（生产路径）。测试请显式传。
        database: 不传则按 settings 新建，且由 lifespan 负责建表 / 关闭。
        provider: 不传则按 settings 构造**真实** LLM 提供方
            （缺 API Key 时立刻抛 ``CONFIG_LLM_API_KEY_MISSING``）。
            测试请传 :class:`~app.providers.mock.MockProvider`。
        vault_root: 覆盖 ``OBSIDIAN_VAULT_PATH``。
        run_startup_recovery: 关闭后 lifespan 不跑恢复（测试可以精确控制时机）。
        configure_logging: 关闭后不安装全局 handler（避免测试里互相干扰）。
        mount_web: 关闭后不挂载 ``/`` 的静态页（只留纯 JSON API）。
        enable_extension_cors: 放行 Chrome 扩展的跨域请求（Phase 10）。
            只认 ``chrome-extension://<id>`` 这一种来源；关掉后扩展连不上。
        media_capability_service: 本地媒体入口的 ASR/OCR 编排。
            不传 = Null provider（引擎没装就安静降级，不报错）。
    """
    resolved_settings = settings  # 可能为 None —— 由 build_container 兜底读取
    resolved_logger = logger or logging.getLogger("knowledgeflow.api")

    if configure_logging and resolved_settings is not None:
        setup_logging(
            level=resolved_settings.log_level,
            verbose_content=resolved_settings.content_logging_enabled,
        )

    container = build_container(
        settings=resolved_settings,
        database=database,
        provider=provider,
        vault_root=vault_root,
        logger=resolved_logger,
        douyin_source_factory=douyin_source_factory,
        media_capability_service=media_capability_service,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if container.owns_database:
            await container.database.create_all()
        if run_startup_recovery:
            # 第十七节：服务启动时把卡住的任务捞回来。
            # 它自己吞掉恢复流程的异常 —— 应用照常起来。
            await execute_startup_recovery(container)
        try:
            yield
        finally:
            if container.owns_database:
                await container.database.dispose()

    app = FastAPI(
        title=title,
        version=version,
        description=(
            "KnowledgeFlow —— 互联网碎片信息 → 结构化知识资产 → Obsidian。\n\n"
            "前端不手写 API 类型：用 `openapi-typescript` 从 `/openapi.json` 生成"
            "（定稿第二十八节）。"
        ),
        lifespan=lifespan,
        openapi_tags=TAGS_METADATA,
    )
    app.state.container = container
    if enable_extension_cors:
        install_extension_cors(app)
    app.include_router(router)
    install_exception_handlers(app, logger=resolved_logger)

    if mount_web:
        mount_web_ui(app, logger=resolved_logger)
    return app


def install_extension_cors(app: FastAPI) -> None:
    """放行 **Chrome 扩展**的跨域请求（Phase 10）。

    为什么必须有：扩展的 popup 运行在 ``chrome-extension://<id>`` 源下，
    它 fetch ``http://127.0.0.1:8000/api/...`` 是跨域的。没有 CORS 头，
    浏览器会在拿到响应前就把结果丢掉 —— 服务端日志里看到的是**成功的 200**，
    扩展里却是「网络错误」，这种错位很难查。

    为什么不能图省事写 ``allow_origins=["*"]``：
    那等于把「能写你 Obsidian 库、能用你 LLM 额度」的接口开放给**任意网页**
    （本机跑着的恶意页面也能直接调）。所以这里只认扩展那一种来源。

    边界要诚实说明：这拦不住「用户自己装的另一个扩展」——
    任何已安装的 Chrome 扩展都满足这个来源格式。服务只绑 127.0.0.1
    （``--expose`` 守卫），把这个面缩到「本机能跑的东西」。
    """
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=EXTENSION_ORIGIN_PATTERN,
        allow_methods=CORS_ALLOWED_METHODS,
        allow_headers=CORS_ALLOWED_HEADERS,
        allow_credentials=False,
        max_age=600,
    )


def mount_web_ui(app: FastAPI, *, logger: logging.Logger | None = None) -> bool:
    """把零构建静态页挂到 ``/``。

    **必须在所有 API 路由之后 mount**：Starlette 按 ``app.routes`` 顺序匹配，
    挂早了 ``/`` 这个前缀会把 ``/api/*``、``/docs``、``/openapi.json`` 全吃掉。

    目录不存在时**静默跳过**而不是崩 —— 「少了静态资源」不该让后端起不来。
    """
    if not WEB_DIR.is_dir():
        log = logger or logging.getLogger("knowledgeflow.api")
        log_event(
            log,
            logging.WARNING,
            stage="web",
            message="静态页目录不存在，跳过挂载（后端接口照常可用）",
            path=str(WEB_DIR),
        )
        return False

    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
    return True


__all__ = [
    "create_app",
    "mount_web_ui",
    "install_extension_cors",
    "WEB_DIR",
    "API_TITLE",
    "API_VERSION",
    "TAGS_METADATA",
    "EXTENSION_ORIGIN_PATTERN",
    "CORS_ALLOWED_METHODS",
    "CORS_ALLOWED_HEADERS",
    "ErrorResponse",
    "AppContainer",
]
