"""Offline conversation invariants; no external API calls."""

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from ob_harness.common import HarnessError, digest
from ob_harness.ontologies.repository import OntologyRepository
from ob_harness.services import Application
from ob_harness.services.application import initialize
from ob_harness.services.conversation import ConversationService
from ob_harness.services.maintenance import cleanup
from ob_harness.state.store import RunStore


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        initialize(self.root, writable=True)
        self.service = ConversationService(Application(self.root))
        self.conv = self.service.create(ontology_id="demo")
        self.id = self.conv["id"]

    def tearDown(self):
        self.temp.cleanup()

    async def say(self, text, **kwargs):
        current = self.service.get(self.id)
        return await self.service.send(
            self.id,
            text=text,
            client_message_id=kwargs.pop("key", uuid4().hex),
            expected_revision=current["revision"],
            planner="scripted",
            **kwargs,
        )

    async def test_multi_turn_evidence_rename_delete_undo_and_confirm(self):
        original = Application(self.root).ontologies.read("demo")["version"]
        first = await self.say("加入鲸，它属于Mammal")
        self.assertEqual(first["status"], "completed", first)
        second = await self.say("加入蓝鲸作为鲸的下位概念")
        self.assertEqual(second["status"], "completed", second)
        raw = self.service.drafts.read(self.id)["ontology"]
        whale = next(r for r in raw["entities"] if r["name"] == "鲸")
        whale_id = whale["id"]
        doc = Application(self.root).db.read_artifact(
            whale["field_evidence"]["name"][0]["artifact"]
        )
        self.assertEqual(doc["metadata"]["source_kind"], "user_message")
        self.assertEqual(doc["text"], "加入鲸，它属于Mammal")
        updated = await self.say("把鲸的定义改成“海洋中的哺乳动物”")
        self.assertEqual(updated["status"], "completed", updated)
        renamed = await self.say("把鲸重命名为鲸类")
        self.assertEqual(renamed["status"], "completed", renamed)
        self.assertEqual(
            next(
                r
                for r in self.service.drafts.read(self.id)["ontology"]["entities"]
                if r["id"] == whale_id
            )["name"],
            "鲸类",
        )
        blocked = await self.say("删除鲸类")
        self.assertEqual(blocked["status"], "waiting_input", blocked)
        deleted = await self.say("删除鲸类及其关系")
        self.assertEqual(deleted["status"], "completed", deleted)
        self.assertNotIn(
            whale_id, [r["id"] for r in self.service.drafts.read(self.id)["ontology"]["entities"]]
        )
        undone = await self.say("撤销上一步")
        self.assertEqual(undone["status"], "completed", undone)
        self.assertIn(
            whale_id, [r["id"] for r in self.service.drafts.read(self.id)["ontology"]["entities"]]
        )
        self.assertEqual(Application(self.root).ontologies.read("demo")["version"], original)
        review = await self.say("保存")
        self.assertEqual(review["status"], "completed", review)
        packet = review["result"]["review"]
        with self.assertRaises(HarnessError) as error:
            Application(self.root).ontologies.commit(
                packet["changeset_id"],
                expected_hash=packet["changeset_hash"],
                revision=1,
                idempotency_key="bypass",
            )
        self.assertEqual(error.exception.code, "confirmation_required")
        self.service.present(self.id, packet["id"])
        committed = await self.say("确认保存", reply_to=packet["id"], key="confirm")
        self.assertEqual(committed["status"], "completed", committed)
        self.assertEqual(committed["result"]["kind"], "ontology_committed")
        retry = await self.say("确认保存", reply_to=packet["id"], key="confirm")
        self.assertEqual(retry, committed)
        self.assertEqual(len(Application(self.root).ontologies.versions("demo")), 2)
        again = await self.say("加入海豚")
        self.assertEqual(again["status"], "completed", again)
        self.assertNotEqual(again["draft"]["id"], committed["draft"]["id"])

    async def test_stale_review_cannot_save_new_draft(self):
        await self.say("加入鲸")
        review = await self.say("保存")
        packet = review["result"]["review"]
        self.service.present(self.id, packet["id"])
        await self.say("加入海豚")
        result = await self.say("确认保存", reply_to=packet["id"])
        # No active review: prepare a new one, never publish the newer draft.
        self.assertNotEqual(result["result"].get("kind"), "ontology_committed")
        self.assertEqual(len(Application(self.root).ontologies.versions("demo")), 1)

    async def test_message_idempotency_and_revision_conflict(self):
        before = self.service.get(self.id)
        result = await self.say("加入鲸", key="one")
        duplicate = await self.service.send(
            self.id,
            text="加入鲸",
            client_message_id="one",
            expected_revision=before["revision"],
            planner="scripted",
        )
        self.assertEqual(result, duplicate)
        with self.assertRaises(HarnessError) as error:
            await self.service.send(
                self.id,
                text="加入海豚",
                client_message_id="other",
                expected_revision=before["revision"],
                planner="scripted",
            )
        self.assertEqual(error.exception.code, "conversation_conflict")
        self.assertEqual(self.service.get(self.id)["draft"]["revision"], 1)

    async def test_new_ontology_remains_unpublished_until_confirmed(self):
        path = self.root / "harness.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["permissions"]["new"] = ["read", "propose", "write"]
        path.write_text(json.dumps(config), encoding="utf-8")
        self.conv = self.service.create(ontology_id="new", create_spec={})
        self.id = self.conv["id"]
        await self.say("加入鲸，它属于哺乳动物")
        with self.assertRaises(HarnessError):
            Application(self.root).ontologies.read("new")
        review = await self.say("保存")
        self.assertEqual(review["status"], "completed", review)
        self.service.present(self.id, review["result"]["review"]["id"])
        saved = await self.say("确认保存")
        self.assertEqual(saved["status"], "completed", saved)
        self.assertEqual(
            len(Application(self.root).ontologies.read("new")["ontology"]["entities"]), 2
        )

    async def test_permission_reloaded_before_confirmation(self):
        await self.say("加入鲸")
        review = await self.say("保存")
        self.service.present(self.id, review["result"]["review"]["id"])
        path = self.root / "harness.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["permissions"]["demo"] = ["read", "propose"]
        path.write_text(json.dumps(config), encoding="utf-8")
        result = await self.say("确认保存")
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(result["result"]["error"]["code"], "permission_denied")
        self.assertEqual(len(Application(self.root).ontologies.versions("demo")), 1)

    async def test_interruption_after_edit_recovers_receipt_without_duplicate(self):
        complete = RunStore.complete_operation
        interrupted = False

        def interrupt(store, state, identifier, value):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                raise asyncio.CancelledError()
            return complete(store, state, identifier, value)

        with patch.object(RunStore, "complete_operation", interrupt):
            with self.assertRaises(asyncio.CancelledError):
                await self.say("加入鲸")
        current = self.service.get(self.id)
        self.assertIsNotNone(current["active_turn_id"])
        self.assertEqual(current["draft"]["revision"], 1)
        restored = ConversationService(Application(self.root))
        result = await restored.resume(self.id)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(restored.get(self.id)["draft"]["revision"], 1)
        self.assertEqual(Application(self.root).runs.get(result["run_id"])["status"], "completed")

    async def test_confirmation_transaction_rolls_back_pointer_and_receipt(self):
        await self.say("加入鲸")
        review = await self.say("保存")
        packet = review["result"]["review"]
        self.service.present(self.id, packet["id"])
        publish = OntologyRepository.publish_review

        def fail_after_pointer(repo, *args, **kwargs):
            publish(repo, *args, **kwargs)
            raise HarnessError("injected_failure", "Interrupted during commit transaction")

        with patch.object(OntologyRepository, "publish_review", fail_after_pointer):
            result = await self.say("确认保存")
        self.assertEqual(result["status"], "failed")
        app = Application(self.root)
        self.assertEqual(len(app.ontologies.versions("demo")), 1)
        self.assertEqual(app.db.get("conversation_review", packet["id"])["status"], "pending")
        self.assertEqual(app.db.list("confirmation_receipt"), [])
        result = await self.say("确认保存")
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(len(app.ontologies.versions("demo")), 2)

    async def test_application_resume_finishes_turn_after_agent_already_completed(self):
        with patch.object(ConversationService, "_finish", side_effect=asyncio.CancelledError()):
            with self.assertRaises(asyncio.CancelledError):
                await self.say("加入鲸")
        conv = self.service.get(self.id)
        turn = self.service.db.get("conversation_turn", conv["active_turn_id"])
        self.assertEqual(Application(self.root).runs.get(turn["run_id"])["status"], "completed")
        response = await Application(self.root).resume(turn["run_id"])
        self.assertEqual(response["result"]["kind"], "draft_edited")
        self.assertIsNone(self.service.get(self.id)["active_turn_id"])
        self.assertEqual(self.service.get(self.id)["draft"]["revision"], 1)

    async def test_competing_conversation_conflict_can_explicitly_rebase(self):
        other = self.service.create(ontology_id="demo")["id"]
        await self.say("加入鲸")
        first = await self.say("保存")
        self.service.present(self.id, first["result"]["review"]["id"])
        original_id = self.id
        self.id = other
        await self.say("加入海豚")
        other_review = await self.say("保存")
        self.service.present(other, other_review["result"]["review"]["id"])
        await self.say("确认保存")
        self.id = original_id
        conflict = await self.say("确认保存")
        self.assertEqual(conflict["status"], "failed", conflict)
        self.assertEqual(conflict["draft"]["status"], "conflicted")
        rebased = await self.say("/rebase")
        self.assertEqual(rebased["status"], "completed", rebased)
        names = {r["name"] for r in self.service.drafts.read(self.id)["ontology"]["entities"]}
        self.assertTrue({"鲸", "海豚"} <= names)
        self.assertNotEqual(rebased["draft"]["id"], conflict["draft"]["id"])

    async def test_replay_llm_path_and_invalid_batch_are_atomic(self):
        text = "Whale is a Mammal"
        decision = {
            "action": "tool",
            "tool": "draft_edit",
            "arguments": {
                "operations": [
                    {
                        "op": "add_concept",
                        "ref": "whale",
                        "record": {"name": "Whale", "type": "concept"},
                        "quote": text,
                    },
                    {
                        "op": "add_relation",
                        "ref": "edge",
                        "record": {"source": "whale", "target": "mammal", "type": "is_a"},
                        "quote": text,
                    },
                ]
            },
            "question": "",
        }
        bad_text = "Make a cycle"
        bad = {
            "action": "tool",
            "tool": "draft_edit",
            "arguments": {
                "operations": [
                    {
                        "op": "add_concept",
                        "ref": "unused",
                        "record": {"name": "Unused", "type": "concept"},
                        "quote": bad_text,
                    },
                    {
                        "op": "add_relation",
                        "ref": "cycle",
                        "record": {"source": "animal", "target": "mammal", "type": "is_a"},
                        "quote": bad_text,
                    },
                ]
            },
            "question": "",
        }
        path = self.root / "harness.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["model"] = {
            "provider": "replay",
            "responses": [
                {
                    "match": {"task": "conversation", "input_hash": digest(text)},
                    "response": decision,
                },
                {
                    "match": {"task": "conversation", "input_hash": digest(bad_text)},
                    "response": bad,
                },
            ],
        }
        path.write_text(json.dumps(config), encoding="utf-8")
        conv = self.service.get(self.id)
        result = await self.service.send(
            self.id,
            text=text,
            client_message_id="llm",
            expected_revision=conv["revision"],
            planner="llm",
        )
        self.assertEqual(result["status"], "completed", result)
        before = self.service.get(self.id)
        result = await self.service.send(
            self.id,
            text=bad_text,
            client_message_id="bad",
            expected_revision=before["revision"],
            planner="llm",
        )
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(
            self.service.get(self.id)["draft"]["revision"], before["draft"]["revision"]
        )
        self.assertNotIn(
            "Unused", [r["name"] for r in self.service.drafts.read(self.id)["ontology"]["entities"]]
        )
        self.assertEqual(self.service.get(self.id)["model_calls"], 2)

    async def test_expired_review_and_other_subject_are_rejected(self):
        await self.say("加入鲸")
        result = await self.say("保存")
        packet = result["result"]["review"]
        self.service.present(self.id, packet["id"])
        app = Application(self.root)
        stored = app.db.get("conversation_review", packet["id"])
        stored["expires_at"] = "2000-01-01T00:00:00+00:00"
        app.db.put("conversation_review", packet["id"], stored)
        refused = await self.say("确认保存")
        self.assertEqual(refused["result"]["error"]["code"], "review_expired")
        path = self.root / "harness.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["subject"] = "someone_else"
        path.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaises(HarnessError) as error:
            self.service.messages(self.id)
        self.assertEqual(error.exception.code, "not_found")

    async def test_cleanup_retains_conversation_provenance(self):
        response = await self.say("加入鲸")
        app = Application(self.root)
        source = next(m for m in self.service.messages(self.id) if m["role"] == "user")
        with app.db.transaction() as db:
            db.execute("UPDATE runs SET updated='2000-01-01T00:00:00+00:00'")
            db.execute("UPDATE artifact_pins SET touched=?", (time.time() - 86400 * 100,))
        result = cleanup(app, older_than_days=1, apply=True)
        self.assertNotIn(response["run_id"], result["run_ids"])
        self.assertEqual(app.db.read_artifact(source["document_ref"])["text"], "加入鲸")
        self.assertEqual(self.service.get(self.id)["draft"]["revision"], 1)
