from __future__ import annotations

import hashlib
import unicodedata
from pathlib import Path


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


def build_idempotency_key(
    destination_chat_id: int,
    normalized_text: str,
    media_hash: str,
    schedule_identity: str,
) -> str:
    source = "\0".join((str(destination_chat_id), normalized_text, media_hash, schedule_identity))
    return hashlib.sha256(source.encode("utf-8", errors="strict")).hexdigest()
