"""Framework-independent inputs, tools and results for application adapters."""

from dataclasses import dataclass, field
from math import isfinite
from typing import Any, Awaitable, Callable, Literal, Protocol


@dataclass(frozen=True)
class Budget:
    max_steps: int = 6
    max_tool_calls: int = 4
    timeout_seconds: float = 90.0
    max_model_calls: int = 12
    max_observations: int = 8
    max_history_bytes: int = 4000

    def __post_init__(self) -> None:
        counters = (
            self.max_steps,
            self.max_tool_calls,
            self.max_model_calls,
            self.max_observations,
            self.max_history_bytes,
        )
        if (
            any(type(value) is not int or value < 1 for value in counters)
            or type(self.timeout_seconds) not in (int, float)
            or not isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("Agent budgets must be positive")


@dataclass(frozen=True)
class AgentDecision:
    action: Literal["tool", "clarify"]
    tool: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    question: str = ""


@dataclass(frozen=True)
class ToolResult:
    payload: dict[str, Any]
    # Small, structured, authorized data for the next planner call; never raw traces.
    summary: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    invoke: Callable[[dict[str, Any]], Awaitable[ToolResult]]
    terminal: bool = False
    result_kind: str = "tool_result"
    effect: Literal["read", "propose", "write", "external"] = "external"
    idempotent: bool = False
    version: str = "1"
    timeout_seconds: float = 60.0
    output_schema: dict[str, Any] | None = None

    def descriptor(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "terminal": self.terminal,
            "effect": self.effect,
            "version": self.version,
        }


@dataclass(frozen=True)
class Observation:
    id: str
    tool: str
    summary: dict[str, Any]


class Planner(Protocol):
    async def decide(
        self, message: str, tools: list[dict[str, Any]], observations: list[Observation]
    ) -> AgentDecision: ...


@dataclass
class AgentResult:
    run_id: str
    status: Literal[
        "completed", "waiting_input", "waiting_review", "budget_exhausted", "failed", "cancelled"
    ]
    result_kind: str
    payload: dict[str, Any]
    steps: int
    tool_calls: int
    events: list[dict[str, Any]]


class InvalidDecision(ValueError):
    """A planner selected an unknown tool or produced invalid tool arguments."""


class BudgetExceeded(RuntimeError):
    """Raised by a metered adapter before another model call is started."""
