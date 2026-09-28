from __future__ import annotations

import hashlib

from app.messaging.idempotency import (
    MARKDOWN_PARSE,
    NO_PARSE,
    build_idempotency_key,
    canonical_parse_mode,
    format_identity,
)


def test_telethon_equivalent_spellings_canonicalize_together() -> None:
    assert canonical_parse_mode("md") == canonical_parse_mode("markdown") == MARKDOWN_PARSE
    assert canonical_parse_mode(None) == NO_PARSE
    assert canonical_parse_mode("html") == "html"


def test_plain_text_format_is_omitted_from_the_key() -> None:
    assert format_identity(None, False, has_media=False) == ""


def test_markdown_and_link_preview_suppression_change_the_key_source() -> None:
    plain = format_identity(None, False, has_media=False)
    markdown = format_identity("md", False, has_media=False)
    suppressed_preview = format_identity(None, True, has_media=False)

    assert len({plain, markdown, suppressed_preview}) == 3


def test_link_preview_flag_cannot_split_a_caption() -> None:
    """The sender passes link_preview for text only, so it is inert for media."""
    assert format_identity(None, True, has_media=True) == ""
    assert format_identity("markdown", True, has_media=True) == format_identity(
        "markdown", False, has_media=True
    )


def test_default_key_matches_the_pre_formatting_construction() -> None:
    """Guards the stored history: SENT rows keep keys built without a format component."""
    legacy = hashlib.sha256(b"-1001\0hi\0\0IMMEDIATE").hexdigest()

    assert build_idempotency_key(-1001, "hi", "", "IMMEDIATE") == legacy
    assert build_idempotency_key(-1001, "hi", "", "IMMEDIATE", "") == legacy
    formatted = build_idempotency_key(-1001, "hi", "", "IMMEDIATE", "parse=markdown;preview=on")
    assert formatted != legacy
