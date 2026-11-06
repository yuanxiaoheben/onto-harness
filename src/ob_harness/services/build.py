import asyncio
from dataclasses import asdict
from time import monotonic
from typing import TYPE_CHECKING, Any, Awaitable

from .. import __version__
from ..common import HarnessError, digest, object_json, safe_error
from ..context import TemplateContextBuilder
from ..domain import Chunk, LLMRequest
from ..ontologies.validation import apply_operations, validate_ontology
from ..retrieval import SimpleRetriever
from ..state import WorkspaceLock
from .proposals import BUILD_CONTRACT, BUILD_PROMPT, candidate_operations

if TYPE_CHECKING:
    from .application import Application


async def controlled(
    awaitable: Awaitable[Any], app: "Application", state: dict[str, Any], remaining: float
) -> Any:
    task = asyncio.ensure_future(awaitable)
    started = monotonic()
    try:
        while not task.done():
            if app.runs.cancelled(state["run_id"]):
                raise HarnessError("cancelled", "Cancellation requested")
            if monotonic() - started >= remaining:
                raise HarnessError("deadline", "Cumulative active runtime budget exhausted")
            await asyncio.wait(
                {task}, timeout=min(0.2, max(0.01, remaining - (monotonic() - started)))
            )
        return task.result()
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


async def execute_build(
    app: "Application", run_id: str, *, locked: bool = False, clear_cancel: bool = False
) -> dict[str, Any]:
    if not locked:
        with WorkspaceLock(app.db.root):
            return await execute_build(app, run_id, locked=True, clear_cancel=clear_cancel)
    state = app.runs.get(run_id)
    if state["kind"] != "build":
        raise HarnessError("incorrect_run_kind", "This executor only resumes build runs")
    if state["status"] in {"completed", "completed_with_errors"}:
        return state
    if clear_cancel:
        with app.db.connect() as db:
            db.execute("UPDATE controls SET cancel=0 WHERE run_id=?", (run_id,))
    app.authorizer.require("read", state["ontology_id"])
    app.authorizer.require("propose", state["ontology_id"])
    if state["config_hash"] != app.execution_hash or state["package_version"] != __version__:
        raise HarnessError(
            "resume_incompatible", "Configuration, model fixtures or package version changed"
        )
    base = app.ontologies.read(state["ontology_id"], state["base_version"])
    inputs = app.db.read_artifact(state["inputs_ref"])
    documents = {
        item["id"]: (item, reference)
        for reference in inputs["documents"]
        for item in [app.db.read_artifact(reference)]
    }
    chunks = [Chunk(**item) for item in inputs["chunks"]]
    retriever, builder = (
        SimpleRetriever(app.config["retrieval"]),
        TemplateContextBuilder(app.config["context"]),
    )
    model = app.model()
    active_start, elapsed_before = monotonic(), state["elapsed_seconds"]

    def save() -> None:
        state["elapsed_seconds"] = elapsed_before + monotonic() - active_start
        app.runs.save(state)

    def attempt() -> None:
        from ..tools.invocation import model_attempt

        if app.runs.cancelled(run_id):
            raise HarnessError("cancelled", "Cancellation requested")
        if elapsed_before + monotonic() - active_start >= app.config["budget"]["timeout_seconds"]:
            raise HarnessError("deadline", "Cumulative active runtime budget exhausted")
        if state["model_calls"] >= app.config["budget"]["max_model_calls"]:
            raise HarnessError("model_call_limit", "Cumulative model call budget exhausted")
        parent_meter = model_attempt.get()
        if parent_meter:
            parent_meter()
        state["model_calls"] += 1
        state["usage"]["unknown_calls"] += 1
        save()
        app.runs.event(
            run_id, "model_attempt", {"chunk_index": state["index"], "call": state["model_calls"]}
        )

    try:
        state["status"] = "running"
        state.pop("error", None)
        save()
        app.runs.event(
            run_id,
            "build_started",
            {"chunk_index": state["index"], "base_version": state["base_version"]},
        )
        while state["index"] < len(chunks):
            if app.runs.cancelled(run_id):
                raise HarnessError("cancelled", "Cancellation requested")
            remaining = app.config["budget"]["timeout_seconds"] - (
                elapsed_before + monotonic() - active_start
            )
            if remaining <= 0:
                raise HarnessError("deadline", "Cumulative active runtime budget exhausted")
            chunk = chunks[state["index"]]
            document, document_ref = documents[chunk.document_id]
            operations = app.db.read_artifact(state["operations_ref"])
            current = apply_operations(base["ontology"], operations, app.db.read_artifact)
            operation_id = f"chunk-{state['index']}"
            if state["phase"] == "prepare":
                subgraph = retriever.search(
                    chunk.text,
                    current,
                    version=f"{state['base_version']}+draft:{state['workspace_revision']}",
                )
                memories = (
                    app.memories.search(
                        chunk.text,
                        namespace=state["memory_namespace"],
                        ontology_id=state["ontology_id"],
                        version=state["base_version"],
                    )
                    if state["use_memory"]
                    else []
                )
                pack = builder.pack(
                    "build",
                    chunk.text,
                    current,
                    base["schema"],
                    subgraph,
                    output_contract=BUILD_CONTRACT,
                    prompt=BUILD_PROMPT,
                    source={
                        "document_id": chunk.document_id,
                        "text_hash": digest(document["text"]),
                        "start": chunk.start,
                        "end": chunk.end,
                        "base_version": state["base_version"],
                        "workspace_revision": state["workspace_revision"],
                    },
                    memories=memories,
                )
                request = LLMRequest(
                    pack.messages, {"task": "build", "input_hash": digest(chunk.text)}
                )
                state["context_ref"] = app.db.artifact(asdict(pack))
                state["request_ref"] = app.db.artifact(asdict(request))
                state["phase"] = "model"
                save()
            if state["phase"] == "model":
                request = LLMRequest(**app.db.read_artifact(state["request_ref"]))
                prior = app.runs.operation(run_id, operation_id, digest(asdict(request)))
                if prior and prior["status"] == "done":
                    state["response_ref"] = prior["value"]["response_ref"]
                    state["phase"] = "accept"
                    save()
                else:
                    response = await controlled(
                        model.agenerate(request, on_attempt=attempt), app, state, remaining
                    )
                    from ..tools.invocation import model_response

                    parent_response = model_response.get()
                    if parent_response:
                        parent_response(response)
                    state["response_ref"] = app.db.artifact(asdict(response))
                    state["phase"] = "accept"
                    if (
                        response.usage.get("input_tokens") is not None
                        and response.usage.get("output_tokens") is not None
                    ):
                        state["usage"]["input_tokens"] += response.usage["input_tokens"]
                        state["usage"]["output_tokens"] += response.usage["output_tokens"]
                        state["usage"]["unknown_calls"] -= 1
                    state["elapsed_seconds"] = elapsed_before + monotonic() - active_start
                    app.runs.complete_operation(
                        state, operation_id, {"response_ref": state["response_ref"]}
                    )
            if state["phase"] == "accept":
                response = app.db.read_artifact(state["response_ref"])
                trace: dict[str, Any] = {
                    "schema_version": 1,
                    "chunk_id": chunk.id,
                    "workspace_revision": state["workspace_revision"],
                    "context_ref": state["context_ref"],
                    "request_ref": state["request_ref"]
                    if app.config["tracing"]["save_raw"]
                    else None,
                    "response_ref": state["response_ref"]
                    if app.config["tracing"]["save_raw"]
                    else None,
                    "model": response["model"],
                    "usage": response["usage"],
                    "metadata": response["metadata"],
                }
                try:
                    proposed, issues = candidate_operations(
                        object_json(response["text"]),
                        current,
                        chunk,
                        document_ref,
                        document["text"],
                    )
                    candidate = apply_operations(current, proposed, app.db.read_artifact)
                    report = validate_ontology(candidate, base["schema"])
                    if not report["passed"]:
                        raise HarnessError(
                            "invalid_ontology_proposal",
                            "Proposed changes violate ontology structural constraints",
                        )
                    operations.extend(proposed)
                    state["operations_ref"] = app.db.artifact(operations)
                    state["workspace_revision"] += 1
                    trace.update(
                        status="accepted",
                        operation_count=len(proposed),
                        validation=report,
                        issues=issues,
                    )
                except HarnessError as error:
                    state["invalid_chunks"] += 1
                    trace.update(status="invalid", error=error.as_dict())
                state["traces"].append(app.db.artifact(trace))
                state["index"] += 1
                state["phase"] = "prepare"
                for key in ("context_ref", "request_ref", "response_ref"):
                    state.pop(key, None)
                save()
                app.runs.event(
                    run_id,
                    "chunk_finished",
                    {
                        "chunk_id": chunk.id,
                        "status": trace["status"],
                        "workspace_revision": state["workspace_revision"],
                    },
                )
        proposal = app.ontologies.propose(
            state["ontology_id"],
            state["base_version"],
            app.db.read_artifact(state["operations_ref"]),
            run_id=run_id,
        )
        state["result"] = {
            "schema_version": 1,
            "kind": "ontology_build",
            "run_id": run_id,
            "ontology_id": state["ontology_id"],
            "base_version": state["base_version"],
            "changeset_id": proposal["id"],
            "changeset_hash": proposal["hash"],
            "revision": proposal["revision"],
            "validation": proposal["validation"],
            "chunk_count": len(chunks),
            "invalid_chunks": state["invalid_chunks"],
            "replay": app.config["model"]["provider"] == "replay",
        }
        state["status"] = "completed_with_errors" if state["invalid_chunks"] else "completed"
    except asyncio.CancelledError:
        state["status"], state["error"] = (
            "cancelled",
            {"code": "cancelled", "message": "Executor cancelled", "retryable": True},
        )
        raise
    except Exception as error:
        state["error"] = safe_error(error)
        code = state["error"]["code"]
        state["status"] = (
            "cancelled"
            if code == "cancelled"
            else ("budget_exhausted" if code in {"deadline", "model_call_limit"} else "failed")
        )
    finally:
        save()
        app.runs.event(
            run_id,
            "run_stopped",
            {
                "status": state["status"],
                "elapsed_seconds": state["elapsed_seconds"],
                "model_calls": state["model_calls"],
            },
        )
    return state
