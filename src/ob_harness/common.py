"""Versioned JSON utilities and errors shared by the local application."""

import hashlib
import json
import math
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any


class HarnessError(ValueError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code, self.retryable = code, retryable

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "retryable": self.retryable}


def canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def terms(text: str) -> set[str]:
    text = normalized(text)
    result = set(re.findall(r"[a-z0-9_]+", text))
    for word in re.findall(r"[\u3400-\u9fff]+", text):
        result.update(word[i : i + 2] for i in range(max(1, len(word) - 1)))
    return result


def object_json(text: str) -> dict[str, Any]:
    # Accept a single provider envelope, never extract a guessed JSON substring.
    text = text.strip()
    if text.startswith("```json\n") and text.endswith("```"):
        text = text[8:-3].strip()
    try:
        result = json.loads(text, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError) as error:
        raise HarnessError("invalid_json", "Expected a JSON object") from error
    if not isinstance(result, dict):
        raise HarnessError("invalid_json", "Expected a JSON object")
    return result


def positive_int(value: Any, name: str, *, maximum: int = 1_000_000) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise HarnessError("invalid_config", f"{name} must be an integer in [1, {maximum}]")
    return value


def validate_json(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    """Validate our deliberately small, documented tool-schema vocabulary.

    Unsupported validation keywords are rejected instead of silently ignored.
    This is not a general JSON Schema implementation.
    """
    supported = {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "description",
        "title",
        "default",
        "$schema",
    }
    if set(schema) - supported:
        raise HarnessError("unsupported_schema", "Tool schema uses unsupported validation keywords")
    kinds = schema.get("type", [])
    kinds = [kinds] if isinstance(kinds, str) else kinds
    checks = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": type(value) is int,
        "number": type(value) in (int, float) and math.isfinite(value),
        "boolean": type(value) is bool,
        "null": value is None,
    }
    if kinds and not any(checks.get(kind, False) for kind in kinds):
        raise HarnessError("invalid_arguments", f"{path}: incorrect type")
    if "enum" in schema and value not in schema["enum"]:
        raise HarnessError("invalid_arguments", f"{path}: value is not allowed")
    if "const" in schema and value != schema["const"]:
        raise HarnessError("invalid_arguments", f"{path}: incorrect constant")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        if set(schema.get("required", [])) - value.keys():
            raise HarnessError("invalid_arguments", f"{path}: missing required fields")
        if schema.get("additionalProperties") is False and value.keys() - props.keys():
            raise HarnessError("invalid_arguments", f"{path}: unexpected fields")
        for key, item in value.items():
            if key in props:
                validate_json(item, props[key], f"{path}.{key}")
    if isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", math.inf):
            raise HarnessError("invalid_arguments", f"{path}: incorrect array size")
        for index, item in enumerate(value):
            if "items" in schema:
                validate_json(item, schema["items"], f"{path}[{index}]")
    if isinstance(value, str):
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", math.inf):
            raise HarnessError("invalid_arguments", f"{path}: incorrect string length")
    if type(value) in (int, float):
        if not math.isfinite(value) or not schema.get("minimum", -math.inf) <= value <= schema.get(
            "maximum", math.inf
        ):
            raise HarnessError("invalid_arguments", f"{path}: number out of range")


def safe_error(error: BaseException) -> dict[str, Any]:
    if isinstance(error, HarnessError):
        return error.as_dict()
    # Provider bodies, paths, credentials and third-party exceptions are not public logs.
    return {"code": "execution_failed", "message": type(error).__name__, "retryable": False}
