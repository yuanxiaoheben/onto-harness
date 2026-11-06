"""Schema semantics and compatibility; no model calls."""

import tempfile
import unittest
from pathlib import Path

from ob_harness.agent.conversation import scripted_decision
from ob_harness.common import HarnessError
from ob_harness.ontologies import schema_preset, validate_ontology, validate_schema
from ob_harness.services import Application
from ob_harness.services.application import initialize


class SchemaPresetTests(unittest.TestCase):
    def ontology(self, edges):
        return {
            "entities": [{"id": x, "name": x, "type": "concept"} for x in "abc"],
            "relations": [
                {"id": str(i), "source": s, "target": t, "type": k}
                for i, (s, t, k) in enumerate(edges)
            ],
        }

    def test_presets_are_valid_and_isolated(self):
        for name in ("general", "taxonomy"):
            validate_schema(schema_preset(name))
        schema = schema_preset()
        self.assertEqual(len(schema["relation_types"]), 8)
        schema["relation_types"]["is_a"]["source_types"].clear()
        validate_schema(schema_preset())
        self.assertEqual(list(schema_preset("taxonomy")["relation_types"]), ["is_a"])
        with self.assertRaises(ValueError):
            schema_preset("missing")

    def test_all_relations_and_separate_graphs(self):
        schema = schema_preset()
        for kind in schema["relation_types"]:
            self.assertTrue(validate_ontology(self.ontology([("a", "b", kind)]), schema)["passed"])
        # Opposite arrows in independent structures are not a taxonomy cycle.
        raw = self.ontology([("a", "b", "is_a"), ("b", "a", "part_of")])
        self.assertTrue(validate_ontology(raw, schema)["passed"])

    def test_cycles_self_loops_and_unknown_types(self):
        for kind in ("is_a", "part_of", "precedes"):
            for edges in (
                [("a", "a", kind)],
                [("a", "b", kind), ("b", "c", kind), ("c", "a", kind)],
            ):
                result = validate_ontology(self.ontology(edges), schema_preset())
                self.assertFalse(result["passed"])
                expected = "hierarchy_cycle" if kind == "is_a" else "relation_cycle"
                self.assertIn(expected, [x["code"] for x in result["issues"]])
        raw = self.ontology([("a", "b", "related_to"), ("b", "a", "related_to")])
        self.assertTrue(validate_ontology(raw, schema_preset())["passed"])
        self.assertFalse(
            validate_ontology(self.ontology([("a", "b", "invented")]), schema_preset())["passed"]
        )
        schema = schema_preset()
        schema["relation_types"]["part_of"]["acyclic"] = "true"
        with self.assertRaises(HarnessError):
            validate_schema(schema)

    def test_draft_defaults_existing_schema_and_scripted_compatibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            initialize(Path(tmp), writable=True)
            app = Application(Path(tmp))
            service = app.conversations
            new = service._new_draft("new", create_spec={})
            self.assertEqual(app.db.read_artifact(new["schema_ref"]), schema_preset())
            old = service._new_draft("demo")
            self.assertEqual(
                app.db.read_artifact(old["schema_ref"]), app.ontologies.read("demo")["schema"]
            )
            custom = service._new_draft("custom", create_spec={"schema": schema_preset("taxonomy")})
            self.assertEqual(app.db.read_artifact(custom["schema_ref"]), schema_preset("taxonomy"))
        decision = scripted_decision(
            "鲸是哺乳动物", {"entities": [], "relations": []}, schema_preset()
        )
        self.assertEqual(decision.tool, "draft_edit")
        self.assertEqual(decision.arguments["operations"][-1]["record"]["type"], "is_a")
