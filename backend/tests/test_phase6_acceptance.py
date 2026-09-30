"""Phase 6 验收：ManualPaste + Douyin Ingestion（定稿第二十二 / 二十三 / 二十六节）。

链路：

```text
Douyin URL ──resolve_short_link──→ aweme_id ──→ RawContent ──→ Core Pipeline
ManualPastePayload ────────────────────────→ RawContent ──→ Core Pipeline
```

**B 线允许 degraded，A 线必须始终可用。** 全程离线（httpx MockTransport）。
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
import yaml

from app.db.repository import ContentRepository
from app.ingestion import (
    DouyinSource,
    ManualPastePayload,
    ManualPasteSource,
    extract_aweme_id,
    resolve_short_link,
)
from app.ingestion.redirects import (
    AwemeIdNotFoundError,
    CookieRequiredError,
    NetworkTimeoutError,
    ParserUnsupportedError,
    RequestBlockedError,
    ShortLinkResolutionError,
)
from app.pipeline.service import DEDUP_BY_SOURCE_ID
from app.schemas import RawContent

AWEME_ID = "7321567890123456789"
CANONICAL = f"https://www.douyin.com/video/{AWEME_ID}"
SHORT_LINK_A = "https://v.douyin.com/aaaaaaa/"
SHORT_LINK_B = "https://v.douyin.com/bbbbbbb/"
OTHER_AWEME_ID = "7300000000000000001"
OTHER_CANONICAL = f"https://www.douyin.com/video/{OTHER_AWEME_ID}"

CAPTION = "三分钟讲清楚 AI Agent 到底是什么"

MANUAL_TEXT = (
    "中国人口正在下降，所以未来房地产一定会大涨。"
    "过去十年，中国出生人口从每年 1600 万降到不足 1000 万，这是公开的统计数据。"
    "人口少了，需求就少；但核心城市的房子依然稀缺，所以我认为一线城市的房价未来一定会涨。"
)


def aweme_payload(aweme_id: str = AWEME_ID, desc: str = CAPTION) -> dict:
    return {
        "app": {
            "aweme": {
                "detail": {
                    "aweme_id": aweme_id,
                    "desc": desc,
                    "author": {"nickname": "老张说AI", "uid": "1234567890"},
                    "video": {"cover": {"url_list": ["https://p3.example.com/c.jpg"]}},
                }
            }
        }
    }


def douyin_page(aweme_id: str = AWEME_ID, desc: str = CAPTION) -> str:
    encoded = quote(json.dumps(aweme_payload(aweme_id, desc), ensure_ascii=False))
    return f'<html><body><script id="RENDER_DATA">{encoded}</script></body></html>'


def plain_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )


def douyin_client(
    *,
    short_links: dict[str, str] | None = None,
    page: str | None = None,
    status: int = 200,
) -> httpx.AsyncClient:
    """短链 → 规范 URL；规范 URL → 给定页面。"""
    links = short_links if short_links is not None else {
        SHORT_LINK_A: CANONICAL,
        SHORT_LINK_B: CANONICAL,
    }
    html = page if page is not None else douyin_page()

    def handler(request: httpx.Request) -> httpx.Response:
        target = links.get(str(request.url))
        if target:
            return httpx.Response(302, headers={"location": target})
        return httpx.Response(status, text=html)

    return plain_client(handler)


def notes_in(vault_root: Path, kind: str = "Processed") -> list[Path]:
    directory = vault_root / "KnowledgeFlow" / kind
    return sorted(directory.glob("*.md")) if directory.is_dir() else []


#: 2026-09-30 实测：抖音页面只吐 JS 壳页，服务端不再内嵌任何数据。
JS_SHELL = "<html><body><script>var _$jsvmprt=1;</script></body></html>"
DETAIL_API_PATH = "/aweme/v1/web/aweme/detail"
TEST_COOKIE = "sessionid=test-session; ttwid=test-ttwid"


def detail_api_client(*, desc: str = CAPTION, status_code: int = 0) -> httpx.AsyncClient:
    """当前**真实**形态的抖音：页面是壳页，元数据只能从详情接口拿。"""
    api_body = json.dumps(
        {
            "status_code": status_code,
            "aweme_detail": {
                "aweme_id": AWEME_ID,
                "desc": desc,
                "author": {"nickname": "老张说AI", "uid": "1234567890"},
                "video": {"cover": {"url_list": ["https://p3.example.com/c.jpg"]}},
            },
        },
        ensure_ascii=False,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == SHORT_LINK_A:
            return httpx.Response(302, headers={"location": CANONICAL})
        if request.url.path.startswith(DETAIL_API_PATH):
            return httpx.Response(
                200,
                content=api_body.encode("utf-8"),
                headers={"content-type": "application/json; charset=utf-8"},
            )
        return httpx.Response(200, text=JS_SHELL)

    return plain_client(handler)


# --------------------------------------------------------------------------- #
# 1. ManualPaste 全链路
# --------------------------------------------------------------------------- #
async def test_manual_paste_full_chain(
    pipeline, repo: ContentRepository, vault_root: Path
) -> None:
    raw = await ManualPasteSource().fetch(
        ManualPastePayload(
            title="人口下降之后，房子还会涨吗",
            author="老王聊经济",
            description="一段把事实、观点、推论和预测混在一起讲的短视频文案。",
            raw_text=MANUAL_TEXT,
        )
    )
    assert isinstance(raw, RawContent)

    result = await pipeline.process(raw)

    assert result.outcome == "completed"
    assert result.claim_count == 6
    row = await repo.get_content(result.content_id)
    assert row is not None
    assert row.source == "manual"
    assert row.source_id.startswith("hash:")
    assert row.needs_manual_review is True
    assert len(notes_in(vault_root)) == 1


async def test_two_identical_manual_pastes_dedupe(
    pipeline, repo: ContentRepository
) -> None:
    """降级入口也参与判重：同一份粘贴内容第二次不重复处理。"""
    source = ManualPasteSource()
    payload = ManualPastePayload(title="同一段话", raw_text="正文内容在这里。")

    first = await pipeline.process(await source.fetch(payload))
    second = await pipeline.process(await source.fetch(payload))

    assert first.outcome == "completed"
    assert second.outcome == "duplicate"
    assert second.deduplicated_by == DEDUP_BY_SOURCE_ID
    assert await repo.count_contents() == 1


# --------------------------------------------------------------------------- #
# 2. Douyin 全链路（mock transport）
# --------------------------------------------------------------------------- #
async def test_douyin_full_chain(
    pipeline, repo: ContentRepository, vault_root: Path
) -> None:
    async with douyin_client() as client:
        raw = await DouyinSource(client=client).fetch(SHORT_LINK_A)

    assert raw.source == "douyin"
    assert raw.source_id == AWEME_ID  # source_id = aweme_id，不是 URL
    assert raw.source_url == CANONICAL
    # Phase 6 只做 URL → metadata：正文文本就是文案，逐字稿留给 Phase 7
    assert raw.raw_text == CAPTION
    assert raw.transcript is None

    result = await pipeline.process(raw)

    assert result.outcome == "completed"
    row = await repo.get_content(result.content_id)
    assert row is not None
    assert row.source == "douyin"
    assert row.source_id == AWEME_ID
    assert row.needs_manual_review is False

    note = notes_in(vault_root)[0]
    assert note.name == f"{CAPTION}.md"
    front = yaml.safe_load(note.read_text(encoding="utf-8").split("---\n")[1])
    assert front["source"] == "douyin"
    assert front["source_id"] == AWEME_ID  # 全数字会被 YAML 加引号，所以按值比
    assert front["source_url"] == CANONICAL


async def test_douyin_full_chain_via_detail_api(
    pipeline, repo: ContentRepository, vault_root: Path
) -> None:
    """页面已是 JS 壳页（无内嵌数据）时的真实链路：元数据来自详情接口。

    这是 2026-09-30 联网实测后补的路：服务端不再把数据渲染进 HTML，
    死守页面解析就是「永远 ``PARSER_UNSUPPORTED``」。
    """
    async with detail_api_client() as client:
        raw = await DouyinSource(client=client, cookie=TEST_COOKIE).fetch(SHORT_LINK_A)

    assert raw.source == "douyin"
    assert raw.source_id == AWEME_ID
    assert raw.title == CAPTION
    assert raw.raw_text == CAPTION  # 逐字稿仍留给 Phase 7 的 ASR

    result = await pipeline.process(raw)

    assert result.outcome == "completed"
    assert await repo.count_contents() == 1
    assert notes_in(vault_root)[0].name == f"{CAPTION}.md"


async def test_detail_api_path_is_still_deduplicated_by_aweme_id(
    pipeline, repo: ContentRepository
) -> None:
    """换了取元数据的方式，判重身份依然是 ``(source, aweme_id)``。"""
    async with detail_api_client() as client:
        source = DouyinSource(client=client, cookie=TEST_COOKIE)
        first = await source.fetch(SHORT_LINK_A)
        second = await source.fetch(SHORT_LINK_A)

    assert first.source_id == second.source_id == AWEME_ID
    assert (await pipeline.process(first)).outcome == "completed"
    assert (await pipeline.process(second)).outcome == "duplicate"
    assert await repo.count_contents() == 1


async def test_detail_api_failure_does_not_break_the_core_pipeline(
    pipeline, repo: ContentRepository, vault_root: Path
) -> None:
    """接口挂了（且页面是壳页）→ 如实报错、库里一条不留、A 线照跑。"""
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == SHORT_LINK_A:
            return httpx.Response(302, headers={"location": CANONICAL})
        if request.url.path.startswith(DETAIL_API_PATH):
            state["n"] += 1
            return httpx.Response(200, content=b"", request=request)
        return httpx.Response(200, text=JS_SHELL, request=request)

    async with plain_client(handler) as client:
        with pytest.raises(CookieRequiredError):
            await DouyinSource(client=client).fetch(SHORT_LINK_A)

    assert state["n"] == 1, "匿名时应当试过详情接口"
    assert await repo.count_contents() == 0

    manual = await ManualPasteSource().fetch(
        ManualPastePayload(title="接口挂了也能用", raw_text="手贴的内容照样能被分析。")
    )
    result = await pipeline.process(manual)
    assert result.outcome == "completed"
    assert len(notes_in(vault_root)) == 1


async def test_two_short_links_to_same_video_dedupe(
    pipeline, repo: ContentRepository
) -> None:
    """**第五节的核心用途**：短链每次分享可能不同，却指向同一视频。"""
    assert SHORT_LINK_A != SHORT_LINK_B

    async with douyin_client() as client:
        source = DouyinSource(client=client)
        first_raw = await source.fetch(SHORT_LINK_A)
        second_raw = await source.fetch(SHORT_LINK_B)

    assert first_raw.source_url == second_raw.source_url == CANONICAL
    assert first_raw.source_id == second_raw.source_id == AWEME_ID

    first = await pipeline.process(first_raw)
    second = await pipeline.process(second_raw)

    assert first.outcome == "completed"
    assert second.outcome == "duplicate"
    assert second.deduplicated_by == DEDUP_BY_SOURCE_ID
    assert await repo.count_contents() == 1


async def test_two_different_videos_are_not_deduped(
    pipeline, repo: ContentRepository
) -> None:
    async with douyin_client() as client:
        first_raw = await DouyinSource(client=client).fetch(SHORT_LINK_A)

    other_page = douyin_page(OTHER_AWEME_ID, "完全另一条视频的文案")
    async with douyin_client(
        short_links={SHORT_LINK_B: OTHER_CANONICAL}, page=other_page
    ) as client:
        second_raw = await DouyinSource(client=client).fetch(SHORT_LINK_B)

    first = await pipeline.process(first_raw)
    second = await pipeline.process(second_raw)

    assert first.outcome == "completed"
    assert second.outcome == "completed"
    assert second.content_id != first.content_id
    assert await repo.count_contents() == 2


async def test_manual_and_douyin_are_not_merged(
    pipeline, repo: ContentRepository
) -> None:
    """跨来源不合并（Q1 已定）：同一段文案手动粘贴 + 抖音采集 = 两条内容。"""
    async with douyin_client() as client:
        douyin_raw = await DouyinSource(client=client).fetch(SHORT_LINK_A)

    manual_raw = RawContent(
        source="manual",
        source_id="",
        source_url="",
        title=CAPTION,
        author="老张说AI",
        media_type="text",
        raw_text="人工智能正在改变这个行业。",
    )

    first = await pipeline.process(douyin_raw)
    second = await pipeline.process(manual_raw)

    assert first.outcome == "completed"
    assert second.outcome == "completed"
    assert second.content_id != first.content_id
    assert await repo.count_contents() == 2


# --------------------------------------------------------------------------- #
# 3. B 线 degraded 不得影响 A 线
# --------------------------------------------------------------------------- #
async def test_ingestion_failure_does_not_touch_the_database(
    repo: ContentRepository,
) -> None:
    """采集失败时**一条行都不该落** —— 不得伪造 RawContent、不得返回假的成功。"""
    page = "<html>请完成验证</html>"
    async with douyin_client(page=page) as client:
        with pytest.raises(RequestBlockedError):
            await DouyinSource(client=client).fetch(SHORT_LINK_A)

    assert await repo.count_contents() == 0


async def test_core_pipeline_still_works_after_ingestion_failure(
    pipeline, repo: ContentRepository, vault_root: Path
) -> None:
    """抖音挂了，A 线照常跑（定稿第二节第 2 条）。"""
    async with douyin_client(status=403) as client:
        with pytest.raises((RequestBlockedError, ShortLinkResolutionError)):
            await DouyinSource(client=client).fetch(SHORT_LINK_A)

    raw = await ManualPasteSource().fetch(
        ManualPastePayload(title="抖音挂了也能用", raw_text="手贴的内容，照样能被分析。")
    )
    result = await pipeline.process(raw)

    assert result.outcome == "completed"
    assert await repo.count_contents() == 1
    assert len(notes_in(vault_root)) == 1


def test_pipeline_does_not_depend_on_ingestion() -> None:
    """依赖方向：A 线不许依赖 B 线（否则 B 线一挂 A 线就挂）。"""
    import inspect

    from app.pipeline import service as pipeline_service

    assert "app.ingestion" not in inspect.getsource(pipeline_service)


# --------------------------------------------------------------------------- #
# 4. 六个 B 线 error_type 都要能被真的触发
# --------------------------------------------------------------------------- #
async def test_blocked_status_error_type() -> None:
    async with plain_client(lambda request: httpx.Response(403, text="denied")) as client:
        with pytest.raises(RequestBlockedError) as info:
            await resolve_short_link(SHORT_LINK_A, client=client)
    assert info.value.error_type_value == "REQUEST_BLOCKED"


async def test_login_redirect_error_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302, headers={"location": "https://passport.douyin.com/login"}
        )

    async with plain_client(handler) as client:
        with pytest.raises(CookieRequiredError) as info:
            await resolve_short_link(SHORT_LINK_A, client=client)
    assert info.value.error_type_value == "COOKIE_REQUIRED"


async def test_missing_aweme_id_error_type() -> None:
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(
                302, headers={"location": "https://www.douyin.com/user/MS4w"}
            )
        return httpx.Response(200, text="<html>user page</html>")

    async with plain_client(handler) as client:
        with pytest.raises(AwemeIdNotFoundError) as info:
            await DouyinSource(client=client).fetch(SHORT_LINK_A)
    assert info.value.error_type_value == "AWEME_ID_NOT_FOUND"


async def test_unsupported_platform_error_type() -> None:
    async with plain_client(lambda request: httpx.Response(200, text="ok")) as client:
        with pytest.raises(ParserUnsupportedError) as info:
            await DouyinSource(client=client).fetch("https://www.bilibili.com/video/BV1x")
    assert info.value.error_type_value == "PARSER_UNSUPPORTED"


async def test_timeout_error_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    async with plain_client(handler) as client:
        with pytest.raises(NetworkTimeoutError) as info:
            await resolve_short_link(SHORT_LINK_A, client=client)
    assert info.value.error_type_value == "NETWORK_TIMEOUT"


async def test_too_many_redirects_error_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": SHORT_LINK_A})

    async with plain_client(handler) as client:
        with pytest.raises(ShortLinkResolutionError) as info:
            await resolve_short_link(SHORT_LINK_A, client=client, max_redirects=2)
    assert info.value.error_type_value == "SHORT_LINK_RESOLUTION_FAILED"


# --------------------------------------------------------------------------- #
# 5. content_hash 没有区分度时不许判重
# --------------------------------------------------------------------------- #
def bare_content(source_id: str, raw_text: str = "有正文，但元数据全空。") -> RawContent:
    """只有平台 id、没有标题/作者/简介 —— 元数据没取到的形态。"""
    return RawContent(
        source="douyin",
        source_id=source_id,
        source_url=f"https://www.douyin.com/video/{source_id}",
        media_type="video",
        raw_text=raw_text,
    )


async def test_weak_hash_does_not_dedupe(pipeline, repo: ContentRepository) -> None:
    """元数据全空、正文不同的两条 → **不该**判重 —— 那是误杀。

    注意两条的正文必须真的不同：v2 起正文参与哈希，正文也一样的话
    它们在白名单里就完全无法区分，判重反而是对的。
    """
    first = await pipeline.process(bare_content("1111111111111111111", "第一条的正文。"))
    second = await pipeline.process(bare_content("2222222222222222222", "第二条完全不同的正文。"))

    assert first.outcome == "completed"
    assert second.outcome == "completed"
    assert second.content_id != first.content_id
    assert await repo.count_contents() == 2


async def test_weak_hash_can_still_duplicate_by_source_id(
    pipeline, repo: ContentRepository
) -> None:
    """哈希没区分度 ≠ 不能判重：同 source_id 依然精确命中。"""
    raw = bare_content("3333333333333333333")
    assert (await pipeline.process(raw)).outcome == "completed"
    assert (await pipeline.process(raw)).outcome == "duplicate"
    assert await repo.count_contents() == 1


# --------------------------------------------------------------------------- #
# 6. 两轨并存的总验收
# --------------------------------------------------------------------------- #
async def test_both_tracks_coexist(
    pipeline, repo: ContentRepository, vault_root: Path
) -> None:
    async with douyin_client() as client:
        douyin_raw = await DouyinSource(client=client).fetch(SHORT_LINK_A)
    manual_raw = await ManualPasteSource().fetch(
        ManualPastePayload(title="手贴内容", raw_text="完全不同的另一段内容。")
    )

    douyin_result = await pipeline.process(douyin_raw)
    manual_result = await pipeline.process(manual_raw)

    assert douyin_result.outcome == "completed"
    assert manual_result.outcome == "completed"
    assert await repo.count_contents() == 2
    assert len(notes_in(vault_root)) == 2

    sources = {row.source for row in await repo.list_by_status("completed")}
    assert sources == {"douyin", "manual"}


def test_aweme_id_is_never_a_url() -> None:
    """source_id 永不回退成 URL（第五节）。"""
    assert extract_aweme_id(CANONICAL) == AWEME_ID
    assert extract_aweme_id(SHORT_LINK_A) is None


def test_error_classes_are_the_documented_ones() -> None:
    assert RequestBlockedError().error_type_value == "REQUEST_BLOCKED"
    assert CookieRequiredError().error_type_value == "COOKIE_REQUIRED"
    assert AwemeIdNotFoundError().error_type_value == "AWEME_ID_NOT_FOUND"
    assert ParserUnsupportedError().error_type_value == "PARSER_UNSUPPORTED"
    assert NetworkTimeoutError().error_type_value == "NETWORK_TIMEOUT"
    assert ShortLinkResolutionError().error_type_value == "SHORT_LINK_RESOLUTION_FAILED"
