"""Explicit small live-provider smoke check using only the original demo texts."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from ob_harness.agent.presets import run_agent
from ob_harness.common import normalized, safe_error
from ob_harness.services import Application
from ob_harness.services.application import initialize


async def check(args):
    if args.dotenv:
        for line in args.dotenv.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line.startswith("DEEPSEEK_API_KEY="):
                os.environ["DEEPSEEK_API_KEY"] = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise ValueError("Set DEEPSEEK_API_KEY or explicitly pass --dotenv")
    initialize(args.workspace, writable=True)
    path = args.workspace / "harness.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["model"] = {
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com",
        "model": args.model,
        "api_key_env": "DEEPSEEK_API_KEY",
        "timeout": 60,
        "retries": 0,
        "max_output_tokens": 1536,
        "json_mode": True,
        "thinking": {"type": "disabled"},
    }
    config["budget"] = {"max_model_calls": 4, "timeout_seconds": 180}
    config["tracing"]["save_raw"] = True
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    app = Application(args.workspace)
    state = await app.build(
        "demo", [args.workspace / "examples/part-1.txt", args.workspace / "examples/part-2.txt"]
    )
    report = {
        "workspace": str(args.workspace.resolve()),
        "model": args.model,
        "build_status": state["status"],
        "run_id": state["run_id"],
        "model_calls": state["model_calls"],
        "usage": state["usage"],
        "error": state.get("error"),
        "result": state.get("result"),
        "traces": state["traces"],
    }
    try:
        if state["status"] != "completed":
            raise ValueError("Live build must accept both demo chunks")
        value = state["result"]
        report["commit"] = app.ontologies.commit(
            value["changeset_id"],
            expected_hash=value["changeset_hash"],
            revision=1,
            idempotency_key="live-smoke",
        )
        ontology = app.ontologies.read("demo")["ontology"]
        names = {item["id"]: normalized(item["name"]) for item in ontology["entities"]}
        edges = {
            (names[edge["source"]], edge["type"], names[edge["target"]])
            for edge in ontology["relations"]
        }
        report["concepts"] = list(names.values())
        report["expected_hierarchy_present"] = {
            ("whale", "is_a", "mammal"),
            ("blue whale", "is_a", "whale"),
        } <= edges
        if not report["expected_hierarchy_present"]:
            raise ValueError("Live output omitted the demo's expected hierarchy")
        second_trace = app.db.read_artifact(state["traces"][1])
        report["second_chunk_workspace_revision"] = second_trace["workspace_revision"]
        if args.planner:
            agent = await run_agent(
                app,
                "demo",
                "Look up the Whale concept and return its ontology references.",
                planner="llm",
            )
            report["agent"] = {
                key: agent.get(key)
                for key in ("status", "run_id", "model_calls", "usage", "result", "error")
            }
            if agent["status"] != "completed":
                raise ValueError("Live LLM Planner did not complete the query")
        report["passed"] = True
    except Exception as error:
        report["passed"] = False
        report["check_error"] = safe_error(error)
    (args.workspace / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace", type=Path, required=True, help="Fresh workspace for original sample data"
    )
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--dotenv", type=Path, help="Explicitly read only the DeepSeek key from this file"
    )
    parser.add_argument(
        "--planner", action="store_true", help="Also perform live planning requests"
    )
    raise SystemExit(asyncio.run(check(parser.parse_args())))
