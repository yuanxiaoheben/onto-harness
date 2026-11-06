"""Propose additive schema extensions; only persisted user replies can accept them."""

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from ..common import HarnessError, digest, now
from ..conversations.sources import source_span
from ..ontologies.validation import validate_ontology, validate_schema

ACCEPT_SCHEMA = {
    "同意修改schema",
    "同意修改 schema",
    "同意",
    "是",
    "是的",
    "可以",
    "好",
    "好的",
    "确认修改",
    "yes",
}
REJECT_SCHEMA = {"不同意", "不", "否", "不要", "不修改", "取消", "no"}


def extend_schema(before, additions):
    if not isinstance(additions, dict) or set(additions) - {"concept_types", "relation_types"}:
        raise HarnessError("invalid_schema", "Only additive concept/relation types are supported")
    concepts, relations = additions.get("concept_types", []), additions.get("relation_types", {})
    if (
        not isinstance(concepts, list)
        or not isinstance(relations, dict)
        or len(concepts) + len(relations) > 30
    ):
        raise HarnessError("invalid_schema", "Provide at most 30 new types")
    result = deepcopy(before)
    for name in [*concepts, *relations]:
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise HarnessError("invalid_schema", "Type names must be bounded nonempty strings")
    if len(set(concepts)) != len(concepts) or set(concepts) & set(before["concept_types"]):
        raise HarnessError("schema_conflict", "Concept types must be new and unique")
    result["concept_types"].extend(concepts)
    for name, spec in relations.items():
        if name in before["relation_types"]:
            raise HarnessError(
                "schema_conflict", "Existing relation definitions cannot be replaced"
            )
        allowed = {
            "description",
            "direction",
            "hierarchical",
            "acyclic",
            "source_types",
            "target_types",
        }
        if (
            not isinstance(spec, dict)
            or set(spec) - allowed
            or not isinstance(spec.get("description"), str)
            or not spec["description"].strip()
        ):
            raise HarnessError(
                "invalid_schema", "New relations require semantics and supported constraints"
            )
        if (
            len(spec["description"]) > 2000
            or not {"direction", "source_types", "target_types"} <= spec.keys()
        ):
            raise HarnessError(
                "invalid_schema", "New relations require explicit direction and endpoint types"
            )
        result["relation_types"][name] = deepcopy(spec)
    validate_schema(result)
    if result == before:
        raise HarnessError("no_changes", "No schema extension was proposed")
    return result


class SchemaChangeService:
    def __init__(self, app, drafts):
        self.app, self.db, self.drafts = app, app.db, drafts

    def propose(self, conv_id, turn, additions, reason, *, operation_id):
        signature = digest([conv_id, turn["id"], additions, reason])
        self.drafts.store.get(conv_id)
        prior = self.drafts._receipt(operation_id, signature)
        if prior is not None:
            return prior
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000:
            raise HarnessError("invalid_schema", "Explain why the current schema is insufficient")
        conv, draft = self.drafts._bound(conv_id, turn)
        before = self.db.read_artifact(draft["schema_ref"])
        after = extend_schema(before, additions)
        if not validate_ontology(self.db.read_artifact(draft["snapshot_ref"]), after)["passed"]:
            raise HarnessError("invalid_schema", "Extension conflicts with the current ontology")
        proposal = {
            "id": uuid4().hex,
            "conversation_id": conv_id,
            "ontology_id": conv["ontology_id"],
            "subject": conv["subject"],
            "draft_id": draft["id"],
            "draft_revision": draft["revision"],
            "before_ref": draft["schema_ref"],
            "after_ref": self.db.artifact(after),
            "additions": additions,
            "reason": reason,
            "status": "pending",
            "source_message_ids": turn["source_message_ids"],
            "created_at": now(),
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
        }
        message = (
            f"当前 schema 无法完整表达该需求：{reason}\n建议新增以下定义（方向和约束如下）：\n"
            + json.dumps(additions, ensure_ascii=False, indent=2)
            + "\n是否同意修改 schema？回复“同意修改schema”或“不同意”。目前尚未修改；同意仅更新草案，正式保存仍需审阅确认。"
        )
        result = {"kind": "schema_proposal", "proposal": proposal, "message": message}
        with self.db.transaction() as db:
            conv, _ = self.drafts._bound(conv_id, turn, db)
            self.drafts.invalidate(conv, db)
            conv["pending_schema_id"] = proposal["id"]
            self.db.put("schema_proposal", proposal["id"], proposal, db)
            self.drafts.store.save(conv, db)
            self.db.put(
                "edit_receipt", operation_id, {"signature": signature, "result": result}, db
            )
            self.db.audit(
                conv["subject"],
                "schema_propose",
                conv["ontology_id"],
                "allowed",
                {"proposal_id": proposal["id"]},
                db,
            )
        return result

    def respond(self, conv_id, turn):
        message = self.db.get("conversation_message", turn["message_id"])
        proposal_id = turn["schema_reply_id"]
        signature = digest([conv_id, turn["id"], proposal_id])
        key = "schema-reply:" + turn["id"]
        self.drafts.store.get(conv_id)
        prior = self.drafts._receipt(key, signature)
        if prior is not None:
            return prior
        answer = message["text"].strip().casefold().rstrip("。.!！")
        if (
            message["role"] != "user"
            or message["conversation_id"] != conv_id
            or answer not in ACCEPT_SCHEMA | REJECT_SCHEMA
        ):
            raise HarnessError(
                "confirmation_required", "A separate explicit user reply is required"
            )
        with self.db.transaction() as db:
            conv, draft = self.drafts._bound(conv_id, turn, db)
            proposal = self.db.get("schema_proposal", proposal_id, db)
            if (
                conv.get("pending_schema_id") != proposal_id
                or proposal["status"] != "pending"
                or proposal["conversation_id"] != conv_id
                or proposal["subject"] != conv["subject"]
                or proposal["draft_id"] != draft["id"]
                or proposal["draft_revision"] != draft["revision"]
                or proposal["before_ref"] != draft["schema_ref"]
                or datetime.fromisoformat(proposal["expires_at"]) <= datetime.now(timezone.utc)
            ):
                raise HarnessError(
                    "schema_conflict", "Schema proposal expired or no longer matches the draft"
                )
            active = self.db.get("conversation_turn", conv["active_turn_id"], db)
            if (
                active["message_id"] != message["id"]
                or active.get("schema_reply_id") != proposal_id
            ):
                raise HarnessError(
                    "confirmation_required", "Reply must be the bound active user event"
                )
            conv["pending_schema_id"] = None
            proposal["reply_message_id"] = message["id"]
            if answer in REJECT_SCHEMA:
                proposal["status"] = "rejected"
                result = {
                    "kind": "schema_rejected",
                    "message": "已取消 schema 扩展，原 schema 保持不变。",
                }
                self.drafts.store.save(conv, db)
                self.db.put("edit_receipt", key, {"signature": signature, "result": result}, db)
            else:
                after = extend_schema(
                    self.db.read_artifact(draft["schema_ref"]), proposal["additions"]
                )
                if (
                    digest(after) != proposal["after_ref"]
                    or not validate_ontology(self.db.read_artifact(draft["snapshot_ref"]), after)[
                        "passed"
                    ]
                ):
                    raise HarnessError("schema_conflict", "Schema proposal failed revalidation")
                proposal["status"] = "accepted"
                draft.setdefault("base_schema_ref", draft["schema_ref"])
                batch = {
                    "id": key,
                    "operations": [],
                    "schema_proposal_id": proposal_id,
                    "before_schema_ref": draft["schema_ref"],
                    "after_schema_ref": proposal["after_ref"],
                    "instruction_refs": [source_span(self.app, message)],
                    "run_id": turn["run_id"],
                }
                draft["undo_stack"].append(draft["revision"] + 1)
                result = self.drafts._advance(
                    conv, draft, draft["snapshot_ref"], batch, key, signature, db
                )
                result.update(
                    kind="schema_updated",
                    message="已按你的确认扩展草案 schema。可回复“继续构建”处理原需求；正式版本尚未保存。",
                    source_message_ids=proposal["source_message_ids"],
                )
                self.db.put("edit_receipt", key, {"signature": signature, "result": result}, db)
            self.db.put("schema_proposal", proposal_id, proposal, db)
            self.db.audit(
                conv["subject"],
                "schema_reply",
                conv["ontology_id"],
                proposal["status"],
                {"proposal_id": proposal_id, "message_id": message["id"]},
                db,
            )
            return result
