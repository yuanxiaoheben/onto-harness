"""User-bound schema negotiation, persistence and publication without paid inference."""

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from ob_harness.agent.contracts import AgentDecision
from ob_harness.common import HarnessError
from ob_harness.services import Application
from ob_harness.services.application import initialize
from ob_harness.services.schema_changes import extend_schema

ADDITIONS = {
    "relation_types": {
        "part_of": {
            "description": "部分概念指向整体概念",
            "direction": "directed",
            "acyclic": True,
            "source_types": ["concept"],
            "target_types": ["concept"],
        }
    }
}


class SchemaConversationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        initialize(self.root, writable=True)
        self.app = Application(self.root)
        self.service = self.app.conversations
        self.conv = self.service.create(ontology_id="demo")
        self.id = self.conv["id"]
        self.original = self.app.ontologies.read("demo")

    def tearDown(self):
        self.temp.cleanup()

    async def say(self, text, **kwargs):
        return await self.service.send(
            self.id,
            text=text,
            client_message_id=kwargs.pop("key", uuid4().hex),
            expected_revision=self.service.get(self.id)["revision"],
            planner="scripted",
            **kwargs,
        )

    async def propose(self, additions=None):
        decision = AgentDecision(
            "tool",
            "draft_propose_schema",
            {
                "additions": additions or ADDITIONS,
                "reason": "分类关系不能表示部分与整体。",
            },
        )
        with patch(
            "ob_harness.agent.conversation.ConversationPlanner.decide",
            new=AsyncMock(return_value=decision),
        ):
            result = await self.say("心脏是循环系统的组成部分")
        self.assertEqual(result["result"]["kind"], "schema_proposal", result)
        return result

    async def test_accept_is_draft_only_and_schema_only_publish(self):
        proposed = await self.propose()
        self.assertNotIn("part_of", self.service.drafts.read(self.id)["schema"]["relation_types"])
        self.assertIn("part_of", proposed["message"])
        accepted = await self.say("同意", key="accept")
        self.assertEqual(accepted["result"]["kind"], "schema_updated", accepted)
        self.assertIn("part_of", self.service.drafts.read(self.id)["schema"]["relation_types"])
        self.assertEqual(self.app.ontologies.read("demo")["version"], self.original["version"])
        self.assertEqual(accepted, await self.say("同意", key="accept"))
        # A fresh service retains both the schema and approval state.
        self.service = Application(self.root).conversations
        review = await self.say("保存")
        self.assertEqual(review["result"]["kind"], "draft_review", review)
        self.assertEqual(len(review["result"]["review"]["diff"]["schema_changes"]), 1)
        self.service.present(self.id, review["result"]["review"]["id"])
        saved = await self.say("确认保存")
        self.assertEqual(saved["result"].get("kind"), "ontology_committed", saved)
        formal = self.app.ontologies.read("demo")
        self.assertIn("part_of", formal["schema"]["relation_types"])
        self.assertEqual(formal["ontology"], self.original["ontology"])

    async def test_continue_uses_original_evidence_and_undo_restores_schema(self):
        proposed = await self.propose()
        message_id = proposed["result"]["proposal"]["source_message_ids"][0]
        await self.say("同意修改schema")
        operations = [
            {
                "op": "add_concept",
                "ref": ref,
                "record": {"name": name, "type": "concept"},
                "quote": "心脏是循环系统的组成部分",
                "message_id": message_id,
            }
            for ref, name in [("heart", "心脏"), ("system", "循环系统")]
        ]
        operations.append(
            {
                "op": "add_relation",
                "ref": "edge",
                "record": {"source": "heart", "target": "system", "type": "part_of"},
                "quote": "心脏是循环系统的组成部分",
                "message_id": message_id,
            }
        )
        with patch(
            "ob_harness.agent.conversation.ConversationPlanner.decide",
            new=AsyncMock(
                return_value=AgentDecision("tool", "draft_edit", {"operations": operations})
            ),
        ):
            edited = await self.say("继续构建")
        self.assertEqual(edited["result"]["kind"], "draft_edited", edited)
        await self.say("撤销")
        self.assertIn("part_of", self.service.drafts.read(self.id)["schema"]["relation_types"])
        await self.say("撤销")
        self.assertEqual(self.service.drafts.read(self.id)["schema"], self.original["schema"])
        self.assertEqual(self.service.drafts.diff(self.id)["schema_changes"], [])

    async def test_reject_and_unrelated_reply_do_not_approve(self):
        await self.propose()
        result = await self.say("不同意")
        self.assertEqual(result["result"]["kind"], "schema_rejected")
        proposed = await self.propose()
        await self.say("查询 Animal")
        await self.say("同意")
        self.assertEqual(self.service.drafts.read(self.id)["schema"], self.original["schema"])
        self.assertEqual(
            self.app.db.get("schema_proposal", proposed["result"]["proposal"]["id"])["status"],
            "invalidated",
        )

    async def test_expired_and_wrong_proposal_rejected(self):
        proposed = await self.propose()
        identifier = proposed["result"]["proposal"]["id"]
        result = await self.say("同意", reply_to="wrong-id")
        self.assertEqual(result["status"], "failed")
        proposal = self.app.db.get("schema_proposal", identifier)
        proposal["expires_at"] = "2000-01-01T00:00:00+00:00"
        self.app.db.put("schema_proposal", identifier, proposal)
        result = await self.say("同意")
        self.assertEqual(result["result"]["error"]["code"], "schema_conflict")
        self.assertEqual(self.service.drafts.read(self.id)["schema"], self.original["schema"])

    async def test_resume_after_approval_before_finish_is_idempotent(self):
        await self.propose()
        with patch.object(self.service, "_finish", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError):
                await self.say("同意", key="interrupted")
        revision = self.service.drafts.read(self.id)["revision"]
        result = await Application(self.root).conversations.resume(self.id)
        self.assertEqual(result["result"]["kind"], "schema_updated")
        self.assertEqual(self.service.drafts.read(self.id)["revision"], revision)

    async def test_unapproved_schema_cannot_be_published(self):
        schema = extend_schema(self.original["schema"], ADDITIONS)
        with self.assertRaises(HarnessError) as caught:
            self.app.ontologies.propose_v2(
                "demo",
                self.original["version"],
                [],
                schema,
                {
                    "conversation_id": self.id,
                    "draft_id": self.conv["active_draft_id"],
                    "draft_revision": 0,
                },
                mode="extend",
            )
        self.assertEqual(caught.exception.code, "confirmation_required")

    async def test_new_ontology_multiple_extensions_and_publish(self):
        path = self.root / "harness.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["permissions"]["new"] = ["read", "propose", "write"]
        path.write_text(json.dumps(config), encoding="utf-8")
        self.conv = self.service.create(ontology_id="new", create_spec={})
        self.id = self.conv["id"]
        await self.propose({"concept_types": ["process"]})
        await self.say("可以")
        await self.propose({"concept_types": ["quality"]})
        await self.say("是的")
        review = await self.say("保存")
        self.assertEqual(review["result"].get("kind"), "draft_review", review)
        self.assertEqual(len(review["result"]["review"]["schema_approval_ids"]), 2)
        self.service.present(self.id, review["result"]["review"]["id"])
        saved = await self.say("确认保存")
        self.assertEqual(saved["result"].get("kind"), "ontology_committed", saved)
        self.assertEqual(
            Application(self.root).ontologies.read("new")["schema"]["concept_types"],
            ["concept", "process", "quality"],
        )

    async def test_permission_revocation_prevents_schema_acceptance(self):
        await self.propose()
        path = self.root / "harness.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["permissions"]["demo"] = ["read"]
        path.write_text(json.dumps(config), encoding="utf-8")
        result = await self.say("同意")
        self.assertEqual(result["result"]["error"]["code"], "permission_denied", result)
        self.assertEqual(self.service.drafts.read(self.id)["schema"], self.original["schema"])

    async def test_stale_draft_and_reused_approval_rejected(self):
        proposed = await self.propose()
        draft = self.service.store.draft(self.service.store.get(self.id))
        draft["revision"] += 1
        self.app.db.put("draft", draft["id"], draft)
        result = await self.say("同意")
        self.assertEqual(result["result"]["error"]["code"], "schema_conflict", result)
        self.assertEqual(self.service.drafts.read(self.id)["schema"], self.original["schema"])
        with self.assertRaises(HarnessError):
            self.app.ontologies.propose_v2(
                "demo",
                self.original["version"],
                [],
                extend_schema(self.original["schema"], ADDITIONS),
                {
                    "conversation_id": self.id,
                    "draft_id": draft["id"],
                    "draft_revision": draft["revision"],
                    "schema_approval_ids": [proposed["result"]["proposal"]["id"]],
                },
                mode="extend",
            )

    def test_extensions_reject_redefinition_and_invalid_constraints(self):
        for additions in [
            {"concept_types": ["concept"]},
            {"relation_types": {"is_a": ADDITIONS["relation_types"]["part_of"]}},
            {"relation_types": {"x": {"description": "missing endpoints"}}},
            {
                "relation_types": {
                    "x": {**ADDITIONS["relation_types"]["part_of"], "symmetric": True}
                }
            },
        ]:
            with self.assertRaises(HarnessError):
                extend_schema(self.original["schema"], additions)
        additions = deepcopy(ADDITIONS)
        additions["concept_types"] = ["process"]
        additions["relation_types"]["part_of"]["target_types"] = ["process"]
        self.assertIn("process", extend_schema(self.original["schema"], additions)["concept_types"])
