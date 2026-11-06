"""Optional FastAPI adapter checks; skipped when the web extra is absent."""

import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from ob_harness.common import HarnessError
from ob_harness.services.application import initialize

try:
    from fastapi.testclient import TestClient
    from ob_harness.web.server import create_app
except ImportError:  # pragma: no cover - exercised only by minimal installs
    TestClient = None
    create_app = None


@unittest.skipIf(TestClient is None, "web extra is not installed")
class WebChatTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "workspace"
        initialize(self.root, writable=False)
        self.client = TestClient(create_app(self.root, "demo"))

    def tearDown(self):
        self.temporary.cleanup()

    def test_static_client_and_info_are_served(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Ontology Harness", page.text)
        self.assertIn("/static/app.js", page.text)

        script = self.client.get("/static/app.js")
        self.assertEqual(script.status_code, 200)
        self.assertIn("function renderResponse", script.text)

        info = self.client.get("/api/info")
        self.assertEqual(info.status_code, 200)
        self.assertEqual(info.json()["ontology_id"], "demo")
        self.assertEqual(info.json()["concept_count"], 2)
        self.assertEqual(info.json()["relation_count"], 1)

    def test_scripted_chat_returns_ontology_answer(self):
        response = self.client.post("/api/chat", json={"message": "mammal"})
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["result_kind"], "ontology_answer")
        self.assertEqual(payload["result"]["status"], "found")
        self.assertIn("mammal", [item["id"] for item in payload["result"]["concepts"]])

    def test_empty_message_is_rejected_by_the_api(self):
        response = self.client.post("/api/chat", json={"message": "   "})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")

    def test_missing_ontology_fails_during_startup(self):
        with self.assertRaises(HarnessError) as error:
            create_app(self.root, "missing")
        self.assertIn(error.exception.code, {"permission_denied", "not_found"})


@unittest.skipIf(TestClient is None, "web extra is not installed")
class EditableWebTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        initialize(self.root, writable=True)
        self.client = TestClient(create_app(self.root, "demo", editable=True))
        info = self.client.get("/api/info").json()
        self.headers = {"X-Harness-CSRF": info["csrf_token"]}

    def tearDown(self):
        self.temporary.cleanup()

    def test_web_conversation_edit_review_confirm_and_refresh(self):
        created = self.client.post("/api/conversations", json={}, headers=self.headers)
        self.assertEqual(created.status_code, 200, created.text)
        conv = created.json()
        identifier = conv["id"]

        def send(text):
            current = self.client.get(f"/api/conversations/{identifier}").json()
            response = self.client.post(
                f"/api/conversations/{identifier}/messages",
                headers=self.headers,
                json={
                    "message": text,
                    "client_message_id": uuid4().hex,
                    "expected_revision": current["revision"],
                    "planner": "scripted",
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "completed", response.text)
            return response.json()

        send("加入鲸，它属于Mammal")
        review = send("保存")["result"]["review"]
        ack = self.client.post(
            f"/api/conversations/{identifier}/reviews/{review['id']}/present",
            headers=self.headers,
            json={},
        )
        self.assertEqual(ack.status_code, 200, ack.text)
        committed = send("确认保存")
        self.assertEqual(committed["result"]["kind"], "ontology_committed")
        messages = self.client.get(f"/api/conversations/{identifier}/messages").json()
        self.assertEqual(len(messages), 6)
        self.assertEqual(
            messages[-1]["response"]["result"]["version"], committed["result"]["version"]
        )
        self.assertEqual(self.client.get("/api/info").json()["concept_count"], 3)

    def test_csrf_origin_and_readonly_mode_block_writes(self):
        self.assertEqual(self.client.post("/api/conversations", json={}).status_code, 403)
        hostile = {**self.headers, "Origin": "http://attacker.invalid"}
        self.assertEqual(
            self.client.post("/api/conversations", json={}, headers=hostile).status_code, 403
        )
        readonly = TestClient(create_app(self.root, "demo"))
        self.assertEqual(readonly.post("/api/conversations", json={}).status_code, 403)
