import re
from typing import Any

from ..common import HarnessError, digest, now
from ..governance import Authorizer
from ..state import Database
from .operations import apply_v2
from .validation import apply_operations, validate_ontology


class OntologyRepository:
    def __init__(self, database: Database, authorizer: Authorizer):
        self.db, self.auth = database, authorizer

    def _snapshot(
        self, ontology_id: str, raw: dict[str, Any], schema: dict[str, Any]
    ) -> dict[str, Any]:
        report = validate_ontology(raw, schema)
        if not report["passed"]:
            raise HarnessError(
                "invalid_ontology", "Ontology structure does not conform to its schema"
            )
        version = digest({"ontology": raw, "schema": schema})
        blob = self.db.artifact({"ontology": raw, "schema": schema})
        return {
            "schema_version": 1,
            "ontology_id": ontology_id,
            "version": version,
            "content_hash": blob,
            "created_at": now(),
        }

    def import_ontology(
        self, ontology_id: str, raw: dict[str, Any], schema: dict[str, Any]
    ) -> dict[str, Any]:
        self.auth.require("write", ontology_id)
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", ontology_id):
            raise HarnessError(
                "invalid_id", "Ontology IDs use letters, digits, underscore, dot and hyphen"
            )
        reference = self._snapshot(ontology_id, raw, schema)
        with self.db.transaction() as db:
            exists = db.execute(
                "SELECT 1 FROM objects WHERE kind='ontology' AND id=?", (ontology_id,)
            ).fetchone()
            if exists:
                raise HarnessError(
                    "already_exists", "Ontology already exists; use a ChangeSet to update it"
                )
            self.db.put("version", f"{ontology_id}:{reference['version']}", reference, db)
            self.db.put(
                "ontology", ontology_id, {"id": ontology_id, "current": reference["version"]}, db
            )
            self.db.audit(
                self.auth.principal.subject, "import", ontology_id, "allowed", reference, db
            )
        return reference

    def read(self, ontology_id: str, version: str | None = None) -> dict[str, Any]:
        self.auth.require("read", ontology_id)
        if version is None or version == "current":
            version = self.db.get("ontology", ontology_id)["current"]
        reference = self.db.get("version", f"{ontology_id}:{version}")
        content = self.db.read_artifact(reference["content_hash"])
        self.auth.record("read", ontology_id, {"version": version})
        return {**reference, **content}

    def versions(self, ontology_id: str) -> list[dict[str, Any]]:
        self.auth.require("read", ontology_id)
        return [item for item in self.db.list("version") if item["ontology_id"] == ontology_id]

    def _evidence(self, ontology_id: str, reference: str) -> dict[str, Any]:
        self.auth.require("read", ontology_id)
        registration = self.db.get("document", reference)
        if ontology_id not in registration["ontology_ids"]:
            raise HarnessError(
                "resource_not_bound", "Evidence document is not registered for this ontology"
            )
        return self.db.read_artifact(reference)

    def propose(
        self,
        ontology_id: str,
        base_version: str,
        operations: list[dict[str, Any]],
        *,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        self.auth.require("propose", ontology_id)
        base = self.read(ontology_id, base_version)
        candidate = apply_operations(
            base["ontology"], operations, lambda ref: self._evidence(ontology_id, ref)
        )
        report = validate_ontology(candidate, base["schema"])
        body = {
            "schema_version": 1,
            "ontology_id": ontology_id,
            "base_version": base["version"],
            "revision": 1,
            "operations": operations,
        }
        content_hash = digest(body)
        changeset = {
            **body,
            "id": content_hash,
            "hash": content_hash,
            "validation": report,
            "run_id": run_id,
        }
        # Immutable content-addressed proposals; retries return the exact stored proposal.
        with self.db.transaction() as db:
            row = db.execute(
                "SELECT value FROM objects WHERE kind='changeset' AND id=?", (content_hash,)
            ).fetchone()
            if row:
                return self.db.get("changeset", content_hash, db)
            self.db.put("changeset", content_hash, changeset, db)
            self.db.audit(
                self.auth.principal.subject,
                "propose",
                ontology_id,
                "allowed",
                {"changeset": content_hash, "run_id": run_id},
                db,
            )
        return changeset

    def changeset(self, identifier: str) -> dict[str, Any]:
        value = self.db.get("changeset", identifier)
        self.auth.require("read", value["ontology_id"])
        body = {
            key: value[key]
            for key in ("schema_version", "ontology_id", "base_version", "revision", "operations")
        }
        if value["schema_version"] == 2:
            body.update(
                {
                    key: value[key]
                    for key in ("mode", "domain_schema", "submission_policy", "binding")
                }
            )
        if digest(body) != value["hash"] or value["id"] != value["hash"]:
            raise HarnessError("changeset_changed", "ChangeSet checksum does not match")
        return value

    def validate(self, identifier: str) -> dict[str, Any]:
        value = self.changeset(identifier)
        if value["schema_version"] == 2:
            candidate, schema = self.candidate_v2(value)
            return validate_ontology(candidate, schema)
        base = self.read(value["ontology_id"], value["base_version"])
        raw = apply_operations(
            base["ontology"],
            value["operations"],
            lambda ref: self._evidence(value["ontology_id"], ref),
        )
        return validate_ontology(raw, base["schema"])

    def diff(self, identifier: str) -> dict[str, Any]:
        value = self.changeset(identifier)
        return {
            "schema_version": 1,
            "changeset_id": identifier,
            "ontology_id": value["ontology_id"],
            "base_version": value["base_version"],
            "revision": value["revision"],
            "hash": value["hash"],
            "operations": value["operations"],
            "validation": self.validate(identifier),
        }

    def commit(
        self, identifier: str, *, expected_hash: str, revision: int, idempotency_key: str
    ) -> dict[str, Any]:
        value = self.changeset(identifier)
        if value["schema_version"] == 2:
            raise HarnessError(
                "confirmation_required",
                "Conversation edits require their exact review and a user confirmation",
            )
        ontology_id = value["ontology_id"]
        self.auth.require("write", ontology_id)
        if not idempotency_key or len(idempotency_key) > 200:
            raise HarnessError(
                "invalid_key", "Commit requires an idempotency key of at most 200 characters"
            )
        if expected_hash != value["hash"] or revision != value["revision"]:
            raise HarnessError("changeset_conflict", "ChangeSet revision or hash does not match")
        base = self.read(ontology_id, value["base_version"])
        raw = apply_operations(
            base["ontology"], value["operations"], lambda ref: self._evidence(ontology_id, ref)
        )
        reference = self._snapshot(ontology_id, raw, base["schema"])
        if reference["version"] == base["version"]:
            raise HarnessError("no_changes", "ChangeSet does not change the ontology")
        key = digest([self.auth.principal.subject, ontology_id, idempotency_key])
        with self.db.transaction() as db:
            if not self.auth.allowed("write", ontology_id):
                raise HarnessError("permission_denied", "Write permission was revoked")
            prior = db.execute(
                "SELECT value FROM objects WHERE kind='commit' AND id=?", (key,)
            ).fetchone()
            if prior:
                result = self.db.get("commit", key, db)
                if result["changeset_id"] != identifier:
                    raise HarnessError(
                        "idempotency_conflict", "Idempotency key was used for a different ChangeSet"
                    )
                return result
            current = self.db.get("ontology", ontology_id, db)
            if current["current"] != value["base_version"]:
                raise HarnessError("version_conflict", "Base version is no longer current")
            result = {
                **reference,
                "changeset_id": identifier,
                "parent_version": value["base_version"],
            }
            self.db.put("version", f"{ontology_id}:{reference['version']}", result, db)
            self.db.put(
                "ontology", ontology_id, {"id": ontology_id, "current": reference["version"]}, db
            )
            self.db.put("commit", key, result, db)
            self.db.audit(self.auth.principal.subject, "commit", ontology_id, "allowed", result, db)
        return result

    def candidate_v2(self, value: dict) -> tuple[dict, dict]:
        self.auth.require("read", value["ontology_id"])
        schema = value["domain_schema"]
        if value["mode"] == "create":
            raw: dict[str, Any] = {"metadata": {}, "entities": [], "relations": []}
            base_schema = None
        else:
            base = self.read(value["ontology_id"], value["base_version"])
            base_schema = base["schema"]
            raw = base["ontology"]
        approvals = value["binding"].get("schema_approval_ids", [])
        if approvals or (base_schema is not None and base_schema != schema):
            if not isinstance(approvals, list) or not approvals:
                raise HarnessError(
                    "confirmation_required", "Schema extensions require user approval"
                )
            bound_draft = self.db.get("draft", value["binding"]["draft_id"])
            expected = (
                digest(base_schema) if base_schema is not None else bound_draft["base_schema_ref"]
            )
            for identifier in approvals:
                approval = self.db.get("schema_proposal", identifier)
                if (
                    approval["status"] != "accepted"
                    or approval["subject"] != self.auth.principal.subject
                    or approval["ontology_id"] != value["ontology_id"]
                    or approval["conversation_id"] != value["binding"]["conversation_id"]
                    or approval["draft_id"] != value["binding"]["draft_id"]
                    or approval["before_ref"] != expected
                ):
                    raise HarnessError(
                        "confirmation_required", "Schema approval does not match this candidate"
                    )
                expected = approval["after_ref"]
            if expected != digest(schema):
                raise HarnessError(
                    "schema_conflict", "Candidate schema differs from approved extensions"
                )
        return apply_v2(
            raw, value["operations"], schema, lambda ref: self._evidence(value["ontology_id"], ref)
        ), schema

    def propose_v2(
        self,
        ontology_id: str,
        base_version: str | None,
        operations: list,
        schema: dict,
        binding: dict,
        *,
        mode: str,
    ) -> dict:
        self.auth.require("propose", ontology_id)
        if mode not in {"create", "extend"} or (mode == "extend" and not base_version):
            raise HarnessError("invalid_changeset", "An explicit draft base is required")
        body = {
            "schema_version": 2,
            "ontology_id": ontology_id,
            "base_version": base_version,
            "revision": 1,
            "operations": operations,
            "mode": mode,
            "domain_schema": schema,
            "submission_policy": "conversation_review",
            "binding": binding,
        }
        raw, domain_schema = self.candidate_v2(body)
        identifier = digest(body)
        value = {
            **body,
            "id": identifier,
            "hash": identifier,
            "validation": validate_ontology(raw, domain_schema),
        }
        self.db.put("changeset", identifier, value)
        return value

    def publish_review(self, value: dict, reference: dict, *, review: dict, db) -> dict:
        """Transaction participant; caller atomically consumes the matching user review."""
        resource = value["ontology_id"]
        if not self.auth.allowed("write", resource) or not self.auth.allowed("read", resource):
            raise HarnessError(
                "permission_denied", "Saving requires current read and write permission"
            )
        binding = {key: review[key] for key in ("conversation_id", "draft_id", "draft_revision")}
        if "schema_approval_ids" in review:
            binding["schema_approval_ids"] = review["schema_approval_ids"]
        if (
            value["submission_policy"] != "conversation_review"
            or value["binding"] != binding
            or value["hash"] != review["changeset_hash"]
            or reference["version"] != review["candidate_hash"]
        ):
            raise HarnessError("review_conflict", "Review does not describe this exact candidate")
        row = db.execute(
            "SELECT value FROM objects WHERE kind='ontology' AND id=?", (resource,)
        ).fetchone()
        if value["mode"] == "create":
            if row:
                raise HarnessError("version_conflict", "Another task already created this ontology")
        elif not row or self.db.get("ontology", resource, db)["current"] != value["base_version"]:
            raise HarnessError(
                "version_conflict",
                "The formal ontology changed; rebuild the draft on its latest version",
            )
        result = {**reference, "changeset_id": value["id"], "parent_version": value["base_version"]}
        self.db.put("version", f"{resource}:{reference['version']}", result, db)
        self.db.put("ontology", resource, {"id": resource, "current": reference["version"]}, db)
        self.db.put("commit", "review:" + review["id"], result, db)
        self.db.audit(self.auth.principal.subject, "commit", resource, "allowed", result, db)
        return result
