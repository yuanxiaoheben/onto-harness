"""Serializable plan/tool machine with local or optional LangGraph execution.

RunStore persists application checkpoints at explicit node boundaries. It is not
a native LangGraph checkpointer, nor an exactly-once provider execution service.
"""

import asyncio
from dataclasses import asdict
from time import monotonic
from typing import Any, TypedDict
from uuid import uuid4

from .. import __version__
from ..common import HarnessError, canonical, digest, safe_error, validate_json
from ..domain import LLMResponse
from ..state import RunStore, WorkspaceLock
from .contracts import (
    AgentDecision,
    AgentResult,
    Budget,
    BudgetExceeded,
    Observation,
    Planner,
    Tool,
    ToolResult,
)


class GraphState(TypedDict):
    durable: dict[str, Any]


class AgentRuntime:
    def __init__(
        self,
        planner: Planner,
        tools: list[Tool],
        budget: Budget | None = None,
        *,
        store: RunStore | None = None,
        run_id: str | None = None,
        backend: str = "local",
        config_hash: str = "",
        ontology_id: str | None = None,
        base_version: str | None = None,
        bindings: dict[str, Any] | None = None,
    ):
        self.planner, self.tools, self.budget = (
            planner,
            {tool.name: tool for tool in tools},
            budget or Budget(),
        )
        if not self.tools or len(self.tools) != len(tools):
            raise HarnessError("invalid_tools", "Tools must be nonempty with unique names")
        if backend not in {"local", "langgraph"}:
            raise HarnessError("invalid_backend", "Agent backend must be local or langgraph")
        self.store, self.run_id, self.backend = store, run_id or uuid4().hex, backend
        self.config_hash, self.ontology_id, self.base_version = (
            config_hash,
            ontology_id,
            base_version,
        )
        self.bindings = bindings or {}
        self.state: dict[str, Any] = {}
        self.events: list[dict[str, Any]] = []
        self._used = False
        self._started = self._elapsed = 0.0
        self._signature = digest(
            {
                "tools": [
                    {
                        **tool.descriptor(),
                        "idempotent": tool.idempotent,
                        "output_schema": tool.output_schema,
                    }
                    for tool in tools
                ],
                "planner": getattr(planner, "fingerprint", type(planner).__qualname__),
                "budget": asdict(self.budget),
                "config": config_hash,
                "backend": backend,
                "bindings": self.bindings,
                "ontology_id": self.ontology_id,
                "base_version": self.base_version,
            }
        )

    def _save(self) -> None:
        self.state["elapsed_seconds"] = self._elapsed + monotonic() - self._started
        if self.store:
            self.store.save(self.state)

    def _event(self, kind: str, **value: Any) -> None:
        if self.store:
            self.store.event(self.run_id, kind, value)
        else:
            self.events.append({"sequence": len(self.events), "kind": kind, **value})

    def _finish(self, status: str, kind: str, payload: dict[str, Any]) -> None:
        self.state.update(status=status, result_kind=kind, result=payload)
        self._save()
        self._event("run_stopped", status=status)

    def _meter(self) -> None:
        if self.store and self.store.cancelled(self.run_id):
            raise HarnessError("cancelled", "Cancellation requested")
        if self._elapsed + monotonic() - self._started >= self.budget.timeout_seconds:
            raise HarnessError("deadline", "Cumulative Agent active runtime budget exhausted")
        if self.state["model_calls"] >= self.budget.max_model_calls:
            raise HarnessError("model_call_limit", "Cumulative Agent model call budget exhausted")
        self.state["model_calls"] += 1
        self.state["usage"]["unknown_calls"] += 1
        self._save()
        self._event("model_attempt", call=self.state["model_calls"])

    def _record_response(self, response: LLMResponse) -> None:
        usage = response.usage
        if usage.get("input_tokens") is not None and usage.get("output_tokens") is not None:
            self.state["usage"]["input_tokens"] += usage["input_tokens"]
            self.state["usage"]["output_tokens"] += usage["output_tokens"]
            self.state["usage"]["unknown_calls"] -= 1
        self._save()
        self._event("model_response", model=response.model, usage=usage, metadata=response.metadata)

    def _history(self) -> list[Observation]:
        result: list[Observation] = []
        size = 0
        for item in reversed(self.state["observations"][-self.budget.max_observations :]):
            length = len(canonical(item["summary"]).encode("utf-8"))
            if size + length > self.budget.max_history_bytes:
                continue
            result.append(Observation(item["id"], item["tool"], item["summary"]))
            size += length
        return list(reversed(result))

    async def _plan(self) -> None:
        if self.state["steps"] >= self.budget.max_steps:
            self._finish(
                "budget_exhausted",
                "partial_result",
                {"reason": "step_limit", "observations": self.state["observations"]},
            )
            return
        self.state["steps"] += 1
        self._save()
        decision = await self.planner.decide(
            self.state["message"],
            [tool.descriptor() for tool in self.tools.values()],
            self._history(),
        )
        if not isinstance(decision, AgentDecision):
            raise HarnessError("invalid_decision", "Planner must return an AgentDecision")
        if decision.action == "clarify":
            if (
                not isinstance(decision.question, str)
                or not decision.question.strip()
                or decision.tool
                or decision.arguments
            ):
                raise HarnessError("invalid_decision", "Invalid clarification decision")
            self._finish(
                "waiting_input",
                "clarification",
                {"question": decision.question, "resumable": self.store is not None},
            )
            return
        if (
            decision.action != "tool"
            or decision.tool not in self.tools
            or decision.question
            or not isinstance(decision.arguments, dict)
        ):
            raise HarnessError("invalid_decision", "Planner selected an invalid tool decision")
        self.state["decision"] = asdict(decision)
        self.state["stage"] = "execute"
        self._save()
        self._event("tool_selected", tool=decision.tool, step=self.state["steps"])

    async def _execute(self) -> None:
        from ..tools.invocation import invoke_tool, operation_id

        decision = AgentDecision(**self.state["decision"])
        tool = self.tools[decision.tool]
        validate_json(decision.arguments, tool.input_schema)
        signature = digest([tool.name, tool.version, decision.arguments])
        identifier = f"tool-{self.state['steps']}"
        self.state["pending_operation"] = identifier
        prior = (
            self.store.operation(self.run_id, identifier, signature, create=False)
            if self.store
            else None
        )
        if prior and prior["status"] == "done":
            result = ToolResult(**prior["value"])
        else:
            if prior and not tool.idempotent:
                self._finish(
                    "waiting_review",
                    "operation_review",
                    {
                        "operation_id": identifier,
                        "tool": tool.name,
                        "reason": "Previous side effect may have completed; supply an inspected result before resuming",
                    },
                )
                return
            if self.state["tool_calls"] >= self.budget.max_tool_calls:
                self._finish(
                    "budget_exhausted",
                    "partial_result",
                    {"reason": "tool_limit", "observations": self.state["observations"]},
                )
                return
            if signature in self.state["seen"] and not prior:
                self._finish(
                    "budget_exhausted",
                    "partial_result",
                    {"reason": "repeated_tool_call", "observations": self.state["observations"]},
                )
                return
            self.state["tool_calls"] += 1
            if self.store:
                self.store.operation(self.run_id, identifier, signature)
            self._save()
            token = operation_id.set(self.run_id + ":" + identifier)
            try:
                result = await invoke_tool(tool, decision.arguments)
            finally:
                operation_id.reset(token)
        reference = self.store.db.artifact(asdict(result)) if self.store else None
        summary = result.summary
        if len(canonical(summary).encode("utf-8")) > self.budget.max_history_bytes:
            summary = {"artifact_ref": reference, "truncated": True}
        self.state["observations"].append(
            {"id": identifier, "tool": tool.name, "summary": summary, "artifact_ref": reference}
        )
        self.state["seen"].append(signature)
        self.state["decision"], self.state["stage"] = None, "plan"
        self.state.pop("pending_operation", None)
        if tool.terminal:
            self.state.update(
                status="completed", result_kind=tool.result_kind, result=result.payload
            )
        self.state["elapsed_seconds"] = self._elapsed + monotonic() - self._started
        if self.store:
            self.store.complete_operation(self.state, identifier, asdict(result))
        else:
            self._save()
        self._event("tool_completed", tool=tool.name, operation_id=identifier)

    async def _advance(self) -> None:
        if self.store and self.store.cancelled(self.run_id):
            raise HarnessError("cancelled", "Cancellation requested")
        if self.state["stage"] == "execute":
            await self._execute()
        else:
            await self._plan()

    async def _loop(self) -> None:
        if self.backend == "langgraph":
            from langgraph.graph import END, START, StateGraph

            async def node(state: GraphState) -> GraphState:
                self.state = state["durable"]
                await self._advance()
                return {"durable": self.state}

            graph = StateGraph(GraphState)
            graph.add_node("advance", node)
            graph.add_edge(START, "advance")
            graph.add_conditional_edges(
                "advance",
                lambda state: state["durable"]["status"] == "running",
                {True: "advance", False: END},
            )
            await graph.compile().ainvoke(
                {"durable": self.state}, config={"recursion_limit": 2 * self.budget.max_steps + 10}
            )
        else:
            while self.state["status"] == "running":
                await self._advance()

    async def run(self, message: str, *, answer: str | None = None) -> AgentResult:
        if self._used:
            raise HarnessError("runtime_reused", "Create one AgentRuntime instance per execution")
        self._used = True
        if self.store:
            with WorkspaceLock(self.store.db.root):
                return await self._run(message, answer=answer)
        return await self._run(message, answer=answer)

    async def _run(self, message: str, *, answer: str | None = None) -> AgentResult:
        from ..tools.invocation import model_attempt, model_response

        existing = False
        if self.store:
            try:
                self.state = self.store.get(self.run_id)
                existing = True
            except HarnessError as error:
                if error.code != "not_found":
                    raise
        if existing:
            if (
                self.state["kind"] != "agent"
                or self.state["signature"] != self._signature
                or self.state["package_version"] != __version__
            ):
                raise HarnessError(
                    "resume_incompatible",
                    "Agent tools, configuration, planner or package version changed",
                )
            if self.state["status"] == "completed":
                return self._result()
            if self.store:
                with self.store.db.connect() as db:
                    db.execute("UPDATE controls SET cancel=0 WHERE run_id=?", (self.run_id,))
            if self.state["status"] == "waiting_input":
                if not answer or not answer.strip():
                    return self._result()
                self.state["message"] += "\nClarification answer: " + answer
            elif answer:
                raise HarnessError(
                    "invalid_resume", "This run is not waiting for a clarification answer"
                )
        else:
            self.state = {
                "ontology_id": self.ontology_id,
                "base_version": self.base_version,
                "signature": self._signature,
                "package_version": __version__,
                "config_hash": self.config_hash,
                "bindings": self.bindings,
                "tool_manifest": {
                    name: {
                        "effect": tool.effect,
                        "version": tool.version,
                        "output_schema": tool.output_schema,
                    }
                    for name, tool in self.tools.items()
                },
                "backend": self.backend,
                "budget": asdict(self.budget),
                "message": message,
                "stage": "plan",
                "decision": None,
                "observations": [],
                "seen": [],
                "steps": 0,
                "tool_calls": 0,
                "model_calls": 0,
                "elapsed_seconds": 0.0,
            }
            self.state["usage"] = {"input_tokens": 0, "output_tokens": 0, "unknown_calls": 0}
            if self.store:
                self.run_id = self.store.create("agent", self.state, run_id=self.run_id)
                self.state = self.store.get(self.run_id)
            else:
                self.state.update(run_id=self.run_id, revision=0)
        self._started, self._elapsed = monotonic(), self.state["elapsed_seconds"]
        self.state["status"] = "running"
        self.state.pop("error", None)
        self._save()
        token = model_attempt.set(self._meter)
        response_token = model_response.set(self._record_response)
        try:
            remaining = self.budget.timeout_seconds - self._elapsed
            if remaining <= 0:
                raise HarnessError("deadline", "Cumulative Agent active runtime exhausted")
            task = asyncio.create_task(self._loop())
            try:
                while not task.done():
                    if self.store and self.store.cancelled(self.run_id):
                        raise HarnessError("cancelled", "Cancellation requested")
                    if monotonic() - self._started >= remaining:
                        raise HarnessError("deadline", "Cumulative Agent active runtime exhausted")
                    await asyncio.wait({task}, timeout=0.2)
                task.result()
            finally:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
        except asyncio.CancelledError:
            self._finish(
                "cancelled", "error", {"code": "cancelled", "message": "Executor cancelled"}
            )
            raise
        except Exception as error:
            detail = safe_error(error)
            code = detail["code"]
            status = (
                "cancelled"
                if code == "cancelled"
                else (
                    "budget_exhausted"
                    if code in {"deadline", "model_call_limit"} or isinstance(error, BudgetExceeded)
                    else "failed"
                )
            )
            self.state["error"] = detail
            self._finish(
                status,
                "partial_result",
                {"error": detail, "observations": self.state["observations"]},
            )
        finally:
            model_attempt.reset(token)
            model_response.reset(response_token)
            self._save()
            self._event(
                "execution_ended",
                status=self.state["status"],
                elapsed_seconds=self.state["elapsed_seconds"],
            )
        return self._result()

    def _result(self) -> AgentResult:
        events = self.store.events(self.run_id) if self.store else self.events
        return AgentResult(
            self.run_id,
            self.state["status"],
            self.state.get("result_kind", "agent_result"),
            self.state.get("result", {}),
            self.state["steps"],
            self.state["tool_calls"],
            events,
        )
