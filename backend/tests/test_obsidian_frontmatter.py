"""Frontmatter 渲染与解析（第二十节）。"""

from __future__ import annotations

import pytest
import yaml

from app.obsidian.frontmatter import (
    DELIMITER,
    FRONTMATTER_KEY_ORDER,
    frontmatter_fields_in_order,
    parse_frontmatter,
    render_frontmatter,
)

SAMPLE = {
    "title": "人口下降之后，房子还会涨吗",
    "source": "douyin",
    "source_id": "7321567890123456789",
    "source_url": "https://www.douyin.com/video/1",
    "author": "老王聊经济",
    "media_type": "video",
    "analysis_type": "mixed",
    "topics": ["人口结构", "房地产"],
    "tags": ["mixed", "人口结构", "房地产"],
    "created_at": "2026-09-30T00:10:50Z",
    "processed_at": "2026-09-30T00:12:00Z",
    "model": "mock-llm-v1",
    "prompt_version": "analysis_prompt_v1",
    "confidence": 0.72,
    "needs_verification": True,
    "needs_manual_review": False,
    "content_hash_version": 1,
}


def test_key_order_matches_spec() -> None:
    assert FRONTMATTER_KEY_ORDER == (
        "title",
        "source",
        "source_id",
        "source_url",
        "author",
        "media_type",
        "analysis_type",
        "topics",
        "tags",
        "created_at",
        "processed_at",
        "model",
        "prompt_version",
        "confidence",
        "needs_verification",
        "needs_manual_review",
        "content_hash_version",
    )
    assert len(FRONTMATTER_KEY_ORDER) == 17


def test_render_has_delimiters() -> None:
    block = render_frontmatter(SAMPLE)
    assert block.startswith(f"{DELIMITER}\n")
    assert block.endswith(f"{DELIMITER}\n")


def test_round_trip_preserves_values() -> None:
    block = render_frontmatter(SAMPLE)
    loaded = yaml.safe_load(block.strip().strip(DELIMITER))
    assert loaded == SAMPLE


def test_round_trip_preserves_key_order() -> None:
    block = render_frontmatter(SAMPLE)
    assert frontmatter_fields_in_order(block) == list(FRONTMATTER_KEY_ORDER)


def test_missing_field_rejected() -> None:
    incomplete = dict(SAMPLE)
    incomplete.pop("prompt_version")
    with pytest.raises(ValueError) as excinfo:
        render_frontmatter(incomplete)
    assert "prompt_version" in str(excinfo.value)


def test_extra_field_is_ignored() -> None:
    """多给的键不写进 frontmatter（保持与文档字段集一致）。"""
    block = render_frontmatter({**SAMPLE, "extra_key": "x"})
    assert "extra_key" not in block


def test_empty_strings_render_as_quoted_empty() -> None:
    values = dict(SAMPLE, title="", source_url="")
    block = render_frontmatter(values)
    loaded = yaml.safe_load(block.strip().strip(DELIMITER))
    assert loaded["title"] == ""
    assert loaded["source_url"] == ""


def test_empty_list_renders_as_flow_empty() -> None:
    block = render_frontmatter(dict(SAMPLE, topics=[], tags=[]))
    assert "topics: []" in block
    assert "tags: []" in block


def test_iso_timestamp_stays_string() -> None:
    block = render_frontmatter(SAMPLE)
    loaded = yaml.safe_load(block.strip().strip(DELIMITER))
    assert isinstance(loaded["created_at"], str)
    assert loaded["created_at"] == "2026-09-30T00:10:50Z"


def test_booleans_are_lowercase() -> None:
    block = render_frontmatter(SAMPLE)
    assert "needs_verification: true" in block
    assert "needs_manual_review: false" in block


@pytest.mark.parametrize(
    "title",
    [
        "含冒号: 的标题",
        "含井号 # 的标题",
        '"带双引号的标题"',
        "带'单引号'的标题",
        "- 以破折号开头",
        "含 反斜杠 \\ 与 花括号 { }",
        "很长的标题" * 30,
    ],
)
def test_tricky_titles_round_trip(title: str) -> None:
    block = render_frontmatter(dict(SAMPLE, title=title))
    loaded = yaml.safe_load(block.strip().strip(DELIMITER))
    assert loaded["title"] == title


def test_multiline_value_round_trips() -> None:
    block = render_frontmatter(dict(SAMPLE, title="第一行\n第二行"))
    loaded = yaml.safe_load(block.strip().strip(DELIMITER))
    assert loaded["title"] == "第一行\n第二行"


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def test_parse_round_trip() -> None:
    block = render_frontmatter(SAMPLE)
    assert parse_frontmatter(block + "\n# 标题\n") == SAMPLE


def test_parse_without_frontmatter() -> None:
    assert parse_frontmatter("# 只有正文\n") == {}


def test_parse_broken_yaml_returns_empty() -> None:
    assert parse_frontmatter("---\ntitle: [unclosed\n---\n") == {}


def test_parse_non_mapping_returns_empty() -> None:
    assert parse_frontmatter("---\n- 1\n- 2\n---\n") == {}


def test_parse_unterminated_block_returns_empty() -> None:
    assert parse_frontmatter("---\ntitle: x\n") == {}


def test_fields_in_order_ignores_body() -> None:
    markdown = "---\na: 1\nb: 2\n---\n\n# 标题\nc: 3\n"
    assert frontmatter_fields_in_order(markdown) == ["a", "b"]
