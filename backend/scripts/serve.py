#!/usr/bin/env python
"""启动 KnowledgeFlow API 服务：`python scripts/serve.py --port 8000`

默认走**真实** LLM（读 ``.env``）。缺 API Key 时在启动那一刻就报
``CONFIG_LLM_API_KEY_MISSING`` —— 与其让服务起来后每个请求都 503。

``--mock`` 用 fixture 假输出跑起来（零网络、零 token），用于本地冒烟。

**默认只绑 ``127.0.0.1``**：API 没有鉴权，绑到 ``0.0.0.0`` 等于把「读全库 +
触发 reprocess（会写 vault）」送给任何能访问到该地址的人。所以非本机地址必须
显式加 ``--expose``，否则直接拒绝启动（退出码 3）—— 这条不能靠人记住。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:  # pragma: no cover - 直接执行脚本时用
    sys.path.insert(0, str(BACKEND_ROOT))

from app.api import create_app  # noqa: E402
from app.config import Settings, load_settings  # noqa: E402
from app.errors import KnowledgeFlowError  # noqa: E402
from app.logging_config import setup_logging  # noqa: E402
from app.providers import MockProvider  # noqa: E402
from app.testing import load_fixture_json  # noqa: E402

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_EXPOSE_REFUSED = 3

#: 只认这些为「本机回环」。其它一切地址（含 ``0.0.0.0``）都算对外暴露。
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})

#: API 无鉴权 —— 暴露出去等于把「读库 + 触发 reprocess（会写 vault）」送人。
EXPOSE_WARNING: str = (
    "API 无鉴权：任何能访问该地址的人都能读取全部内容、触发 reprocess 写入你的 Obsidian vault。"
)


def is_loopback(host: str) -> bool:
    """``--host`` 是否只监听本机。``0.0.0.0`` 监听所有网卡 → **不是**。"""
    return host.strip().lower() in LOOPBACK_HOSTS


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="KnowledgeFlow API 服务")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认只本机）")
    parser.add_argument(
        "--expose",
        action="store_true",
        help="确认要绑到非本机地址（API 无鉴权，风险自负）；不带的话直接拒绝启动",
    )
    parser.add_argument("--port", type=int, default=8000, help="监听端口")
    parser.add_argument("--vault", type=Path, default=None, help="覆盖 OBSIDIAN_VAULT_PATH")
    parser.add_argument(
        "--mock", action="store_true", help="用 fixture 假 LLM 输出（零网络、零 token）"
    )
    parser.add_argument(
        "--no-recovery", action="store_true", help="启动时不跑任务恢复（第十七节默认会跑）"
    )
    parser.add_argument("--reload", action="store_true", help="开发模式：代码改动自动重载")
    parser.add_argument("--no-web", action="store_true", help="不挂载静态页，只提供 JSON API")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # 先拦「无鉴权 + 对外暴露」这个组合 —— 它不该靠用户记住。
    if not is_loopback(args.host) and not args.expose:
        print(f"[拒绝启动] --host {args.host} 会监听非本机地址。", file=sys.stderr)
        print(f"  {EXPOSE_WARNING}", file=sys.stderr)
        print("  确认要这么做就加 --expose（比如受信任的局域网）。", file=sys.stderr)
        return EXIT_EXPOSE_REFUSED

    settings = load_settings()

    setup_logging(
        level=settings.log_level, verbose_content=settings.content_logging_enabled
    )

    provider = (
        MockProvider(payload=load_fixture_json("llm_analysis_valid.json")) if args.mock else None
    )

    # 本地媒体入口的能力编排：真实引擎（懒加载，没装就自动降级为 Null）。
    from app.capabilities.service import MediaCapabilityService
    from app.capabilities.tesseract import TesseractOCR
    from app.capabilities.whisper import FasterWhisperASR

    capabilities = MediaCapabilityService(
        asr=FasterWhisperASR(),
        ocr=TesseractOCR(),
    )

    try:
        app = create_app(
            settings=settings,
            provider=provider,
            vault_root=args.vault,
            run_startup_recovery=not args.no_recovery,
            configure_logging=False,  # 上面已经装好了，别装两遍
            mount_web=not args.no_web,
            media_capability_service=capabilities,
        )
    except KnowledgeFlowError as exc:
        print(f"[启动失败] error_type={exc.error_type_value}", file=sys.stderr)
        print(f"  {exc.message}", file=sys.stderr)
        if exc.error_type_value == "CONFIG_LLM_API_KEY_MISSING":
            print(
                "\n  补上 .env 里的 API Key（照 backend/.env.example 填），"
                "或者加 --mock 用假输出先跑通链路。",
                file=sys.stderr,
            )
        return EXIT_CONFIG

    import uvicorn

    base = f"http://{args.host}:{args.port}"
    if not is_loopback(args.host):
        print(f"[警告] 正在监听 {args.host} —— {EXPOSE_WARNING}")
    print(f"KnowledgeFlow  →  {base}/")
    print(f"  UI           = {base}/           （零构建原生静态页）")
    print(f"  接口文档     = {base}/docs")
    print(f"  LLM provider = {app.state.container.provider.name}")
    print(f"  vault        = {app.state.container.settings.obsidian_vault or '（未配置）'}")
    print(
        f"  ASR / OCR    = {capabilities.asr_name} / {capabilities.ocr_name}"
        "（本地媒体入口；引擎没装会自动降级）"
    )
    # 只说「配了没有」——Cookie 的值永不打印（第二十四条 / 第二十九条）。
    print(
        "  抖音登录态   = "
        + ("已配置（DOUYIN_COOKIE，值不回显）" if settings.douyin_cookie.strip() else "匿名（未配置，大概率撞验证页）")
    )
    if args.no_web:
        print("  （--no-web：只提供 JSON API，静态页未挂载）")
    uvicorn.run(app, host=args.host, port=args.port, log_level=settings.log_level.lower())
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - 脚本入口
    raise SystemExit(main())
