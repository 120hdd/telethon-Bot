from __future__ import annotations

import hashlib
import unicodedata
from pathlib import Path

PARSE_MODES = frozenset({"md", "markdown", "html"})

# format_identity() tokens. They are hashed into stored idempotency keys, so their
# spelling is part of the database contract: renaming one re-keys those jobs.
NO_PARSE = "none"
MARKDOWN_PARSE = "markdown"
HTML_PARSE = "html"


def normalize_message(text: str | None) -> str:
    if text is None:
        return ""
    normalized = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    return normalized.strip()


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_parse_mode(parse_mode: str | None) -> str:
    """Collapse the spellings Telethon parses alike, so they cannot hash apart."""
    if parse_mode is None:
        return NO_PARSE
    lowered = parse_mode.lower()
    if lowered in {"md", "markdown"}:
        return MARKDOWN_PARSE
    if lowered in {"htm", "html"}:
        return HTML_PARSE
    return lowered


def format_identity(parse_mode: str | None, disable_link_preview: bool, *, has_media: bool) -> str:
    """Describe how a job renders, for the idempotency key.

    An empty result means the plain default and omits the component from the key
    entirely. That is deliberate: SENT rows stay in the unique index forever, so
    re-keying them would discard the "already sent" memory and let a repeated plain
    send reach a live group. Only formatting that was previously untracked gets a
    new scope.

    A caption has no link preview to suppress (the sender passes ``link_preview``
    for text only), so that flag is normalized to "on" for media and cannot split
    the key into a duplicate post.
    """
    canonical = canonical_parse_mode(parse_mode)
    if has_media:
        return "" if canonical == NO_PARSE else f"parse={canonical};preview=on"
    if canonical == NO_PARSE and not disable_link_preview:
        return ""
    return f"parse={canonical};preview={'off' if disable_link_preview else 'on'}"


def build_idempotency_key(
    destination_chat_id: int,
    normalized_text: str,
    media_hash: str,
    schedule_identity: str,
    format_component: str = "",
) -> str:
    components = [str(destination_chat_id), normalized_text, media_hash, schedule_identity]
    if format_component:
        components.append(format_component)
    source = "\0".join(components)
    return hashlib.sha256(source.encode("utf-8", errors="strict")).hexdigest()
