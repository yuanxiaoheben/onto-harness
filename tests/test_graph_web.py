"""Panorama API binding and permissions; no external inference."""

import json
import tempfile
import unittest
from pathlib import Path

from ob_harness.services import Application
from ob_harness.services.application import initialize

try:
    from fastapi.testclient import TestClient

    from ob_harness.web.server import create_app
except ImportError:
    TestClient = None


@unittest.skipIf(TestClient is None, "web extra is not installed")
class GraphWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        initialize(self.root, writable=True)
        self.client = TestClient(create_app(self.root, "demo", editable=True))

    def tearDown(self):
        self.temp.cleanup()

    def test_formal_snapshot_and_draft_are_distinct(self):
        formal = self.client.get("/api/ontology").json()
        self.assertEqual(formal["view"], "formal")
        self.assertEqual(len(formal["ontology"]["entities"]), 2)
        headers = {"X-Harness-CSRF": self.client.get("/api/info").json()["csrf_token"]}
        conv = self.client.post("/api/conversations", json={}, headers=headers).json()
        result = self.client.post(
            f"/api/conversations/{conv['id']}/messages",
            headers=headers,
            json={
                "message": "加入鲸",
                "expected_revision": conv["revision"],
                "planner": "scripted",
            },
        )
        self.assertEqual(result.json()["status"], "completed", result.text)
        draft = self.client.get(f"/api/conversations/{conv['id']}/draft").json()
        self.assertEqual(len(draft["ontology"]["entities"]), 3)
        self.assertEqual(self.client.get("/api/ontology").json(), formal)

    def test_snapshot_rechecks_read_permissions_and_serves_assets(self):
        for path in ("/static/graph.js", "/static/graph-layout.js"):
            self.assertEqual(self.client.get(path).status_code, 200)
        self.assertIn('id="view-graph"', self.client.get("/").text)
        path = self.root / "harness.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["permissions"]["demo"] = []
        path.write_text(json.dumps(config), encoding="utf-8")
        self.assertEqual(self.client.get("/api/ontology").status_code, 403)

    def test_snapshot_honors_fixed_version_and_server_resource(self):
        original = Application(self.root).ontologies.read("demo")
        client = TestClient(create_app(self.root, "demo", version=original["version"]))
        snapshot = client.get("/api/ontology?ontology_id=other&version=other").json()
        self.assertEqual(snapshot["version"], original["version"])
        self.assertEqual(snapshot["ontology_id"], "demo")
