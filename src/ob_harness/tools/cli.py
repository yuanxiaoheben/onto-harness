"""Run tools directly without an ontology, planner, model provider or application server."""

import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from ..agent.contracts import ToolResult
from .registry import ToolContext, ToolRegistry


def add_parsers(commands) -> None:
    commands.add_parser("tool-list", help="List known and installed optional tools")
    info = commands.add_parser("tool-info", help="Inspect optional tool availability")
    info.add_argument("name")
    run = commands.add_parser(
        "tool-run", help="Invoke an installed tool on an explicitly bound document"
    )
    run.add_argument("name")
    run.add_argument("--input", type=Path)
    run.add_argument("--output", type=Path, default=Path("outputs/tools"))
    run.add_argument("--workspace", type=Path)
    run.add_argument("--ontology")
    run.add_argument("--version")
    run.add_argument(
        "--arguments", type=Path, help="JSON arguments; document_id defaults to document"
    )
    run.add_argument("--timeout", type=int, default=3600)


def dispatch(args) -> dict | list:
    registry = ToolRegistry()
    if args.command == "tool-list":
        return registry.list()
    if args.command == "tool-info":
        return registry.info(args.name)
    arguments = json.loads(args.arguments.read_text(encoding="utf-8-sig")) if args.arguments else {}
    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be a JSON object")
    application, child_run_id = None, None
    if args.workspace:
        from ..services.application import Application

        application = Application(args.workspace)
    if args.name == "ontology_build" and application and args.ontology and args.input:
        child_run_id = application.create_build(args.ontology, [args.input], version=args.version)
    if args.input and not args.name.startswith("ontology_"):
        arguments.setdefault("document_id", "document")
    tool = registry.bind(
        args.name,
        ToolContext(
            {"document": args.input.resolve()} if args.input else {},
            args.output.resolve(),
            args.timeout,
            application,
            args.ontology,
            args.version,
            child_run_id,
        ),
    )

    async def invoke() -> ToolResult:
        from ..state import WorkspaceLock
        from .invocation import invoke_tool

        if application:
            with WorkspaceLock(application.db.root):
                return await invoke_tool(tool, arguments)
        return await invoke_tool(tool, arguments)

    return asdict(asyncio.run(invoke()))
