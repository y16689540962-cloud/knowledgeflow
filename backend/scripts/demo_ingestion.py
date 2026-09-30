#!/usr/bin/env python
"""B 线（Ingestion）验收脚本：`python scripts/demo_ingestion.py --url <抖音链接>`

用途：拿**真实链接**验一次「抖音 URL → aweme_id → RawContent」。
默认只做采集并把结果打出来；加 `--process` 会继续走完整 A 线（写进临时 vault）。

元数据来源（实测，2026-09-30 起）：
抖音页面只吐 JS 壳页，服务端**不再**内嵌数据，所以只能走详情接口
`/aweme/v1/web/aweme/detail/`——它**需要登录态**，因此本脚本默认读 `.env` 的
`DOUYIN_COOKIE`（也可以用 `--cookie` 临时覆盖）。

合规（定稿第二十九条）：只用**你自己**的合法登录态做公开数据的 GET，
不做签名伪造、不绕验证码、不窃取他人 Cookie。被挡住时如实报 `error_type`。

也可以用 `--manual-title/--manual-text` 验手动粘贴这条降级入口（零网络）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:  # pragma: no cover - 直接执行脚本时用
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import Settings  # noqa: E402
from app.db.session import Database  # noqa: E402
from app.errors import KnowledgeFlowError  # noqa: E402
from app.ingestion import DouyinSource, ManualPastePayload, ManualPasteSource  # noqa: E402
from app.ingestion.redirects import ResolvedURL  # noqa: E402
from app.logging_config import setup_logging  # noqa: E402
from app.normalization import normalize_content  # noqa: E402
from app.pipeline.service import ProcessingPipeline  # noqa: E402
from app.providers import MockProvider  # noqa: E402
from app.schemas import RawContent  # noqa: E402

EXIT_OK = 0
EXIT_BLOCKED = 2
EXIT_USAGE = 3


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="KnowledgeFlow B 线验收：抖音 URL / 手动粘贴 → RawContent"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", help="抖音视频 URL（长链或 v.douyin.com 短链）")
    source.add_argument("--manual-text", help="改用手动粘贴（零网络）")
    parser.add_argument("--manual-title", default=None, help="手动粘贴时的标题")
    parser.add_argument("--timeout", type=int, default=15, help="请求超时（秒）")
    parser.add_argument(
        "--cookie",
        default=None,
        help=(
            "你**自己**的抖音登录态（形如 'sessionid=...; ttdid=...'），"
            "用于提高成功率（第二十九条允许）。不传 = 匿名，大概率撞验证页"
        ),
    )
    parser.add_argument("--process", action="store_true", help="继续跑完整 A 线（Mock LLM）")
    parser.add_argument("--vault", type=Path, default=None, help="--process 时的 vault 根目录")
    return parser.parse_args(argv)


def print_raw(raw: RawContent) -> None:
    normalized = normalize_content(raw)
    print("  取得 RawContent：")
    print(f"    source        = {raw.source}")
    print(f"    source_id     = {raw.source_id or '（空，交给 normalize 填）'}")
    print(f"    source_url    = {raw.source_url}")
    print(f"    media_type    = {raw.media_type}")
    print(f"    title         = {(raw.title or '')[:70]}")
    print(f"    author        = {raw.author}（author_id={raw.author_id}）")
    print(f"    raw_text 长度 = {len(raw.raw_text or '')}")
    print(f"    transcript    = {'有' if raw.transcript else '无（等 Phase 7 的 ASR）'}")
    print(f"    归一化 source_id = {normalized.source_id}   ← 真正的判重身份（永不回退成 URL）")
    print(f"    content_hash  = {normalized.content_hash[:16]}…（v{normalized.content_hash_version}）")
    print(f"    需人工复核    = {normalized.needs_manual_review}"
          + (f"（{normalized.error_type}）" if normalized.error_type else ""))


async def fetch_douyin(url: str, timeout: int, cookie: str | None = None) -> RawContent:
    if (cookie or "").strip():
        # Cookie 是凭据：只说「带了」，绝不回显它的值。
        print("  登录态：已带上（值不打印）")
    else:
        print("  登录态：匿名（不带 Cookie）—— 抖音大概率返回验证页 / 空响应")
    async with DouyinSource(timeout_seconds=timeout, cookie=cookie) as source:
        resolved: ResolvedURL = await source.resolve(url)
        print("  短链解析：")
        for key, value in resolved.to_dict().items():
            print(f"    {key:14s}= {value}")
        metadata = await source.fetch_metadata(resolved)
        print("  元数据：")
        for key, value in metadata.to_dict().items():
            print(f"    {key:14s}= {value}")
        return source.to_raw_content(resolved, metadata)


async def process(raw: RawContent, vault_root: Path) -> int:
    from app.testing import load_fixture_json

    database = Database.create(f"sqlite:///{vault_root.parent / 'ingestion.db'}")
    await database.create_all()
    try:
        pipeline = ProcessingPipeline.from_database(
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            provider=MockProvider(payload=load_fixture_json("llm_analysis_valid.json")),
            database=database,
            vault_root=vault_root,
        )
        result = await pipeline.process(raw)
    finally:
        await database.dispose()

    print("\n  A 线结果：")
    for key, value in result.to_dict().items():
        if key != "steps":
            print(f"    {key:16s}= {value}")
    print(f"  vault: {vault_root}")
    return EXIT_OK if result.success else EXIT_BLOCKED


def _cookie_from_env() -> str | None:
    """``--cookie`` 没给时，回落到 ``.env`` 的 ``DOUYIN_COOKIE``（值永不打印）。

    这样用户填一次 ``.env`` 就不必把 Cookie 摆到命令行上（那会留在 shell 历史里）。
    """
    from app.config import Settings

    cookie = (Settings().douyin_cookie or "").strip()
    if cookie:
        print("  登录态来源：.env 的 DOUYIN_COOKIE（值不打印）")
        return cookie
    return None


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(level="WARNING")
    cookie: str | None = None

    try:
        if args.url:
            print(f"=== 抖音采集：{args.url} ===")
            cookie = args.cookie or _cookie_from_env()
            raw = await fetch_douyin(args.url, args.timeout, cookie)
        else:
            print("=== 手动粘贴 ===")
            raw = await ManualPasteSource().fetch(
                ManualPastePayload(title=args.manual_title, raw_text=args.manual_text)
            )
    except KnowledgeFlowError as exc:
        print(f"\n[采集失败] error_type={exc.error_type_value}")
        print(f"  message = {exc.message}")
        if exc.context:
            print(f"  context = {exc.context}")
        print(
            "\n  这是**预期内**的 degraded 结果（定稿第二十二条）：\n"
            "  · 不阻塞 A 线 —— 抖音挂了照样能用手动粘贴\n"
            "  · 不伪造 RawContent、不返回假的处理成功\n"
            "  · 按定稿第二十九条，不尝试绕验证码 / 伪造签名 / 借用他人登录态"
        )
        if exc.error_type_value in ("COOKIE_REQUIRED", "REQUEST_BLOCKED") and not (
            cookie or ""
        ).strip():
            print(
                "\n  下一步：在 backend/.env 里填你自己的 DOUYIN_COOKIE\n"
                "  （浏览器登录抖音后从开发者工具里取请求头里的 Cookie，整串拷过来）\n"
                "  实测：抖音页面只吐 JS 壳页，元数据只能从详情接口取，而该接口需要登录态。"
            )
        return EXIT_BLOCKED

    print_raw(raw)

    if not args.process:
        print("\n（加 --process 可以继续跑完整 A 线）")
        return EXIT_OK

    vault_root = (args.vault or Path(tempfile.mkdtemp(prefix="knowledgeflow-ingest-")) / "vault").resolve()
    vault_root.mkdir(parents=True, exist_ok=True)
    return await process(raw, vault_root)


if __name__ == "__main__":  # pragma: no cover - 脚本入口
    raise SystemExit(asyncio.run(main()))
