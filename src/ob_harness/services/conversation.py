"""Persistent, source-backed conversation construction shared by CLI and Web."""

import asyncio
import re
from uuid import uuid4

from .. import __version__
from ..agent import AgentRuntime, Budget, Tool, ToolResult
from ..agent.conversation import ConversationPlanner
from ..common import HarnessError, digest, now, positive_int, safe_error
from ..conversations.sources import message_document
from ..conversations.store import ConversationStore
from ..ontologies.presets import schema_preset
from ..ontologies.validation import validate_schema
from ..retrieval import SimpleRetriever
from ..state import WorkspaceLock
from ..tools.invocation import operation_id
from .confirmation import CONFIRM_TEXT, ConfirmationService
from .drafts import DraftService
from .schema_changes import ACCEPT_SCHEMA, REJECT_SCHEMA, SchemaChangeService

DEFAULT_SCHEMA = schema_preset()
COMMANDS = {
    "/rebase": "draft_rebase",
    "基于最新版本重新生成草案": "draft_rebase",
    "保存": "draft_prepare_review",
    "确认保存": "draft_prepare_review",
    "save": "draft_prepare_review",
    "/save": "draft_prepare_review",
    "查看差异": "draft_diff",
    "/diff": "draft_diff",
    "diff": "draft_diff",
    "撤销": "draft_undo",
    "撤销上一步": "draft_undo",
    "/undo": "draft_undo",
    "undo": "draft_undo",
}


class ConversationService:
    def __init__(self, app):
        self.app = app
        self._bind()

    def _bind(self):
        self.db, self.store = self.app.db, ConversationStore(self.app)
        self.drafts = DraftService(self.app)
        self.schema_changes = SchemaChangeService(self.app, self.drafts)

    def _refresh(self):
        from .application import Application

        self.app = Application(self.app.workspace)
        self._bind()

    def _new_draft(self, ontology_id, *, version=None, create_spec=None):
        if create_spec is not None:
            schema = create_spec.get("schema", schema_preset())
            validate_schema(schema)
            raw, base_version, mode = (
                {"metadata": {}, "entities": [], "relations": []},
                None,
                "create",
            )
        else:
            base = self.app.ontologies.read(ontology_id, version)
            raw, schema, base_version, mode = (
                base["ontology"],
                base["schema"],
                base["version"],
                "extend",
            )
        snapshot_ref = self.db.artifact(raw)
        return {
            "id": uuid4().hex,
            "ontology_id": ontology_id,
            "base_version": base_version,
            "mode": mode,
            "revision": 0,
            "snapshot_ref": snapshot_ref,
            "base_snapshot_ref": snapshot_ref,
            "schema_ref": self.db.artifact(schema),
            "base_schema_ref": self.db.artifact(schema),
            "undo_stack": [],
            "status": "editing",
            "created_at": now(),
        }

    def create(self, *, ontology_id, base_version=None, create_spec=None, max_model_calls=200):
        self._refresh()
        if not isinstance(ontology_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,100}", ontology_id
        ):
            raise HarnessError("invalid_id", "Invalid ontology ID")
        self.app.authorizer.require("read", ontology_id)
        self.app.authorizer.require("propose", ontology_id)
        positive_int(max_model_calls, "conversation max_model_calls", maximum=10000)
        if create_spec is not None:
            if not isinstance(create_spec, dict) or set(create_spec) - {"schema"} or base_version:
                raise HarnessError(
                    "invalid_request", "New ontology accepts only an explicit schema template"
                )
            with self.db.connect() as db:
                if db.execute(
                    "SELECT 1 FROM objects WHERE kind='ontology' AND id=?", (ontology_id,)
                ).fetchone():
                    raise HarnessError("already_exists", "Ontology already exists")
        draft = self._new_draft(ontology_id, version=base_version, create_spec=create_spec)
        conv = {
            "schema_version": 1,
            "id": uuid4().hex,
            "subject": self.app.authorizer.principal.subject,
            "ontology_id": ontology_id,
            "active_draft_id": draft["id"],
            "revision": 0,
            "active_turn_id": None,
            "pending_review_id": None,
            "pending_clarification": None,
            "message_ids": [],
            "status": "open",
            "model_calls": 0,
            "max_model_calls": max_model_calls,
            "created_at": now(),
        }
        with self.db.transaction() as db:
            self.db.put("draft", draft["id"], draft, db)
            self.db.put("conversation", conv["id"], conv, db)
            self.db.audit(
                conv["subject"],
                "conversation_create",
                ontology_id,
                "allowed",
                {"conversation_id": conv["id"]},
                db,
            )
        return self.get(conv["id"])

    def get(self, conversation_id):
        self._refresh()
        conv = self.store.get(conversation_id)
        draft = self.store.draft(conv)
        raw = self.db.read_artifact(draft["snapshot_ref"])
        return {
            **conv,
            "draft": {
                **draft,
                "concept_count": len(raw["entities"]),
                "relation_count": len(raw["relations"]),
            },
        }

    def messages(self, conversation_id, *, after_seq=0):
        self._refresh()
        conv = self.store.get(conversation_id)
        if type(after_seq) is not int or after_seq < 0:
            raise HarnessError("invalid_request", "after_seq must be nonnegative")
        return [
            self.db.get("conversation_message", identifier)
            for seq, identifier in enumerate(conv["message_ids"], 1)
            if seq > after_seq
        ]

    async def send(
        self,
        conversation_id,
        *,
        text,
        client_message_id,
        expected_revision,
        reply_to=None,
        planner="llm",
    ):
        self._refresh()
        if not isinstance(text, str) or not text.strip() or len(text) > 8000:
            raise HarnessError("invalid_request", "Message must contain 1-8000 characters")
        if not isinstance(client_message_id, str) or not 1 <= len(client_message_id) <= 200:
            raise HarnessError("invalid_request", "A bounded client_message_id is required")
        if planner not in {"scripted", "llm"}:
            raise HarnessError("invalid_planner", "Planner must be scripted or llm")
        conv = self.store.get(conversation_id)
        key = digest([conv["subject"], conversation_id, client_message_id])
        fingerprint = digest([text, reply_to, planner])
        try:
            receipt = self.db.get("conversation_request", key)
        except HarnessError as error:
            if error.code != "not_found":
                raise
            receipt = None
        if receipt:
            if receipt["fingerprint"] != fingerprint:
                raise HarnessError("idempotency_conflict", "Message key has a different payload")
            turn = self.db.get("conversation_turn", receipt["turn_id"])
            if turn.get("response") is not None:
                return turn["response"]
            return await self.resume(conversation_id, turn_id=turn["id"])
        self.store.require_revision(conv, expected_revision)
        if conv["active_turn_id"]:
            raise HarnessError(
                "conversation_busy",
                "Resume or cancel the active turn before sending another message",
            )
        draft = self.store.draft(conv)
        new_draft = None
        if draft["status"] == "committed":
            new_draft = self._new_draft(conv["ontology_id"], version=draft["committed_version"])
        message = {
            "id": uuid4().hex,
            "conversation_id": conversation_id,
            "subject": conv["subject"],
            "role": "user",
            "text": text,
            "created_at": now(),
            "reply_to": reply_to,
            "client_message_id": client_message_id,
        }
        message["document_ref"] = self.db.artifact(message_document(message, text))
        draft = new_draft or draft
        turn = {
            "id": uuid4().hex,
            "conversation_id": conversation_id,
            "message_id": message["id"],
            "run_id": uuid4().hex,
            "draft_id": draft["id"],
            "draft_revision": draft["revision"],
            "snapshot_ref": draft["snapshot_ref"],
            "schema_ref": draft["schema_ref"],
            "source_message_ids": [message["id"]],
            "planner": planner,
            "config_hash": self.app.execution_hash,
            "package_version": __version__,
            "history_ids": conv["message_ids"][-4:],
            "status": "pending",
            "response": None,
            "command": COMMANDS.get(text.strip(), ""),
            "confirmation_review_id": None,
            "schema_reply_id": None,
        }
        answer = text.strip().casefold().rstrip("。.!！")
        if conv.get("pending_schema_id") and answer in ACCEPT_SCHEMA | REJECT_SCHEMA:
            turn["schema_reply_id"] = reply_to or conv["pending_schema_id"]
        if conv.get("pending_clarification"):
            turn["source_message_ids"] += conv["pending_clarification"]["source_message_ids"]
        if text.strip() in CONFIRM_TEXT and conv.get("pending_review_id"):
            turn["confirmation_review_id"] = reply_to or conv["pending_review_id"]
        elif text.strip() in CONFIRM_TEXT:
            turn["command"] = "draft_prepare_review"
        with self.db.transaction() as db:
            conv = self.store.get(conversation_id, db)
            self.store.require_revision(conv, expected_revision)
            if conv["active_turn_id"]:
                raise HarnessError("conversation_busy", "Conversation already has an active turn")
            if not turn["confirmation_review_id"]:
                self.drafts.invalidate(conv, db, preserve_schema=bool(turn["schema_reply_id"]))
            if new_draft:
                self.db.put("draft", new_draft["id"], new_draft, db)
                conv["active_draft_id"] = new_draft["id"]
            message["seq"] = len(conv["message_ids"]) + 1
            conv["message_ids"].append(message["id"])
            conv["active_turn_id"] = turn["id"]
            self.db.put(
                "document",
                message["document_ref"],
                {"artifact": message["document_ref"], "ontology_ids": [conv["ontology_id"]]},
                db,
            )
            self.db.put("conversation_message", message["id"], message, db)
            self.db.put("conversation_turn", turn["id"], turn, db)
            self.db.put(
                "conversation_request", key, {"fingerprint": fingerprint, "turn_id": turn["id"]}, db
            )
            self.store.save(conv, db)
        return await self.resume(conversation_id, turn_id=turn["id"])

    def _tools(self, turn):
        conv_id = turn["conversation_id"]
        raw = self.db.read_artifact(turn["snapshot_ref"])

        def tool_result(payload):
            return ToolResult(
                payload, {"kind": payload["kind"], "message": payload.get("message", "")}
            )

        async def edit(args):
            return tool_result(
                self.drafts.apply(
                    conv_id, turn, args["operations"], operation_id=operation_id.get()
                )
            )

        async def undo(args):
            return tool_result(self.drafts.undo(conv_id, turn, operation_id=operation_id.get()))

        async def propose_schema(args):
            return tool_result(
                self.schema_changes.propose(
                    conv_id,
                    turn,
                    args["additions"],
                    args["reason"],
                    operation_id=operation_id.get(),
                )
            )

        async def review(args):
            return tool_result(
                self.drafts.prepare_review(conv_id, turn, operation_id=operation_id.get())
            )

        async def diff(args):
            return tool_result(self.drafts.diff(conv_id))

        async def rebase(args):
            return tool_result(self.drafts.rebase(conv_id, turn, operation_id=operation_id.get()))

        async def query(args):
            bound = self.store.get(conv_id)
            selected = SimpleRetriever(self.app.config["retrieval"]).search(args["query"], raw)
            nodes, edges = set(selected.entity_ids), set(selected.relation_ids)
            return tool_result(
                {
                    "kind": "ontology_answer",
                    "ontology_id": bound["ontology_id"],
                    "draft_id": turn["draft_id"],
                    "view": "draft",
                    "draft_revision": turn["draft_revision"],
                    "concepts": [r for r in raw["entities"] if r["id"] in nodes],
                    "relations": [r for r in raw["relations"] if r["id"] in edges],
                    "status": "found" if nodes else "insufficient_context",
                    "citations": [
                        {
                            "draft_id": turn["draft_id"],
                            "draft_revision": turn["draft_revision"],
                            "id": key,
                        }
                        for key in [*selected.entity_ids, *selected.relation_ids]
                    ],
                    "message": f"查询草案 r{turn['draft_revision']}，匹配 {len(nodes)} 个概念。",
                }
            )

        empty = {"type": "object", "properties": {}, "additionalProperties": False}
        return [
            Tool(
                name,
                description,
                schema,
                fn,
                terminal=True,
                effect=effect,
                idempotent=True,
                version="1",
            )
            for name, description, schema, fn, effect in [
                (
                    "draft_propose_schema",
                    "Ask the user to approve new concept/relation types; does not modify schema.",
                    {
                        "type": "object",
                        "properties": {
                            "additions": {
                                "type": "object",
                                "properties": {
                                    "concept_types": {"type": "array", "items": {"type": "string"}},
                                    "relation_types": {"type": "object"},
                                },
                                "additionalProperties": False,
                            },
                            "reason": {"type": "string", "maxLength": 2000},
                        },
                        "required": ["additions", "reason"],
                        "additionalProperties": False,
                    },
                    propose_schema,
                    "propose",
                ),
                (
                    "draft_rebase",
                    "Explicitly rebuild on latest version; record conflicts are rejected.",
                    empty,
                    rebase,
                    "propose",
                ),
                (
                    "draft_edit",
                    "Apply one atomic batch of source-backed edits.",
                    {
                        "type": "object",
                        "properties": {
                            "operations": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 100,
                                "items": {"type": "object"},
                            }
                        },
                        "required": ["operations"],
                        "additionalProperties": False,
                    },
                    edit,
                    "propose",
                ),
                (
                    "draft_query",
                    "Search concepts in the current draft.",
                    {
                        "type": "object",
                        "properties": {"query": {"type": "string", "maxLength": 8000}},
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                    query,
                    "read",
                ),
                ("draft_undo", "Undo the last uncommitted batch.", empty, undo, "propose"),
                ("draft_diff", "Show pending net edits.", empty, diff, "read"),
                (
                    "draft_prepare_review",
                    "Prepare a fixed review; never commits.",
                    empty,
                    review,
                    "propose",
                ),
            ]
        ]

    async def resume(self, conversation_id, *, turn_id=None):
        self._refresh()
        conv = self.store.get(conversation_id)
        turn_id = turn_id or conv["active_turn_id"]
        if not turn_id:
            raise HarnessError("not_found", "No active conversation turn")
        turn = self.db.get("conversation_turn", turn_id)
        if turn["conversation_id"] != conversation_id:
            raise HarnessError("not_found", "Turn does not belong to this conversation")
        if turn["response"] is not None:
            return turn["response"]
        if turn["config_hash"] != self.app.execution_hash or turn["package_version"] != __version__:
            raise HarnessError(
                "resume_incompatible", "Conversation model configuration or package changed"
            )
        message = self.db.get("conversation_message", turn["message_id"])
        try:
            if turn.get("schema_reply_id"):
                with WorkspaceLock(self.db.root):
                    result = self.schema_changes.respond(conversation_id, turn)
                status, run_id = "completed", None
            elif turn["confirmation_review_id"]:
                with WorkspaceLock(self.db.root):
                    result = ConfirmationService(self.app).confirm(
                        conversation_id,
                        message=message,
                        review_id=turn["confirmation_review_id"],
                        idempotency_key=turn["id"],
                    )
                status, run_id = "completed", None
            else:
                # Draft mutation and its receipt share a transaction. Recover that
                # terminal business result even if Agent checkpoint publication was
                # interrupted after the mutation (or cancellation changed its stage).
                with WorkspaceLock(self.db.root):
                    with self.db.connect() as db:
                        rows = db.execute(
                            "SELECT id,value FROM objects WHERE kind='edit_receipt' AND id LIKE ? ORDER BY id DESC",
                            (turn["run_id"] + ":tool-%",),
                        ).fetchall()
                    if rows:
                        import json

                        recovered = json.loads(rows[0]["value"])["result"]
                        run = self.app.runs.get(turn["run_id"])
                        run.update(
                            status="completed",
                            result=recovered,
                            result_kind="tool_result",
                            stage="plan",
                            decision=None,
                        )
                        run.pop("error", None)
                        run.pop("pending_operation", None)
                        self.app.runs.complete_operation(
                            run,
                            rows[0]["id"].split(":", 1)[1],
                            {"payload": recovered, "summary": {"kind": recovered["kind"]}},
                        )
                        self.app.runs.event(turn["run_id"], "conversation_result_recovered")
                        return self._finish(turn, "completed", recovered, turn["run_id"])
                raw = self.db.read_artifact(turn["snapshot_ref"])
                schema = self.db.read_artifact(turn["schema_ref"])
                history = [self.db.get("conversation_message", key) for key in turn["history_ids"]]
                text = message["text"]
                if len(turn["source_message_ids"]) > 1:
                    # Preserve exact user-source IDs; no synthetic assistant evidence.
                    text = "\n".join(
                        f"[{key}] {self.db.get('conversation_message', key)['text']}"
                        for key in [*turn["source_message_ids"][1:], turn["message_id"]]
                    )
                planner = ConversationPlanner(self.app, turn, raw, schema, history)
                runtime = AgentRuntime(
                    planner,
                    self._tools(turn),
                    Budget(
                        max_steps=2,
                        max_tool_calls=4,
                        max_model_calls=self.app.config["budget"]["max_model_calls"],
                        timeout_seconds=self.app.config["budget"]["timeout_seconds"],
                    ),
                    store=self.app.runs,
                    run_id=turn["run_id"],
                    config_hash=turn["config_hash"],
                    ontology_id=conv["ontology_id"],
                    base_version=self.store.draft(conv)["base_version"],
                    bindings={
                        "conversation_id": conversation_id,
                        "turn_id": turn_id,
                        "draft_revision": turn["draft_revision"],
                        "snapshot_ref": turn["snapshot_ref"],
                    },
                )
                execution = await runtime.run(text)
                status, result, run_id = execution.status, execution.payload, execution.run_id
        except asyncio.CancelledError:
            # Keep the active turn resumable; its operation receipts prevent repeats.
            raise
        except Exception as error:
            if isinstance(error, HarnessError) and error.code == "workspace_busy":
                raise
            status, result, run_id = (
                "failed",
                {"error": safe_error(error), "message": safe_error(error)["message"]},
                None,
            )
        return self._finish(turn, status, result, run_id)

    def _finish(self, turn, status, result, run_id):
        conv_id = turn["conversation_id"]
        with self.db.transaction() as db:
            saved = self.db.get("conversation_turn", turn["id"], db)
            if saved["response"] is not None:
                return saved["response"]
            conv = self.store.get(conv_id, db)
            if conv["active_turn_id"] != turn["id"]:
                raise HarnessError("conversation_conflict", "Another turn owns the conversation")
            if result.get("kind") in {"schema_proposal", "schema_updated"}:
                conv["pending_clarification"] = {
                    "question": result["message"],
                    "source_message_ids": result.get(
                        "source_message_ids", turn["source_message_ids"]
                    ),
                }
            elif status == "waiting_input":
                conv["pending_clarification"] = {
                    "question": result.get("question"),
                    "source_message_ids": turn["source_message_ids"],
                }
            else:
                conv["pending_clarification"] = None
            conv["active_turn_id"] = None
            if result.get("error", {}).get("code") == "version_conflict":
                draft = self.store.draft(conv, db)
                draft["status"] = "conflicted"
                self.db.put("draft", draft["id"], draft, db)
                self.drafts.invalidate(conv, db)
            self.store.save(conv, db)
            text = result.get(
                "message",
                result.get(
                    "question", result.get("error", {}).get("message", "操作未完成，请检查结果。")
                ),
            )
            response = {
                "conversation_id": conv_id,
                "conversation_revision": conv["revision"],
                "turn_id": turn["id"],
                "run_id": run_id,
                "status": status,
                "message": text,
                "result": result,
                "draft": self.store.draft(conv, db),
            }
            assistant = {
                "id": uuid4().hex,
                "conversation_id": conv_id,
                "subject": conv["subject"],
                "role": "assistant",
                "seq": len(conv["message_ids"]) + 1,
                "text": text,
                "response": response,
                "created_at": now(),
            }
            conv["message_ids"].append(assistant["id"])
            self.db.put("conversation_message", assistant["id"], assistant, db)
            self.db.put("conversation", conv_id, conv, db)
            saved.update(status=status, response=response)
            self.db.put("conversation_turn", turn["id"], saved, db)
            code = result.get("error", {}).get("code")
            self.db.audit(
                conv["subject"],
                "conversation_turn",
                conv["ontology_id"],
                "denied" if code == "permission_denied" else status,
                {
                    "conversation_id": conv_id,
                    "turn_id": turn["id"],
                    "run_id": run_id,
                    "error_code": code,
                },
                db,
            )
            return response

    def present(self, conversation_id, review_id):
        """Trusted transport acknowledges that it displayed the persisted review."""
        self._refresh()
        with self.db.transaction() as db:
            conv = self.store.get(conversation_id, db)
            packet = self.db.get("conversation_review", review_id, db)
            if (
                conv["pending_review_id"] != review_id
                or packet["conversation_id"] != conversation_id
            ):
                raise HarnessError("review_conflict", "Review is no longer active")
            packet["presented"] = True
            self.db.put("conversation_review", review_id, packet, db)
        return {"review_id": review_id, "presented": True}
