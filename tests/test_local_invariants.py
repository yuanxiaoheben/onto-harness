"""Offline invariant checks for future CI; no real models or PDF parsers."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from ob_harness.agent import AgentDecision, AgentRuntime, Tool
from ob_harness.agent.planner import ScriptedPlanner
from ob_harness.agent.presets import run_agent
from ob_harness.common import HarnessError, digest
from ob_harness.services import Application
from ob_harness.services.application import initialize


class LocalInvariants(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "workspace"
        initialize(self.root, writable=True)
        self.app = Application(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    async def build_example(self):
        return await self.app.build(
            "demo", [self.root / "examples/part-1.txt", self.root / "examples/part-2.txt"]
        )

    async def test_build_uses_prior_draft_and_immutable_input_on_resume(self):
        run_id = self.app.create_build(
            "demo", [self.root / "examples/part-1.txt", self.root / "examples/part-2.txt"]
        )
        (self.root / "examples/part-1.txt").write_text(
            "Changed after preparation", encoding="utf-8"
        )
        state = await Application(self.root).resume(run_id)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["model_calls"], 2)
        trace = self.app.db.read_artifact(state["traces"][1])
        context = self.app.db.read_artifact(trace["context_ref"])
        self.assertEqual(context["metadata"]["source"]["workspace_revision"], 1)
        whale_id = "c_" + digest(["concept", "whale"])[:24]
        self.assertIn(whale_id, context["selected_entity_ids"])
        self.assertEqual(len(self.app.ontologies.read("demo")["ontology"]["entities"]), 2)
        again = await Application(self.root).resume(run_id)
        self.assertEqual(again["model_calls"], 2)

    async def test_commit_is_idempotent_and_stale_changes_cannot_overwrite(self):
        first, second = await self.build_example(), await self.build_example()
        value = first["result"]
        result = self.app.ontologies.commit(
            value["changeset_id"],
            expected_hash=value["changeset_hash"],
            revision=1,
            idempotency_key="first",
        )
        repeated = self.app.ontologies.commit(
            value["changeset_id"],
            expected_hash=value["changeset_hash"],
            revision=1,
            idempotency_key="first",
        )
        self.assertEqual(result, repeated)
        self.assertEqual(len(self.app.ontologies.versions("demo")), 2)
        with self.assertRaises(HarnessError) as stale:
            self.app.ontologies.commit(
                second["result"]["changeset_id"],
                expected_hash=second["result"]["changeset_hash"],
                revision=1,
                idempotency_key="second",
            )
        self.assertEqual(stale.exception.code, "version_conflict")

    async def test_current_alias_is_frozen_in_proposal(self):
        state = await self.build_example()
        original = self.app.ontologies.changeset(state["result"]["changeset_id"])
        proposal = self.app.ontologies.propose("demo", "current", original["operations"])
        self.assertEqual(proposal["base_version"], original["base_version"])
        self.assertEqual(proposal["id"], original["id"])
        committed = self.app.ontologies.commit(
            proposal["id"],
            expected_hash=proposal["hash"],
            revision=1,
            idempotency_key="alias",
        )
        self.assertNotEqual(committed["version"], proposal["base_version"])
        self.assertTrue(self.app.ontologies.validate(proposal["id"])["passed"])
        with self.assertRaises(HarnessError) as stale:
            self.app.ontologies.commit(
                proposal["id"],
                expected_hash=proposal["hash"],
                revision=1,
                idempotency_key="alias-new-key",
            )
        self.assertEqual(stale.exception.code, "version_conflict")

    async def test_read_only_principal_cannot_propose_or_commit(self):
        state = await self.build_example()
        path = self.root / "harness.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["permissions"] = {"demo": ["read"]}
        path.write_text(json.dumps(config), encoding="utf-8")
        readonly = Application(self.root)
        with self.assertRaises(HarnessError) as denied:
            readonly.create_build("demo", [self.root / "examples/part-1.txt"])
        self.assertEqual(denied.exception.code, "permission_denied")
        value = state["result"]
        with self.assertRaises(HarnessError):
            readonly.ontologies.commit(
                value["changeset_id"],
                expected_hash=value["changeset_hash"],
                revision=1,
                idempotency_key="denied",
            )

    async def test_unconfirmed_side_effect_is_not_repeated_after_restart(self):
        calls = []

        async def external(_arguments):
            calls.append("external side effect")
            raise RuntimeError("connection lost after execution")

        tool = Tool(
            "external",
            "Uncertain external action",
            {"type": "object", "additionalProperties": False},
            external,
            terminal=True,
        )
        planner = ScriptedPlanner([AgentDecision("tool", "external", {})])
        original = AgentRuntime(planner, [tool], store=self.app.runs)
        failed = await original.run("do it")
        self.assertEqual(failed.status, "failed")
        restored = AgentRuntime(
            planner, [tool], store=Application(self.root).runs, run_id=failed.run_id
        )
        result = await restored.run("do it")
        self.assertEqual(result.status, "waiting_review")
        self.assertEqual(len(calls), 1)

    async def test_memory_namespace_and_version_filters(self):
        current = self.app.ontologies.read("demo")["version"]
        value = self.app.memories.put(
            "Prefer explicit whale definitions",
            namespace="research",
            ontology_id="demo",
            version=current,
        )
        self.assertFalse(
            self.app.memories.search(
                "whale", namespace="default", ontology_id="demo", version=current
            )
        )
        self.assertFalse(
            self.app.memories.search(
                "whale", namespace="research", ontology_id="demo", version="stale"
            )
        )
        self.assertEqual(
            self.app.memories.search(
                "whale", namespace="research", ontology_id="demo", version=current
            )[0]["id"],
            value["id"],
        )
        self.app.memories.delete(value["id"], namespace="research")
        self.assertFalse(
            self.app.memories.search(
                "whale", namespace="research", ontology_id="demo", version=current
            )
        )

    @unittest.skipUnless(importlib.util.find_spec("langgraph"), "optional LangGraph extra")
    async def test_optional_graph_uses_same_business_result(self):
        state = await run_agent(self.app, "demo", "mammal", backend="langgraph")
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["result"]["kind"], "ontology_answer")


if __name__ == "__main__":
    unittest.main()
