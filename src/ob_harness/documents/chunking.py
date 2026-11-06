"""A replaceable character-window baseline with exact source offsets."""

from ..domain import Chunk, Document


def chunk_document(document: Document, max_chars: int, overlap_chars: int) -> list[Chunk]:
    if not 0 <= overlap_chars < max_chars:
        raise ValueError("Require 0 <= overlap_chars < max_chars")
    result = []
    start = 0
    while start < len(document.text):
        end = min(start + max_chars, len(document.text))
        text = document.text[start:end]
        if text.strip():
            result.append(
                Chunk(
                    id=f"{document.id}:{start}:{end}",
                    document_id=document.id,
                    text=text,
                    start=start,
                    end=end,
                )
            )
        if end == len(document.text):
            break
        start = end - overlap_chars
    return result
