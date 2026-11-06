"""Common invocation boundary, including direct CLI and Agent calls."""

import asyncio
from contextvars import ContextVar
from dataclasses import asdict
from math import isfinite
from typing import Callable

from ..agent.contracts import Tool, ToolResult
from ..common import HarnessError, canonical, validate_json
from ..domain import LLMResponse

operation_id: ContextVar[str | None] = ContextVar("harness_operation", default=None)
model_attempt: ContextVar[Callable[[], None] | None] = ContextVar(
    "harness_model_attempt", default=None
)
model_response: ContextVar[Callable[[LLMResponse], None] | None] = ContextVar(
    "harness_model_response", default=None
)


async def invoke_tool(tool: Tool, arguments: dict) -> ToolResult:
    validate_json(arguments, tool.input_schema)
    if (
        type(tool.timeout_seconds) not in (int, float)
        or not isfinite(tool.timeout_seconds)
        or tool.timeout_seconds <= 0
    ):
        raise HarnessError("invalid_tool", "Tool timeout must be positive")
    try:
        result = await asyncio.wait_for(tool.invoke(arguments), timeout=tool.timeout_seconds)
    except asyncio.TimeoutError:
        raise HarnessError(
            "tool_timeout", "Tool execution timed out", retryable=tool.idempotent
        ) from None
    if (
        not isinstance(result, ToolResult)
        or not isinstance(result.payload, dict)
        or not isinstance(result.summary, dict)
    ):
        raise HarnessError("invalid_tool_result", "Tool must return structured payload and summary")
    if len(canonical(asdict(result)).encode("utf-8")) > 1_000_000:
        raise HarnessError(
            "tool_output_too_large", "Tool result exceeded 1 MB; return an artifact reference"
        )
    if tool.output_schema:
        validate_json(result.payload, tool.output_schema)
    return result
