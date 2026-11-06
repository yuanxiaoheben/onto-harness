from ..common import HarnessError, digest


def message_document(message: dict, text: str) -> dict:
    import hashlib

    return {
        "id": "message_" + message["id"],
        "path": f"conversation://{message['conversation_id']}/{message['id']}",
        "text": text,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "metadata": {
            "source_kind": "user_message",
            "source_role": "user",
            "conversation_id": message["conversation_id"],
            "message_id": message["id"],
            "subject": message["subject"],
        },
    }


def source_span(app, message: dict, quote: str | None = None) -> dict:
    doc = app.db.read_artifact(message["document_ref"])
    text = doc["text"]
    quote = text if quote is None else quote
    if not isinstance(quote, str) or not quote:
        raise HarnessError("invalid_evidence", "Quote must occur exactly once in the user message")
    start = text.find(quote)
    if start < 0 or text.find(quote, start + 1) >= 0:
        raise HarnessError("invalid_evidence", "Quote must occur exactly once in the user message")
    return {
        "document_id": doc["id"],
        "artifact": message["document_ref"],
        "text_hash": digest(text),
        "start": start,
        "end": start + len(quote),
        "quote": quote,
    }
