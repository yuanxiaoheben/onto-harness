"""Local HTTP, subprocess, and recovery integration checks; never calls a real API."""

import asyncio
import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from ob_harness.agent import AgentDecision, AgentRuntime, Tool
from ob_harness.agent.planner import ScriptedPlanner
from ob_harness.agent.presets import run_agent
from ob_harness.common import HarnessError, canonical, digest
from ob_harness.context import TemplateContextBuilder
from ob_harness.domain import Chunk, LLMRequest
from ob_harness.models import ChatModel
from ob_harness.retrieval import SimpleRetriever
from ob_harness.services import Application
from ob_harness.services.application import initialize
from ob_harness.services.maintenance import cleanup, resolve_operation
from ob_harness.services.proposals import candidate_operations


def completion(content):
    return {
        "model": "local-test-model",
        "choices": [{"message": {"content": canonical(content)}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 17, "completion_tokens": 9, "total_tokens": 26},
    }


@contextmanager
def endpoint(callback):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.respond({})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            self.respond(body)

        def respond(self, body):
            requests.append({"path": self.path, "body": body})
            status, payload, headers = callback(body, len(requests))
            encoded = canonical(payload).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(encoded)
            except OSError:
                pass  # Expected when a test terminates an in-flight client.

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


class IntegrationChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "workspace"
        initialize(self.root, writable=True)
        self.app = Application(self.root)

    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 5

    def tearDown(self):
        self.temporary.cleanup()

    def use_endpoint(self, url):
        path = self.root / "harness.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["model"] = {
            "provider": "local",
            "base_url": url,
            "model": "local-test-model",
            "timeout": 10,
            "retries": 0,
        }
        path.write_text(json.dumps(config), encoding="utf-8")
        self.app = Application(self.root)

    def replies(self):
        fixtures = json.loads((self.root / "examples/responses.json").read_text(encoding="utf-8"))
        return {
            item["match"]["input_hash"]: item["response"]
            for item in fixtures
            if item["match"]["task"] == "build"
        }

    async def test_http_backend_constructs_ontology_and_plans_terminal_tool(self):
        replies = self.replies()

        def respond(body, _count):
            data = json.loads(body["messages"][-1]["content"])
            if "decision_schema" in data:
                if data["observations"]:
                    summary = data["observations"][0]["summary"]
                    self.assertIn("Mammal", {item["name"] for item in summary["concepts"]})
                    self.assertEqual(summary["query"], "mammal")
                payload = {
                    "action": "tool",
                    "tool": "ontology_answer" if data["observations"] else "ontology_search",
                    "arguments": {"query": "mammal"},
                    "question": "",
                }
            else:
                payload = copy.deepcopy(replies[digest(data["input"]["text"])])
                for item in [*payload["concepts"], *payload["relations"]]:
                    item["evidence"] = [{"quote": span["quote"]} for span in item["evidence"]]
            return 200, completion(payload), {}

        with endpoint(respond) as (url, requests):
            self.use_endpoint(url)
            state = await self.app.build(
                "demo", [self.root / "examples/part-1.txt", self.root / "examples/part-2.txt"]
            )
            self.assertEqual(state["status"], "completed", state.get("error"))
            self.assertEqual(
                state["usage"], {"input_tokens": 34, "output_tokens": 18, "unknown_calls": 0}
            )
            agent = await run_agent(self.app, "demo", "Look up mammal", planner="llm")
            self.assertEqual(agent["status"], "completed", agent.get("error"))
            self.assertEqual(agent["model_calls"], 2)
            self.assertEqual(agent["result"]["kind"], "ontology_answer")
            self.assertEqual(len(requests), 4)

    async def test_http_retry_is_metered_and_auth_errors_are_redacted(self):
        with endpoint(
            lambda _body, count: (
                (429, {"error": "busy"}, {}) if count == 1 else (200, completion({"ok": True}), {})
            )
        ) as (url, requests):
            model = ChatModel(
                {"provider": "local", "base_url": url, "model": "local-test-model", "retries": 1}
            )
            attempts = []
            result = await model.agenerate(
                LLMRequest([{"role": "user", "content": "Return JSON"}]),
                on_attempt=lambda: attempts.append(1),
            )
            self.assertEqual(result.metadata["attempts"], 2)
            self.assertEqual(len(attempts), len(requests))
        with endpoint(lambda *_args: (401, {"error": "private-test-token"}, {})) as (url, requests):
            model = ChatModel(
                {
                    "provider": "api",
                    "base_url": url,
                    "model": "local-test-model",
                    "api_key_env": "HARNESS_TEST_TOKEN",
                    "retries": 2,
                }
            )
            with patch.dict(os.environ, {"HARNESS_TEST_TOKEN": "private-test-token"}):
                with self.assertRaises(HarnessError) as rejected:
                    await model.agenerate(LLMRequest([{"role": "user", "content": "Return JSON"}]))
            self.assertEqual(rejected.exception.code, "model_authentication")
            self.assertNotIn("private-test-token", canonical(rejected.exception.as_dict()))
            self.assertEqual(len(requests), 1)

    async def test_http_redirect_is_never_followed(self):
        with endpoint(lambda *_args: (302, {}, {"Location": "/should-not-follow"})) as (
            url,
            requests,
        ):
            model = ChatModel(
                {"provider": "local", "base_url": url, "model": "local-test-model", "retries": 0}
            )
            with self.assertRaises(HarnessError) as rejected:
                await model.agenerate(LLMRequest([{"role": "user", "content": "Return JSON"}]))
            self.assertEqual(rejected.exception.code, "model_http_error")
            self.assertEqual(len(requests), 1)

    async def test_real_process_interruption_resumes_after_last_accepted_chunk(self):
        replies = self.replies()
        second_started, release = threading.Event(), threading.Event()

        def respond(body, count):
            text = json.loads(body["messages"][-1]["content"])["input"]["text"]
            if count == 2:
                second_started.set()
                release.wait(20)
            return 200, completion(replies[digest(text)]), {}

        with endpoint(respond) as (url, requests):
            self.use_endpoint(url)
            run_id = self.app.create_build(
                "demo", [self.root / "examples/part-1.txt", self.root / "examples/part-2.txt"]
            )
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "ob_harness",
                    "resume",
                    run_id,
                    "--workspace",
                    str(self.root),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            try:
                self.assertTrue(
                    await asyncio.to_thread(second_started.wait, 15),
                    "Child did not reach its second model request",
                )
                process.kill()
                await asyncio.to_thread(process.communicate, timeout=5)
                checkpoint = Application(self.root).inspect(run_id)
                self.assertEqual(checkpoint["index"], 1)
                self.assertEqual(checkpoint["model_calls"], 2)
                release.set()
                restored = await Application(self.root).resume(run_id)
                self.assertEqual(restored["status"], "completed", restored.get("error"))
                self.assertEqual(restored["model_calls"], 3)
                self.assertEqual(restored["usage"]["unknown_calls"], 1)
                requested_texts = [
                    json.loads(item["body"]["messages"][-1]["content"])["input"]["text"]
                    for item in requests
                ]
                self.assertEqual(
                    requested_texts.count((self.root / "examples/part-1.txt").read_text()), 1
                )
                self.assertEqual(len(requests), 3)
            finally:
                release.set()
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)

    async def test_cancel_during_model_wait_preserves_checkpoint_for_resume(self):
        replies = self.replies()
        started, release = threading.Event(), threading.Event()

        def respond(body, count):
            text = json.loads(body["messages"][-1]["content"])["input"]["text"]
            if count == 1:
                started.set()
                release.wait(15)
            return 200, completion(replies[digest(text)]), {}

        with endpoint(respond) as (url, requests):
            self.use_endpoint(url)
            run_id = self.app.create_build("demo", [self.root / "examples/part-1.txt"])
            running = asyncio.create_task(self.app.resume(run_id))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 5))
                self.app.runs.cancel(run_id)
                stopped = await asyncio.wait_for(running, 5)
                self.assertEqual(stopped["status"], "cancelled")
                self.assertEqual(stopped["model_calls"], 1)
                release.set()
                restored = await Application(self.root).resume(run_id)
                self.assertEqual(restored["status"], "completed")
                self.assertEqual(restored["model_calls"], 2)
                self.assertEqual(len(requests), 2)
            finally:
                release.set()
                if not running.done():
                    running.cancel()
                    await asyncio.gather(running, return_exceptions=True)

    async def test_reviewed_result_resumes_without_repeating_external_side_effect(self):
        calls = []

        async def external(_arguments):
            calls.append(1)
            raise RuntimeError("reply lost after external execution")

        tool = Tool(
            "external",
            "External action",
            {"type": "object"},
            external,
            terminal=True,
            output_schema={
                "type": "object",
                "required": ["ok"],
                "properties": {"ok": {"type": "boolean"}},
            },
        )
        planner = ScriptedPlanner([AgentDecision("tool", "external", {})])
        first = AgentRuntime(planner, [tool], store=self.app.runs, ontology_id="demo")
        failed = await first.run("execute")
        waiting = await AgentRuntime(
            planner, [tool], store=self.app.runs, run_id=failed.run_id, ontology_id="demo"
        ).run("execute")
        self.assertEqual(waiting.status, "waiting_review")
        resolve_operation(
            self.app, failed.run_id, {"payload": {"ok": True}, "summary": {"verified": True}}
        )
        restored = await AgentRuntime(
            planner, [tool], store=self.app.runs, run_id=failed.run_id, ontology_id="demo"
        ).run("execute")
        self.assertEqual(restored.status, "completed")
        self.assertEqual(restored.payload, {"ok": True})
        self.assertEqual(calls, [1])

    async def test_cleanup_keeps_provenance_and_collects_only_old_orphans(self):
        queried = await run_agent(self.app, "demo", "mammal")
        built = await self.app.build("demo", [self.root / "examples/part-1.txt"])
        orphan = self.app.db.artifact({"unused": "temporary data"})
        with self.app.db.connect() as db:
            db.execute(
                "UPDATE runs SET updated=? WHERE id IN (?,?)",
                ("2000-01-01T00:00:00+00:00", queried["run_id"], built["run_id"]),
            )
            db.execute(
                "UPDATE artifact_pins SET touched=? WHERE hash=?",
                (time.time() - 40 * 86400, orphan),
            )
        plan = cleanup(self.app, older_than_days=30)
        self.assertIn(queried["run_id"], plan["run_ids"])
        self.assertNotIn(built["run_id"], plan["run_ids"])
        self.assertEqual(self.app.inspect(queried["run_id"])["status"], "completed")
        applied = cleanup(self.app, older_than_days=30, apply=True)
        self.assertFalse(applied["dry_run"])
        with self.assertRaises(HarnessError):
            self.app.inspect(queried["run_id"])
        with self.assertRaises(HarnessError):
            self.app.db.read_artifact(orphan)
        self.assertTrue(self.app.ontologies.read("demo")["ontology"]["entities"])
        self.assertEqual(self.app.inspect(built["run_id"])["status"], "completed")

    async def test_parent_agent_meters_and_restores_child_build(self):
        backends = ["local"] + (["langgraph"] if importlib.util.find_spec("langgraph") else [])
        for backend in backends:
            with self.subTest(backend=backend):
                state = await run_agent(
                    self.app,
                    "demo",
                    "Build a draft",
                    inputs=[self.root / "examples/part-1.txt", self.root / "examples/part-2.txt"],
                    backend=backend,
                )
                self.assertEqual(state["status"], "completed", state.get("error"))
                child = self.app.inspect(state["bindings"]["child_run_id"])
                self.assertEqual(state["model_calls"], child["model_calls"])
                self.assertEqual(state["model_calls"], 2)
                restored = await Application(self.root).resume(state["run_id"])
                self.assertEqual(restored["model_calls"], 2)

    async def test_cli_workflow_uses_installed_resources_from_unrelated_directory(self):
        outside = Path(self.temporary.name)
        workspace = outside / "ontology demo"
        environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}

        def cli(*args):
            process = subprocess.run(
                [sys.executable, "-m", "ob_harness", *map(str, args)],
                cwd=outside,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            return json.loads(process.stdout)

        info = await asyncio.to_thread(cli, "ontology-info")
        self.assertEqual(info["entities"], 2)
        await asyncio.to_thread(cli, "init", workspace, "--writable")
        prepared = await asyncio.to_thread(
            cli,
            "build",
            "--workspace",
            workspace,
            "--ontology",
            "demo",
            "--input",
            workspace / "examples/part-1.txt",
            workspace / "examples/part-2.txt",
            "--prepare-only",
        )
        built = await asyncio.to_thread(cli, "resume", prepared["run_id"], "--workspace", workspace)
        self.assertEqual(built["status"], "completed")
        trace = await asyncio.to_thread(
            cli, "inspect", built["run_id"], "--workspace", workspace, "--trace", "1"
        )
        self.assertEqual(trace["workspace_revision"], 1)
        value = built["result"]
        validation = await asyncio.to_thread(
            cli, "validate", value["changeset_id"], "--workspace", workspace
        )
        self.assertTrue(validation["passed"])
        await asyncio.to_thread(
            cli,
            "commit",
            value["changeset_id"],
            "--workspace",
            workspace,
            "--expected-hash",
            value["changeset_hash"],
            "--key",
            "cli-smoke",
        )
        queried = await asyncio.to_thread(
            cli, "query", "--workspace", workspace, "--ontology", "demo", "--query", "whale"
        )
        self.assertIn("Whale", {item["name"] for item in queried["concepts"]})
        saved = await asyncio.to_thread(
            cli, "memory", "put", "--workspace", workspace, "--content", "Verify whale hierarchy"
        )
        memories = await asyncio.to_thread(
            cli, "memory", "search", "--workspace", workspace, "--query", "whale"
        )
        self.assertEqual(memories[0]["id"], saved["id"])
        context = await asyncio.to_thread(
            cli,
            "context",
            "--workspace",
            workspace,
            "--ontology",
            "demo",
            "--query",
            "whale",
            "--memory",
        )
        self.assertIn(saved["id"], context["metadata"]["selected_memory_ids"])
        await asyncio.to_thread(
            cli, "memory", "delete", "--workspace", workspace, "--id", saved["id"]
        )
        plan = await asyncio.to_thread(cli, "cleanup", "--workspace", workspace)
        self.assertTrue(plan["dry_run"])

    def test_quote_anchoring_preserves_exact_source_and_rejects_ambiguity(self):
        def propose(text, evidence, *, start=0):
            payload = {
                "concepts": [
                    {"id": "new", "name": "Whale", "type": "concept", "evidence": [evidence]}
                ],
                "relations": [],
            }
            return candidate_operations(
                payload,
                {"entities": [], "relations": []},
                Chunk("chunk", "document", text, start, start + len(text)),
                "document-ref",
                " " * start + text,
            )

        operations, issues = propose(
            "A whale is a mammal.", {"quote": "whale", "start": 0, "end": 8}, start=20
        )
        self.assertEqual(operations[0]["evidence"][0]["start"], 22)
        self.assertEqual(operations[0]["evidence"][0]["end"], 27)
        self.assertEqual(issues[0]["code"], "evidence_quote_anchored")
        with self.assertRaises(HarnessError) as absent:
            propose("A whale.", {"quote": "Whale"})
        self.assertEqual(absent.exception.code, "invalid_evidence")
        with self.assertRaises(HarnessError) as ambiguous:
            propose("whale whale", {"quote": "whale"})
        self.assertEqual(ambiguous.exception.code, "ambiguous_evidence")
        operations, _ = propose("whale whale", {"quote": "whale", "start": 6, "end": 11})
        self.assertEqual(operations[0]["evidence"][0]["start"], 6)

    def test_budget_trimming_keeps_closed_relations_and_shared_input(self):
        snapshot = self.app.ontologies.read("demo")
        raw = snapshot["ontology"]
        retriever = SimpleRetriever({"strategy": "all"})
        empty = SimpleRetriever({"strategy": "none"}).search("mammal", raw)
        mandatory = TemplateContextBuilder().pack(
            "build", "source", raw, snapshot["schema"], empty, output_contract={}
        )
        budget = mandatory.input_bytes + 180
        pack = TemplateContextBuilder({"max_input_bytes": budget}).pack(
            "build",
            "source",
            raw,
            snapshot["schema"],
            retriever.search("mammal", raw),
            output_contract={},
        )
        self.assertLessEqual(pack.input_bytes, budget)
        self.assertTrue(pack.metadata["truncated"])
        data = json.loads(pack.messages[-1]["content"])
        nodes = {node["id"] for node in data["ontology"]["entities"]}
        for relation in data["ontology"]["relations"]:
            self.assertLessEqual({relation["source"], relation["target"]}, nodes)
        self.assertEqual(data["input"]["text"], "source")
        self.assertEqual(data["domain_schema"], snapshot["schema"])


if __name__ == "__main__":
    unittest.main()
