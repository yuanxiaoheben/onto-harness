import argparse
import asyncio
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from .common import HarnessError, safe_error
from .config import load_config
from .ontology import load_ontology
from .pipeline import extract_prepared, prepare_run
from .services.cli import add_parsers as add_local_parsers
from .services.cli import dispatch as dispatch_local
from .tools.cli import add_parsers as add_tool_parsers
from .tools.cli import dispatch as dispatch_tool


def main(argv: list[str] | None = None) -> int:
    # Windows consoles can otherwise reject Chinese ontology names and paths.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Independent local Ontology Harness")
    commands = parser.add_subparsers(dest="command", required=True)
    add_tool_parsers(commands)
    add_local_parsers(commands)
    for name, help_text in (
        ("ontology-info", "Inspect ontology structure"),
        ("prepare", "Read and chunk documents without invoking the harness"),
        ("extract", "Run legacy per-chunk extraction with configured components"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--config", type=Path)
        if name != "ontology-info":
            command.add_argument(
                "--input",
                type=Path,
                nargs="+",
                required=True,
                help="One or more document files/directories",
            )
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            try:
                from .web.server import run_server
            except ImportError as error:
                raise HarnessError(
                    "web_dependency_missing",
                    'Install the web extra first: python -m pip install -e ".[web]"',
                ) from error

            run_server(
                args.workspace,
                args.ontology,
                version=args.version,
                planner=args.planner,
                host=args.host,
                port=args.port,
                log_level=args.log_level,
                editable=args.editable,
                create_ontology=args.create_ontology,
            )
            return 0
        if args.command == "chat" and args.message is None:
            from .conversations.cli import interactive_chat

            asyncio.run(interactive_chat(args))
            return 0
        if getattr(args, "local_command", False):
            result = asyncio.run(dispatch_local(args))
            print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
            if (
                args.command == "chat"
                and isinstance(result, dict)
                and result.get("result", {}).get("review")
            ):
                from .services.application import Application

                Application(args.workspace).conversations.present(
                    result["conversation_id"], result["result"]["review"]["id"]
                )
            if isinstance(result, dict) and result.get("status") in {
                "failed",
                "cancelled",
                "budget_exhausted",
                "completed_with_errors",
            }:
                return 2
            return 0
        if args.command.startswith("tool-"):
            print(json.dumps(dispatch_tool(args), ensure_ascii=False, indent=2))
            return 0
        config = load_config(args.config)
        if args.command == "ontology-info":
            ontology = load_ontology(config.ontology_path)
            print(
                json.dumps(
                    {
                        "path": str(ontology.path),
                        "sha256": ontology.sha256,
                        "metadata": ontology.raw.get("metadata", {}),
                        "entities": len(ontology.entities_by_id),
                        "relations": len(ontology.relations_by_id),
                        "entity_types": dict(
                            Counter(item["type"] for item in ontology.entities_by_id.values())
                        ),
                        "relation_types": dict(
                            Counter(item["type"] for item in ontology.relations_by_id.values())
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        prepared = prepare_run(config, args.input)
        print(f"Run directory: {prepared.directory}", flush=True)
        if args.command == "extract":
            extract_prepared(config, prepared)
        print(f"Status: {prepared.manifest['status']}; chunks: {len(prepared.chunks)}")
        return 2 if prepared.manifest["invalid_chunks"] else 0
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
        ImportError,
        sqlite3.Error,
    ) as error:
        detail = error.as_dict() if isinstance(error, HarnessError) else safe_error(error)
        print(json.dumps({"error": detail}, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            json.dumps({"error": {"code": "cancelled", "message": "Interrupted"}}), file=sys.stderr
        )
        return 130
