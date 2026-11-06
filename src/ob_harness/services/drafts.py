"""Atomic, source-backed edits with snapshot undo and immutable review packets."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .. import __version__
from ..common import HarnessError, digest, normalized, now
from ..conversations.sources import source_span
from ..conversations.store import ConversationStore
from ..ontologies.operations import FIELDS, apply_v2, net_operations
from ..ontologies.validation import validate_ontology


class DraftService:
    def __init__(self, app):
        self.app, self.db, self.store = app, app.db, ConversationStore(app)

    def read(self, conversation_id: str) -> dict:
        conv = self.store.get(conversation_id)
        draft = self.store.draft(conv)
        return {
            **draft,
            "ontology": self.db.read_artifact(draft["snapshot_ref"]),
            "schema": self.db.read_artifact(draft["schema_ref"]),
        }

    def _receipt(self, operation_id, signature):
        if not isinstance(operation_id, str) or not operation_id:
            raise HarnessError("invalid_key", "A stable editing operation key is required")
        try:
            value = self.db.get("edit_receipt", operation_id)
        except HarnessError as error:
            if error.code == "not_found":
                return None
            raise
        if value["signature"] != signature:
            raise HarnessError("idempotency_conflict", "Editing key has a different payload")
        return value["result"]

    def _bound(self, conv_id, turn, db=None):
        conv = self.store.get(conv_id, db)
        draft = self.store.draft(conv, db)
        if conv["active_turn_id"] != turn["id"] or draft["id"] != turn["draft_id"]:
            raise HarnessError("draft_conflict", "Turn no longer owns the active draft")
        if draft["revision"] != turn["draft_revision"]:
            raise HarnessError(
                "draft_conflict", "Draft changed while this response was being generated"
            )
        if not self.app.authorizer.allowed("propose", conv["ontology_id"]):
            raise HarnessError("permission_denied", "Editing requires proposal permission")
        return conv, draft

    def invalidate(self, conv, db, *, preserve_schema=False):
        if conv.get("pending_schema_id") and not preserve_schema:
            proposal = self.db.get("schema_proposal", conv["pending_schema_id"], db)
            if proposal["status"] == "pending":
                proposal["status"] = "invalidated"
                self.db.put("schema_proposal", proposal["id"], proposal, db)
            conv["pending_schema_id"] = None
        if conv.get("pending_review_id"):
            review = self.db.get("conversation_review", conv["pending_review_id"], db)
            if review["status"] == "pending":
                review["status"] = "invalidated"
                self.db.put("conversation_review", review["id"], review, db)
            conv["pending_review_id"] = None
            draft = self.store.draft(conv, db)
            if draft["status"] == "awaiting_confirmation":
                draft["status"] = "editing"
                self.db.put("draft", draft["id"], draft, db)

    def _advance(self, conv, draft, snapshot_ref, batch, operation_id, signature, db):
        self.invalidate(conv, db)
        revision = draft["revision"] + 1
        history = {
            "draft_id": draft["id"],
            "revision": revision,
            "snapshot_ref": snapshot_ref,
            "before_ref": draft["snapshot_ref"],
            "before_schema_ref": draft["schema_ref"],
            "schema_ref": batch.get("after_schema_ref", draft["schema_ref"]),
            "batch": batch,
            "created_at": now(),
        }
        draft.update(revision=revision, snapshot_ref=snapshot_ref, status="editing")
        draft["schema_ref"] = history["schema_ref"]
        self.db.put("draft_revision", f"{draft['id']}:{revision}", history, db)
        self.db.put("draft", draft["id"], draft, db)
        self.store.save(conv, db)
        result = {
            "kind": "draft_edited",
            "draft_id": draft["id"],
            "draft_revision": revision,
            "batch": batch,
            "message": f"草案 r{revision} 已更新；尚未保存为正式版本。",
        }
        self.db.put("edit_receipt", operation_id, {"signature": signature, "result": result}, db)
        self.db.audit(
            conv["subject"],
            "draft_undo" if batch.get("undo_of") else "draft_edit",
            conv["ontology_id"],
            "allowed",
            {
                "conversation_id": conv["id"],
                "draft_id": draft["id"],
                "draft_revision": revision,
                "operation_id": operation_id,
            },
            db,
        )
        return result

    def apply(self, conv_id: str, turn: dict, edits: list, *, operation_id: str) -> dict:
        signature = digest([conv_id, turn["id"], edits])
        self.store.get(conv_id)
        prior = self._receipt(operation_id, signature)
        if prior is not None:
            return prior
        conv, draft = self._bound(conv_id, turn)
        if not isinstance(edits, list) or not 1 <= len(edits) <= 100:
            raise HarnessError("invalid_operation", "Provide 1-100 edits in one batch")
        before = self.db.read_artifact(draft["snapshot_ref"])
        candidate = deepcopy(before)
        schema = self.db.read_artifact(draft["schema_ref"])
        indexes = {
            "concept": {r["id"]: r for r in candidate["entities"]},
            "relation": {r["id"]: r for r in candidate["relations"]},
        }
        message = self.db.get("conversation_message", turn["message_id"])
        instructions = [source_span(self.app, message)]
        local = {}
        # Preallocate stable IDs; relations may refer to concepts added in this batch.
        for index, edit in enumerate(edits):
            if not isinstance(edit, dict) or edit.get("op") not in {
                f"{action}_{kind}"
                for action in ("add", "update", "delete")
                for kind in ("concept", "relation")
            }:
                raise HarnessError("invalid_operation", "Unknown editing operation")
            if edit["op"].startswith("add_"):
                ref = edit.get("ref", str(index))
                if not isinstance(ref, str) or ref in local:
                    raise HarnessError(
                        "invalid_operation", "Local references must be unique strings"
                    )
                local[ref] = edit["op"][4] + "_" + digest([turn["id"], index])[:24]
        for index, edit in enumerate(edits):
            action, kind = edit["op"].split("_", 1)
            allowed = (
                {"op", "id"}
                if action == "delete"
                else (
                    {"op", "ref", "record", "quote", "message_id"}
                    if action == "add"
                    else {"op", "id", "set", "unset", "quote", "message_id"}
                )
            )
            if set(edit) - allowed:
                raise HarnessError("invalid_operation", "Unexpected edit fields")
            target = indexes[kind]
            if action == "delete":
                if edit.get("id") not in target:
                    raise HarnessError("not_found", "Edited record not found")
                del target[edit["id"]]
                continue
            source_id = edit.get("message_id", turn["message_id"])
            if source_id not in turn["source_message_ids"]:
                raise HarnessError("resource_not_bound", "Evidence message is outside this turn")
            source = self.db.get("conversation_message", source_id)
            if source["conversation_id"] != conv_id or source["role"] != "user":
                raise HarnessError("invalid_evidence", "Evidence must be a bound user statement")
            support = [source_span(self.app, source, edit.get("quote"))]
            fields = FIELDS[kind]
            if action == "add":
                fields = fields | ({"source", "target", "type"} if kind == "relation" else set())
                record = deepcopy(edit.get("record"))
                if not isinstance(record, dict) or set(record) - fields:
                    raise HarnessError("invalid_operation", "Unsupported record fields")
                identifier = local[edit.get("ref", str(index))]
                if kind == "concept":
                    if not isinstance(record.get("name"), str):
                        raise HarnessError("invalid_operation", "Concept name is required")
                    matches = [
                        r
                        for r in target.values()
                        if normalized(record["name"])
                        in {normalized(n) for n in [r["name"], *r.get("aliases", [])]}
                    ]
                    if matches:
                        raise HarnessError(
                            "ambiguous_alignment", "Name already exists; edit its explicit ID"
                        )
                else:
                    for endpoint in ("source", "target"):
                        endpoint_id = record.get(endpoint)
                        if not isinstance(endpoint_id, str):
                            raise HarnessError(
                                "invalid_operation", "Relation endpoints must be IDs or local refs"
                            )
                        record[endpoint] = local.get(endpoint_id, endpoint_id)
                record["id"] = identifier
                record["field_evidence"] = {k: support for k in record if k != "id"}
                record["assertion_origin"] = "user_asserted"
                target[identifier] = record
            else:
                identifier = edit.get("id")
                if identifier not in target:
                    raise HarnessError("not_found", "Edited record not found")
                record = target[identifier]
                changes, removed = edit.get("set", {}), edit.get("unset", [])
                if (
                    not isinstance(changes, dict)
                    or not isinstance(removed, list)
                    or any(not isinstance(k, str) for k in removed)
                    or (changes.keys() | set(removed)) - fields
                    or changes.keys() & set(removed)
                    or set(removed)
                    - {"definition", "aliases", "constraints", "application_conditions"}
                ):
                    raise HarnessError(
                        "invalid_operation", "Unsupported or contradictory field changes"
                    )
                supports = record.setdefault("field_evidence", {})
                for field, value in changes.items():
                    record[field] = value
                    supports[field] = support
                for field in removed:
                    record.pop(field, None)
                    supports.pop(field, None)
                record["assertion_origin"] = "user_asserted"
        candidate["entities"] = list(indexes["concept"].values())
        candidate["relations"] = list(indexes["relation"].values())
        deleted = {r["id"] for r in before["entities"]} - indexes["concept"].keys()
        affected = [r["id"] for r in candidate["relations"] if {r["source"], r["target"]} & deleted]
        if affected:
            raise HarnessError(
                "concept_in_use",
                "Delete scope must explicitly include these relations: " + ", ".join(affected),
            )
        operations = net_operations(before, candidate, instructions)
        if not operations:
            raise HarnessError("no_changes", "This batch makes no changes")
        candidate = apply_v2(before, operations, schema, self.app.db.read_artifact)
        snapshot_ref = self.db.artifact(candidate)
        batch = {
            "id": operation_id,
            "operations": operations,
            "message_id": message["id"],
            "run_id": turn["run_id"],
            "instruction_refs": instructions,
        }
        with self.db.transaction() as db:
            conv, draft = self._bound(conv_id, turn, db)
            draft["undo_stack"].append(draft["revision"] + 1)
            return self._advance(conv, draft, snapshot_ref, batch, operation_id, signature, db)

    def undo(self, conv_id, turn, *, operation_id):
        signature = digest([conv_id, turn["id"], "undo"])
        self.store.get(conv_id)
        prior = self._receipt(operation_id, signature)
        if prior is not None:
            return prior
        with self.db.transaction() as db:
            conv, draft = self._bound(conv_id, turn, db)
            if not draft["undo_stack"]:
                raise HarnessError("nothing_to_undo", "No uncommitted edit remains to undo")
            target = draft["undo_stack"].pop()
            history = self.db.get("draft_revision", f"{draft['id']}:{target}", db)
            message = self.db.get("conversation_message", turn["message_id"], db)
            batch = {
                "id": operation_id,
                "undo_of": history["batch"]["id"],
                "instruction_refs": [source_span(self.app, message)],
                "run_id": turn["run_id"],
                "after_schema_ref": history.get("before_schema_ref", draft["schema_ref"]),
            }
            return self._advance(
                conv, draft, history["before_ref"], batch, operation_id, signature, db
            )

    def diff(self, conv_id):
        draft = self.read(conv_id)
        before = self.db.read_artifact(draft["base_snapshot_ref"])
        # Provenance remains in immutable history, including deletes and undo.
        instructions = []
        schema_changes = []
        per_record: dict[tuple[str, str], list] = {}
        for rev in draft["undo_stack"]:
            history = self.db.get("draft_revision", f"{draft['id']}:{rev}")
            instructions.extend(history["batch"]["instruction_refs"])
            if history["batch"].get("schema_proposal_id"):
                proposal = self.db.get("schema_proposal", history["batch"]["schema_proposal_id"])
                schema_changes.append(
                    {
                        "proposal_id": proposal["id"],
                        "additions": proposal["additions"],
                        "reason": proposal["reason"],
                        "reply_message_id": proposal["reply_message_id"],
                    }
                )
            for op in history["batch"]["operations"]:
                key = (op["op"].split("_", 1)[1], op["id"])
                per_record.setdefault(key, []).extend(op["instruction_refs"])
        instructions = list({digest(ref): ref for ref in instructions}.values())
        operations = net_operations(before, draft["ontology"], instructions)
        for op in operations:
            refs = per_record[(op["op"].split("_", 1)[1], op["id"])]
            op["instruction_refs"] = list({digest(ref): ref for ref in refs}.values())
        old_records = {
            (kind, r["id"]): r
            for section, kind in (("entities", "concept"), ("relations", "relation"))
            for r in before[section]
        }
        changes = [
            {
                "op": op["op"],
                "id": op["id"],
                "before": old_records.get((op["op"].split("_", 1)[1], op["id"])),
                "after": op.get("record"),
                "instruction_refs": op["instruction_refs"],
            }
            for op in operations
        ]
        return {
            "kind": "draft_diff",
            "draft_id": draft["id"],
            "draft_revision": draft["revision"],
            "operations": operations,
            "changes": changes,
            "schema_changes": schema_changes,
            "validation": validate_ontology(draft["ontology"], draft["schema"]),
            "message": f"草案 r{draft['revision']}：{len(operations)} 项内容变更，{len(schema_changes)} 项 schema 扩展。",
        }

    def prepare_review(self, conv_id, turn, *, operation_id):
        signature = digest([conv_id, turn["id"], "review"])
        self.store.get(conv_id)
        prior = self._receipt(operation_id, signature)
        if prior is not None:
            return prior
        conv, draft = self._bound(conv_id, turn)
        diff = self.diff(conv_id)
        if not diff["operations"] and not diff["schema_changes"]:
            raise HarnessError("no_changes", "There are no pending changes to save")
        schema = self.db.read_artifact(draft["schema_ref"])
        binding = {
            "conversation_id": conv_id,
            "draft_id": draft["id"],
            "draft_revision": draft["revision"],
            "schema_approval_ids": [item["proposal_id"] for item in diff["schema_changes"]],
        }
        changeset = self.app.ontologies.propose_v2(
            conv["ontology_id"],
            draft["base_version"],
            diff["operations"],
            schema,
            binding,
            mode=draft["mode"],
        )
        raw, _ = self.app.ontologies.candidate_v2(changeset)
        reference = self.app.ontologies._snapshot(conv["ontology_id"], raw, schema)
        if reference["content_hash"] != self.db.artifact(
            {"ontology": self.db.read_artifact(draft["snapshot_ref"]), "schema": schema}
        ):
            raise HarnessError("draft_conflict", "Compiled edits do not reproduce the draft")
        packet = {
            "validation_contract": "ontology-edit-v2/1",
            "package_version": __version__,
            **binding,
            "id": uuid4().hex,
            "subject": conv["subject"],
            "status": "pending",
            "base_version": draft["base_version"],
            "changeset_id": changeset["id"],
            "changeset_hash": changeset["hash"],
            "candidate_hash": reference["version"],
            "reference": reference,
            "diff": diff,
            "created_at": now(),
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
            "presented": False,
        }
        result = {
            "kind": "draft_review",
            "review": packet,
            "message": f"请审阅草案 r{draft['revision']} 的净差异与来源。确认后回复“确认保存”。用户引文会随本体版本保留。",
        }
        with self.db.transaction() as db:
            conv, current = self._bound(conv_id, turn, db)
            self.invalidate(conv, db)
            conv["pending_review_id"] = packet["id"]
            current["status"] = "awaiting_confirmation"
            self.db.put("draft", current["id"], current, db)
            self.db.put("conversation_review", packet["id"], packet, db)
            self.store.save(conv, db)
            self.db.put(
                "edit_receipt", operation_id, {"signature": signature, "result": result}, db
            )
            self.db.audit(
                conv["subject"],
                "draft_review",
                conv["ontology_id"],
                "allowed",
                {
                    "conversation_id": conv_id,
                    "review_id": packet["id"],
                    "draft_revision": draft["revision"],
                },
                db,
            )
        return result

    def rebase(self, conv_id, turn, *, operation_id):
        signature = digest([conv_id, turn["id"], "rebase"])
        self.store.get(conv_id)
        prior = self._receipt(operation_id, signature)
        if prior is not None:
            return prior
        conv, old = self._bound(conv_id, turn)
        base = self.app.ontologies.read(conv["ontology_id"])
        schema = self.db.read_artifact(old["schema_ref"])
        if schema != base["schema"]:
            raise HarnessError(
                "schema_conflict", "Rebuilding across schema changes requires a new conversation"
            )
        diff = self.diff(conv_id)
        candidate = apply_v2(base["ontology"], diff["operations"], schema, self.db.read_artifact)
        snapshot_ref = self.db.artifact(candidate)
        base_ref = self.db.artifact(base["ontology"])
        refs = [ref for op in diff["operations"] for ref in op["instruction_refs"]]
        # A rebase is a new draft generation; undo restores its latest formal base.
        draft = {
            "id": uuid4().hex,
            "ontology_id": conv["ontology_id"],
            "base_version": base["version"],
            "mode": "extend",
            "revision": 0,
            "snapshot_ref": base_ref,
            "base_snapshot_ref": base_ref,
            "schema_ref": old["schema_ref"],
            "undo_stack": [1],
            "status": "editing",
            "created_at": now(),
        }
        batch = {
            "id": operation_id,
            "operations": diff["operations"],
            "instruction_refs": refs,
            "rebased_from": old["id"],
            "run_id": turn["run_id"],
        }
        with self.db.transaction() as db:
            conv, old = self._bound(conv_id, turn, db)
            if self.db.get("ontology", conv["ontology_id"], db)["current"] != base["version"]:
                raise HarnessError("version_conflict", "Formal version changed during rebuild")
            self.invalidate(conv, db)
            old["status"] = "abandoned"
            self.db.put("draft", old["id"], old, db)
            conv["active_draft_id"] = draft["id"]
            return self._advance(conv, draft, snapshot_ref, batch, operation_id, signature, db)
