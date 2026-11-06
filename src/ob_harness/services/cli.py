"""CLI adapter for the same application services exposed to Python callers."""

import json
from pathlib import Path
from typing import Any

from ..common import HarnessError
from .application import Application, initialize


def add_parsers(commands) -> None:
    chat = commands.add_parser(
        "chat", help="Construct an ontology through a persistent conversation"
    )
    chat.set_defaults(local_command=True)
    chat.add_argument("--workspace", type=Path, default=Path("."))
    chat.add_argument("--ontology", required=True)
    chat.add_argument("--conversation")
    chat.add_argument(
        "--message", help="Send one message and return JSON instead of an interactive prompt"
    )
    chat.add_argument("--message-id", help="Stable idempotency key for retrying a single message")
    chat.add_argument("--planner", choices=("scripted", "llm"), default="scripted")
    chat.add_argument("--new-ontology", action="store_true")
    init = commands.add_parser(
        "init", help="Create a standalone local workspace and replay examples"
    )
    init.add_argument("directory", type=Path)
    init.add_argument(
        "--writable",
        action="store_true",
        help="Explicitly grant proposal and commit permission for demo",
    )
    init.set_defaults(local_command=True)
    for name in ("query", "context", "build", "agent", "import-ontology", "export"):
        parser = commands.add_parser(name, help=f"Local ontology {name}")
        parser.set_defaults(local_command=True)
        parser.add_argument("--workspace", type=Path, default=Path("."))
        parser.add_argument("--ontology", required=True)
        parser.add_argument("--version")
        if name in {"query", "context"}:
            parser.add_argument("--query", required=True)
        if name in {"context", "build", "agent"}:
            parser.add_argument("--memory", action="store_true")
            parser.add_argument("--namespace", default="default")
        if name in {"build", "agent", "import-ontology"}:
            parser.add_argument(
                "--input",
                type=Path,
                nargs="+" if name in {"build", "agent"} else None,
                required=name != "agent",
            )
        if name == "build":
            parser.add_argument("--prepare-only", action="store_true")
        if name == "agent":
            parser.add_argument("--message", required=True)
            parser.add_argument("--planner", choices=("scripted", "llm"), default="scripted")
            parser.add_argument("--backend", choices=("local", "langgraph"), default="local")
        if name == "import-ontology":
            parser.add_argument("--schema", type=Path, required=True)
        if name == "export":
            parser.add_argument("--output", type=Path, required=True)
    for name in ("inspect", "resume", "cancel", "resolve", "validate", "diff", "commit"):
        parser = commands.add_parser(name, help=f"Local {name}")
        parser.set_defaults(local_command=True)
        parser.add_argument("identifier")
        parser.add_argument("--workspace", type=Path, default=Path("."))
        if name == "inspect":
            parser.add_argument("--events", action="store_true")
            parser.add_argument("--trace", type=int)
        if name == "resume":
            parser.add_argument("--answer")
        if name == "resolve":
            parser.add_argument(
                "--result",
                type=Path,
                required=True,
                help="Inspected ToolResult JSON, no automatic re-execution",
            )
        if name == "commit":
            parser.add_argument("--expected-hash", required=True)
            parser.add_argument("--revision", type=int, default=1)
            parser.add_argument("--key", required=True)
    memory = commands.add_parser("memory", help="Explicit owner-scoped long-term experience")
    memory.set_defaults(local_command=True)
    memory.add_argument("action", choices=("put", "get", "search", "delete", "clear"))
    memory.add_argument("--workspace", type=Path, default=Path("."))
    memory.add_argument("--namespace", default="default")
    memory.add_argument("--content")
    memory.add_argument("--id")
    memory.add_argument("--query", default="")
    memory.add_argument(
        "--kind", default="experience", choices=("experience", "preference", "correction")
    )
    memory.add_argument("--source-run")
    memory.add_argument("--ontology")
    memory.add_argument("--version")
    memory.add_argument("--expires-at")
    memory.add_argument("--top-k", type=int, default=5)
    for name in ("doctor", "cleanup", "audit", "runs"):
        parser = commands.add_parser(name)
        parser.set_defaults(local_command=True)
        parser.add_argument("--workspace", type=Path, default=Path("."))
        if name == "doctor":
            parser.add_argument(
                "--probe-model",
                action="store_true",
                help="Explicitly perform a billable model request",
            )
        if name == "cleanup":
            parser.add_argument("--older-than-days", type=int, default=30)
            parser.add_argument(
                "--apply", action="store_true", help="Apply the retention plan; default is dry run"
            )
        if name in {"audit", "runs"}:
            parser.add_argument("--limit", type=int, default=100)
    serve = commands.add_parser("serve", help="Run the local FastAPI chat UI")
    serve.set_defaults(local_command=True)
    serve.add_argument("--workspace", type=Path, default=Path("."))
    serve.add_argument("--ontology", required=True)
    serve.add_argument("--version")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--planner", choices=("scripted", "llm"), default="scripted")
    serve.add_argument(
        "--editable",
        action="store_true",
        help="Enable persistent conversation drafts and confirmed writes",
    )
    serve.add_argument(
        "--create-ontology",
        action="store_true",
        help="Start from an unpublished empty draft (requires --editable)",
    )
    serve.add_argument(
        "--log-level",
        choices=("critical", "error", "warning", "info", "debug", "trace"),
        default="info",
    )


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


async def dispatch(args) -> dict | list:
    if args.command == "init":
        return initialize(args.directory, writable=args.writable)
    app = Application(args.workspace)
    command = args.command
    if command == "chat":
        from uuid import uuid4

        service = app.conversations
        conv = (
            service.get(args.conversation)
            if args.conversation
            else service.create(
                ontology_id=args.ontology, create_spec={} if args.new_ontology else None
            )
        )
        if conv["ontology_id"] != args.ontology:
            raise HarnessError("resource_not_bound", "Conversation belongs to another ontology")
        if args.message is None:
            raise HarnessError(
                "invalid_arguments", "Interactive chat is handled by the CLI entry point"
            )
        return await service.send(
            conv["id"],
            text=args.message,
            client_message_id=args.message_id or uuid4().hex,
            expected_revision=conv["revision"],
            planner=args.planner,
        )
    if command == "query":
        return app.answer(args.ontology, args.query, version=args.version)
    if command == "context":
        return app.context(
            args.ontology,
            args.query,
            version=args.version,
            use_memory=args.memory,
            namespace=args.namespace,
        )
    if command == "import-ontology":
        payload = read_json(args.input)
        raw = payload.get("ontology", payload) if isinstance(payload, dict) else payload
        return app.ontologies.import_ontology(args.ontology, raw, read_json(args.schema))
    if command == "export":
        snapshot = app.ontologies.read(args.ontology, args.version)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(snapshot, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        return {
            "path": str(args.output.resolve()),
            "ontology_id": args.ontology,
            "version": snapshot["version"],
        }
    if command == "build":
        run_id = app.create_build(
            args.ontology,
            args.input,
            version=args.version,
            use_memory=args.memory,
            namespace=args.namespace,
        )
        if args.prepare_only:
            return app.inspect(run_id)
        return await app.resume(run_id)
    if command == "agent":
        from ..agent.presets import run_agent

        return await run_agent(
            app,
            args.ontology,
            args.message,
            inputs=args.input,
            planner=args.planner,
            backend=args.backend,
            use_memory=args.memory,
            namespace=args.namespace,
            version=args.version,
        )
    if command == "inspect":
        state = app.inspect(args.identifier)
        if args.events:
            return app.runs.events(args.identifier)
        if args.trace is not None:
            if state["kind"] != "build" or not 0 <= args.trace < len(state["traces"]):
                raise HarnessError("not_found", "Completed chunk trace not found")
            trace = app.db.read_artifact(state["traces"][args.trace])
            return {**trace, "context": app.db.read_artifact(trace["context_ref"])}
        return state
    if command == "resume":
        return await app.resume(args.identifier, answer=args.answer)
    if command == "cancel":
        app.inspect(args.identifier)
        app.runs.cancel(args.identifier)
        return {"run_id": args.identifier, "cancel_requested": True}
    if command == "resolve":
        from .maintenance import resolve_operation

        return resolve_operation(app, args.identifier, read_json(args.result))
    if command == "validate":
        return app.ontologies.validate(args.identifier)
    if command == "diff":
        return app.ontologies.diff(args.identifier)
    if command == "commit":
        return app.ontologies.commit(
            args.identifier,
            expected_hash=args.expected_hash,
            revision=args.revision,
            idempotency_key=args.key,
        )
    if command == "memory":
        if args.action == "put":
            return app.memories.put(
                args.content,
                namespace=args.namespace,
                kind=args.kind,
                source_run=args.source_run,
                ontology_id=args.ontology,
                version=args.version,
                expires_at=args.expires_at,
            )
        if args.action in {"get", "delete"}:
            if not args.id:
                raise HarnessError("invalid_arguments", "memory get/delete requires --id")
            if args.action == "get":
                return app.memories.get(args.id, namespace=args.namespace)
            app.memories.delete(args.id, namespace=args.namespace)
            return {"deleted": args.id}
        if args.action == "clear":
            return {"deleted": app.memories.clear(namespace=args.namespace)}
        return app.memories.search(
            args.query,
            namespace=args.namespace,
            top_k=args.top_k,
            ontology_id=args.ontology,
            version=args.version,
        )
    if command == "doctor":
        result = app.doctor()
        if args.probe_model:
            from ..domain import LLMRequest

            if app.config["model"]["provider"] == "replay":
                app.model()
                result["fixture_loaded"] = True
            else:
                response = await app.model().agenerate(
                    LLMRequest([{"role": "user", "content": 'Return JSON: {"ok":true}'}])
                )
                result["model_probe"] = {
                    "model": response.model,
                    "usage": response.usage,
                    "metadata": response.metadata,
                }
                result["network_checked"] = result["model_inference_checked"] = True
                app.authorizer.record(
                    "model_probe", "configured_endpoint", {"model": response.model}
                )
        return result
    if command == "cleanup":
        from .maintenance import cleanup

        return cleanup(app, older_than_days=args.older_than_days, apply=args.apply)
    if command == "runs":
        return app.list_runs(args.limit)
    if command == "audit":
        from ..common import positive_int

        positive_int(args.limit, "audit limit", maximum=1000)
        with app.db.connect() as db:
            return [
                {**dict(row), "details": json.loads(row["details"])}
                for row in db.execute(
                    "SELECT * FROM audit WHERE subject=? ORDER BY sequence DESC LIMIT ?",
                    (app.authorizer.principal.subject, args.limit),
                )
            ]
    raise HarnessError("unknown_command", "No handler for command")
