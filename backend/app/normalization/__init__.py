"""归一化层出口。"""

from app.normalization.hashing import (
    CONTENT_HASH_VERSION,
    FORBIDDEN_HASH_FIELDS,
    HASH_INPUT_FIELDS,
    HASH_SEPARATOR,
    build_hash_payload,
    compute_content_hash,
)
from app.normalization.service import NormalizedContent, normalize_content
from app.normalization.source_id import (
    HASH_FALLBACK_LENGTH,
    HASH_FALLBACK_PREFIX,
    SourceIdResolution,
    fallback_source_id,
    looks_like_url,
    resolve_source_id,
)
from app.normalization.text import is_blank, normalize_optional, normalize_text
from app.normalization.url import (
    ALLOWED_SCHEMES,
    DEFAULT_ALLOWED_HOSTS,
    URL_PATTERN,
    ValidatedURL,
    extract_first_url,
    host_allowed,
    is_valid_url,
    validate_url,
)

__all__ = [
    "normalize_text",
    "normalize_optional",
    "is_blank",
    "compute_content_hash",
    "build_hash_payload",
    "CONTENT_HASH_VERSION",
    "HASH_SEPARATOR",
    "HASH_INPUT_FIELDS",
    "FORBIDDEN_HASH_FIELDS",
    "resolve_source_id",
    "fallback_source_id",
    "looks_like_url",
    "SourceIdResolution",
    "HASH_FALLBACK_PREFIX",
    "HASH_FALLBACK_LENGTH",
    "NormalizedContent",
    "normalize_content",
    "validate_url",
    "is_valid_url",
    "host_allowed",
    "ValidatedURL",
    "DEFAULT_ALLOWED_HOSTS",
    "ALLOWED_SCHEMES",
    "extract_first_url",
    "URL_PATTERN",
]
