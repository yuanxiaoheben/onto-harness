"""Load parser-produced documents for the existing chunking and construction interfaces."""

import json
from pathlib import Path

from ..domain import Document


def load_document_artifact(path: Path) -> Document:
    record = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(record, dict) or record.get("schema") != "ob-harness.document/v1":
        raise ValueError("Unsupported parsed-document schema")
    value = record.get("document")
    if not isinstance(value, dict) or set(value) != {"id", "path", "text", "sha256", "metadata"}:
        raise ValueError("Invalid document artifact fields")
    if any(
        not isinstance(value[key], str) or not value[key].strip()
        for key in ("id", "path", "text", "sha256")
    ):
        raise ValueError("Document identity, path, text and source hash must be nonempty strings")
    if not isinstance(value["metadata"], dict):
        raise ValueError("Document metadata must be an object")
    digest = value["sha256"]
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("Invalid source SHA-256")
    return Document(**value)
