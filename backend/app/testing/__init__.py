"""测试辅助设施出口。"""

from app.testing.fixture_loader import (
    BACKEND_ROOT,
    FIXTURES_DIR,
    LLM_OUTPUT_FIXTURES,
    RAW_CONTENT_FIXTURES,
    REQUIRED_FIXTURES,
    fixture_exists,
    fixture_path,
    fixtures_dir,
    list_fixtures,
    load_fixture_json,
    load_fixture_text,
    missing_fixtures,
)
from app.testing.providers import ScriptedProvider, TransportFailureProvider

__all__ = [
    "BACKEND_ROOT",
    "FIXTURES_DIR",
    "REQUIRED_FIXTURES",
    "RAW_CONTENT_FIXTURES",
    "LLM_OUTPUT_FIXTURES",
    "fixtures_dir",
    "fixture_path",
    "fixture_exists",
    "list_fixtures",
    "missing_fixtures",
    "load_fixture_text",
    "load_fixture_json",
    "ScriptedProvider",
    "TransportFailureProvider",
]
