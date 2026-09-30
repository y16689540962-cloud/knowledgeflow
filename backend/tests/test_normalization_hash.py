"""content_hash 稳定性与白名单（第七节，强制）。"""

from __future__ import annotations

import hashlib
import inspect

import pytest

from app.normalization import (
    CONTENT_HASH_VERSION,
    FORBIDDEN_HASH_FIELDS,
    HASH_INPUT_FIELDS,
    HASH_SEPARATOR,
    build_hash_payload,
    compute_content_hash,
    normalize_content,
)
from app.schemas import RawContent


def base(**overrides: object) -> dict:
    payload = {
        "source": "douyin",
        "source_id": "7321567890123456789",
        "source_url": "https://www.douyin.com/video/7321567890123456789",
        "title": "标题",
        "author": "作者",
        "description": "描述",
        "media_type": "video",
        "raw_text": "原文",
        "transcript": "逐字稿",
        "ocr_text": "图片文字",
    }
    payload.update(overrides)
    return payload


def hash_of(**overrides: object) -> str:
    return normalize_content(RawContent.from_fixture(base(**overrides))).content_hash


# --------------------------------------------------------------------------- #
# 签名即白名单
# --------------------------------------------------------------------------- #
def test_signature_only_accepts_whitelisted_fields() -> None:
    """函数签名里不存在第 6 个输入参数 —— 从结构上保证白名单。"""
    params = set(inspect.signature(compute_content_hash).parameters)
    assert params == set(HASH_INPUT_FIELDS) == {
        "source",
        "title",
        "author",
        "description",
        "raw_text",
    }


def test_whitelist_and_forbidden_sets_are_disjoint() -> None:
    assert set(HASH_INPUT_FIELDS).isdisjoint(FORBIDDEN_HASH_FIELDS)


@pytest.mark.parametrize(
    "field",
    [
        "source_id",
        "source_url",
        "transcript",
        "ocr_text",
        "id",
        "created_at",
        "processed_at",
        "status",
        "error_type",
        "error_message",
    ],
)
def test_spec_forbidden_fields_are_listed(field: str) -> None:
    assert field in FORBIDDEN_HASH_FIELDS


# --------------------------------------------------------------------------- #
# 确定性
# --------------------------------------------------------------------------- #
def test_hash_version_is_two() -> None:
    """``2`` = 白名单纳入 ``raw_text``（v1 只吃四项，无标题内容会算出同一个哈希）。"""
    assert CONTENT_HASH_VERSION == 2
    assert normalize_content(RawContent.from_fixture(base())).content_hash_version == 2


def test_hash_is_sha256_hex() -> None:
    digest = hash_of()
    assert len(digest) == 64
    assert all(ch in "0123456789abcdef" for ch in digest)


def test_hash_matches_manual_computation() -> None:
    payload = HASH_SEPARATOR.join(["douyin", "标题", "作者", "描述", "原文"])
    assert hash_of() == hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_hash_is_stable_across_calls() -> None:
    assert hash_of() == hash_of()


def test_hash_insensitive_to_whitespace_and_fullwidth() -> None:
    assert hash_of(title="　标题 ") == hash_of(title="标题")
    assert hash_of(author="ＡＩ") == hash_of(author="AI")


def test_none_and_empty_are_equivalent() -> None:
    assert hash_of(title=None) == hash_of(title="")
    assert hash_of(title=None) == hash_of(title="   ")


# --------------------------------------------------------------------------- #
# 排除项（循环依赖 + 漂移）
# --------------------------------------------------------------------------- #
def test_source_id_does_not_affect_hash() -> None:
    """source_id 参与会造成循环依赖（它自己要靠 content_hash fallback）。"""
    assert hash_of(source_id="123") == hash_of(source_id="")


def test_source_url_does_not_affect_hash() -> None:
    assert hash_of(source_url="https://v.douyin.com/aaa") == hash_of(
        source_url="https://v.douyin.com/bbb"
    )


def test_transcript_does_not_affect_hash() -> None:
    """whisper-small 与 whisper-large 结果不同 → 不能参与哈希，否则去重失效。"""
    assert hash_of(transcript="短逐字稿") == hash_of(transcript="完全不同的长逐字稿内容" * 50)


def test_ocr_text_does_not_affect_hash() -> None:
    assert hash_of(ocr_text=None) == hash_of(ocr_text="识别出来的字")


def test_raw_text_does_affect_hash() -> None:
    """v2 起正文参与哈希。

    v1 只吃四项，于是「只粘正文、不填标题」的内容全都算出同一个哈希、
    同一个 ``hash:`` fallback id —— 第二条起被静默判为 duplicate 吞掉。
    """
    assert hash_of(raw_text="正文A") != hash_of(raw_text="正文B")


def test_untitled_pastes_get_distinct_identities() -> None:
    """回归：元数据全空、正文不同的两条 → 哈希与 fallback id 都必须不同。"""
    payload = {
        "source": "manual",
        "source_id": "",
        "source_url": "",
        "media_type": "text",
    }
    first = normalize_content(
        RawContent.from_fixture({**payload, "raw_text": "今天聊人工智能算力。"})
    )
    second = normalize_content(
        RawContent.from_fixture({**payload, "raw_text": "2026 年新能源汽车出口增长四成。"})
    )
    assert first.content_hash != second.content_hash
    assert first.source_id != second.source_id
    assert first.hash_is_discriminating is True
    assert second.hash_is_discriminating is True


def test_media_type_and_path_do_not_affect_hash() -> None:
    assert hash_of(media_type="video") == hash_of(media_type="mixed", media_path="/tmp/a.mp4")


def test_build_hash_payload_shape() -> None:
    assert build_hash_payload(source="s", title="t", author=None, description="d") == (
        "s"
        + HASH_SEPARATOR
        + "t"
        + HASH_SEPARATOR
        + ""
        + HASH_SEPARATOR
        + "d"
        + HASH_SEPARATOR
        + ""
    )


def test_different_field_boundaries_give_different_hash() -> None:
    """分隔符必须真的起作用：``a|bc`` 与 ``ab|c`` 不能撞。"""
    left = compute_content_hash(source="a", title="bc")
    right = compute_content_hash(source="ab", title="c")
    assert left != right


def test_long_fixture_hash_is_deterministic(long_raw_content: RawContent) -> None:
    assert normalize_content(long_raw_content).content_hash == normalize_content(
        long_raw_content
    ).content_hash
