#!/usr/bin/env python
"""Phase 7 能力验收：`python scripts/demo_capabilities.py --file <音频/图片>`

用途：拿**真实文件**验一次「媒体 → transcript / ocr_text」，
加 `--process` 可以继续走完整 A 线（把转写结果写进笔记）。

合规与降级（定稿第二、二十二、二十七节）：

* ASR / OCR 是**可选能力**。引擎没装就如实打印 ``unavailable`` 并降级，
  **绝不用空字符串冒充「转写完成」**，也不阻塞 Core Pipeline。
* 引擎懒加载：不装 faster-whisper 也能跑这个脚本（只是转不了）。
* ``--url`` 会真的联网下载；``--file`` 走本地文件，零网络。

退出码：0 = 成功 / 已降级  2 = 内容处理失败  3 = 参数错误
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

from app.capabilities import MediaCapabilityService  # noqa: E402
from app.capabilities.base import CapabilityAttempt  # noqa: E402
from app.capabilities.whisper import FasterWhisperASR  # noqa: E402
from app.capabilities.tesseract import TesseractOCR  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db.session import Database  # noqa: E402
from app.ingestion import ManualPastePayload, ManualPasteSource  # noqa: E402
from app.logging_config import setup_logging  # noqa: E402
from app.media import HttpMediaDownloader  # noqa: E402
from app.pipeline.service import ProcessingPipeline  # noqa: E402
from app.providers import MockProvider  # noqa: E402
from app.schemas import RawContent  # noqa: E402
from app.testing import load_fixture_json  # noqa: E402

EXIT_OK = 0
EXIT_FAILED = 2
EXIT_USAGE = 3

MEDIA_EXT_BY_TYPE = {
    "image": (".png", ".jpg", ".jpeg", ".webp", ".gif"),
    "video": (".mp4", ".mov", ".webm"),
    "audio": (".mp3", ".wav", ".m4a", ".aac"),
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="KnowledgeFlow Phase 7 验收：媒体 → transcript / ocr_text"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path, help="本地媒体文件（零网络）")
    source.add_argument("--url", help="远端媒体 URL（会真的联网下载）")
    parser.add_argument("--model", default="tiny", help="whisper 模型尺寸（默认 tiny）")
    parser.add_argument("--lang", default="chi_sim+eng", help="tesseract 语言（默认 chi_sim+eng）")
    parser.add_argument("--no-asr", action="store_true", help="不启用 ASR（验证降级路径）")
    parser.add_argument("--no-ocr", action="store_true", help="不启用 OCR（验证降级路径）")
    parser.add_argument("--process", action="store_true", help="补完后继续跑完整 A 线（Mock LLM）")
    parser.add_argument("--vault", type=Path, default=None, help="--process 时的 vault 根目录")
    parser.add_argument("--keep-dir", type=Path, default=None, help="--url 时的下载目录")
    return parser.parse_args(argv)


def guess_media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    for media_type, extensions in MEDIA_EXT_BY_TYPE.items():
        if suffix in extensions:
            return media_type
    raise SystemExit(f"认不出 {suffix} 属于哪种媒体（用 --file 传 png/jpg/mp4/mp3/wav 等）")  # noqa: B904


def print_attempts(attempts: tuple[CapabilityAttempt, ...]) -> None:
    print("\n  能力流水：")
    for item in attempts:
        mark = {"ok": "✓", "skipped": "−", "unavailable": "∅", "failed": "✗"}[item.status]
        detail = item.message or ""
        if item.error_type:
            detail = f"[{item.error_type}] {detail}"
        if item.text_length:
            detail = f"{detail}（{item.text_length} 字）"
        print(f"    {mark} {item.capability:9s} {item.provider:18s} {detail}")


async def process(raw: RawContent, vault_root: Path) -> int:
    database = Database.create(f"sqlite:///{vault_root.parent / 'capability.db'}")
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

    print(f"\n  A 线结果：outcome={result.outcome} failed_step={result.failed_step}")
    if result.note_path:
        print(f"  笔记：{vault_root / result.note_path}")
    return EXIT_OK if result.success else EXIT_FAILED


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(level="WARNING")

    asr = None if args.no_asr else FasterWhisperASR(model_size=args.model)
    ocr = None if args.no_ocr else TesseractOCR(lang=args.lang)

    print(f"  ASR provider = {asr.name if asr else '（关闭）'}"
          + (f"  可用={asr.is_available()}" if asr else ""))
    print(f"  OCR provider = {ocr.name if ocr else '（关闭）'}"
          + (f"  可用={ocr.is_available()}" if ocr else ""))

    downloader = None
    media_url = None
    if args.url:
        keep = args.keep_dir or Path(tempfile.mkdtemp(prefix="knowledgeflow-media-"))
        downloader = HttpMediaDownloader(keep)
        media_url = args.url
        media_type = "video"  # 下载前未知，先按视频；不匹配会被下载器明确拒绝
        print(f"  下载目录：{keep}")
    else:
        if not args.file.is_file():
            print(f"[参数错误] 文件不存在：{args.file}")
            return EXIT_USAGE
        media_type = guess_media_type(args.file)
        print(f"  本地文件：{args.file}（{media_type}）")

    raw = await ManualPasteSource().fetch(
        ManualPastePayload(
            title=f"Phase7 验收 {args.file.name if args.file else args.url}",
            raw_text="（由 capabilities 脚本构造，正文来自转写 / OCR）",
            media_type=media_type,  # type: ignore[arg-type]
        )
    )
    if args.file:
        raw = raw.model_copy(update={"media_path": str(args.file)})

    service = MediaCapabilityService(downloader=downloader, asr=asr, ocr=ocr)
    enriched, attempts = await service.enrich(raw, media_url=media_url)
    print_attempts(attempts)

    print(f"\n  transcript = {(enriched.transcript or '（无）')[:80]}")
    print(f"  ocr_text   = {(enriched.ocr_text or '（无）')[:80]}")

    if not args.process:
        print("\n（加 --process 可以继续跑完整 A 线）")
        return EXIT_OK

    vault_root = (args.vault or Path(tempfile.mkdtemp(prefix="knowledgeflow-cap-")) / "vault").resolve()
    vault_root.mkdir(parents=True, exist_ok=True)
    return await process(enriched, vault_root)


if __name__ == "__main__":  # pragma: no cover - 脚本入口
    raise SystemExit(asyncio.run(main()))
