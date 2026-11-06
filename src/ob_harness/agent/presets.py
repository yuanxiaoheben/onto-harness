from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..common import HarnessError
from ..tools.ontology import ontology_tools
from .contracts import AgentDecision, Budget, Planner
from .planner import LLMPlanner, ScriptedPlanner
from .runtime import AgentRuntime

if TYPE_CHECKING:
    from ..services.application import Application


def make_runtime(
    app: "Application",
    ontology_id: str,
    version: str,
    message: str,
    bindings: dict[str, Any],
    *,
    run_id: str | None = None,
    budget: Budget | None = None,
    backend: str = "local",
) -> AgentRuntime:
    tools = ontology_tools(
        app,
        ontology_id,
        version,
        child_run_id=bindings.get("child_run_id"),
        use_memory=bindings.get("use_memory", False),
        namespace=bindings.get("namespace", "default"),
    )
    mode = bindings.get("planner", "scripted")
    planner: Planner
    if mode == "scripted":
        decision = (
            AgentDecision("tool", "ontology_build", {})
            if bindings.get("child_run_id")
            else AgentDecision("tool", "ontology_answer", {"query": message})
        )
        planner = ScriptedPlanner([decision])
    elif mode == "llm":
        planner = LLMPlanner(
            app.model(),
            max_input_bytes=app.config["context"]["max_input_bytes"],
            memory_provider=(
                lambda query: app.memories.search(
                    query,
                    namespace=bindings.get("namespace", "default"),
                    ontology_id=ontology_id,
                    version=version,
                )
            )
            if bindings.get("use_memory")
            else None,
        )
    else:
        raise HarnessError("invalid_planner", "Planner must be scripted or llm")
    return AgentRuntime(
        planner,
        tools,
        budget
        or Budget(
            max_model_calls=app.config["budget"]["max_model_calls"],
            timeout_seconds=app.config["budget"]["timeout_seconds"],
        ),
        store=app.runs,
        run_id=run_id,
        backend=backend,
        config_hash=app.execution_hash,
        ontology_id=ontology_id,
        base_version=version,
        bindings=bindings,
    )


async def run_agent(
    app: "Application",
    ontology_id: str,
    message: str,
    *,
    inputs: list[Path] | None = None,
    planner: str = "scripted",
    backend: str = "local",
    use_memory: bool = False,
    namespace: str = "default",
    version: str | None = None,
) -> dict[str, Any]:
    if not message.strip():
        raise HarnessError("invalid_request", "Agent message must not be empty")
    snapshot = app.ontologies.read(ontology_id, version)
    child_run_id = (
        app.create_build(
            ontology_id,
            inputs,
            version=snapshot["version"],
            use_memory=use_memory,
            namespace=namespace,
        )
        if inputs
        else None
    )
    bindings = {
        "planner": planner,
        "child_run_id": child_run_id,
        "use_memory": use_memory,
        "namespace": namespace,
    }
    runtime = make_runtime(
        app, ontology_id, snapshot["version"], message, bindings, backend=backend
    )
    await runtime.run(message)
    return app.inspect(runtime.run_id)


async def resume_agent(
    app: "Application", state: dict[str, Any], *, answer: str | None = None
) -> dict[str, Any]:
    if state["config_hash"] != app.execution_hash:
        raise HarnessError("resume_incompatible", "Application configuration changed")
    runtime = make_runtime(
        app,
        state["ontology_id"],
        state["base_version"],
        state["message"],
        state["bindings"],
        run_id=state["run_id"],
        budget=Budget(**state["budget"]),
        backend=state["backend"],
    )
    await runtime.run(state["message"], answer=answer)
    return app.inspect(state["run_id"])
