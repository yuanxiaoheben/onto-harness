import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ..common import HarnessError, canonical, now, positive_int, terms
from ..governance import Authorizer
from ..state import Database, RunStore


class MemoryStore:
    def __init__(self, database: Database, authorizer: Authorizer):
        self.db, self.auth = database, authorizer

    def _scope(self, namespace: str) -> str:
        if not namespace.strip() or len(namespace) > 100:
            raise HarnessError("invalid_namespace", "A bounded nonempty namespace is required")
        return canonical([self.auth.principal.subject, namespace])

    def put(
        self,
        content: str,
        *,
        namespace: str = "default",
        kind: str = "experience",
        source_run: str | None = None,
        ontology_id: str | None = None,
        version: str | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        scope = self._scope(namespace)
        if not isinstance(content, str) or not content.strip() or len(content) > 8000:
            raise HarnessError("invalid_memory", "Memory content must be 1–8000 characters")
        if kind not in {"experience", "preference", "correction"}:
            raise HarnessError("invalid_memory", "Unsupported memory kind")
        if version and not ontology_id:
            raise HarnessError("invalid_memory", "A version-bound memory needs an ontology ID")
        if ontology_id:
            self.auth.require("read", ontology_id)
        if source_run:
            RunStore(self.db, self.auth.principal.subject).get(source_run)
        if expires_at:
            try:
                expiry = datetime.fromisoformat(expires_at)
                if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
                    raise ValueError()
            except ValueError as error:
                raise HarnessError(
                    "invalid_memory", "expires_at must be a future timezone-aware ISO time"
                ) from error
        value: dict[str, Any] = {
            "schema_version": 1,
            "id": uuid4().hex,
            "scope": scope,
            "namespace": namespace,
            "kind": kind,
            "content": content,
            "source_run": source_run,
            "ontology_id": ontology_id,
            "version": version,
            "created_at": now(),
            "expires_at": expires_at,
        }
        self.db.put("memory", value["id"], value)
        self.auth.record("memory_put", ontology_id or scope, {"memory_id": value["id"]})
        return value

    def get(self, identifier: str, *, namespace: str = "default") -> dict[str, Any]:
        value = self.db.get("memory", identifier)
        if value["scope"] != self._scope(namespace):
            raise HarnessError("not_found", "Memory not found in this namespace")
        if value["ontology_id"]:
            self.auth.require("read", value["ontology_id"])
        return value

    def search(
        self,
        query: str = "",
        *,
        namespace: str = "default",
        top_k: int = 5,
        ontology_id: str | None = None,
        version: str | None = None,
    ) -> list[dict[str, Any]]:
        positive_int(top_k, "memory top_k", maximum=100)
        scope, query_terms = self._scope(namespace), terms(query)
        if ontology_id:
            self.auth.require("read", ontology_id)
        scored = []
        for value in self.db.list("memory"):
            if value["scope"] != scope:
                continue
            if value["expires_at"] and datetime.fromisoformat(value["expires_at"]) <= datetime.now(
                timezone.utc
            ):
                continue
            resource = value["ontology_id"]
            if resource and (resource != ontology_id or not self.auth.allowed("read", resource)):
                continue
            if value["version"] and value["version"] != version:
                continue
            score = len(query_terms & terms(value["content"]))
            if query_terms and score == 0:
                continue
            scored.append((score, value["created_at"], value["id"], value))
        return [item[3] for item in sorted(scored, key=lambda x: x[:3], reverse=True)[:top_k]]

    def delete(self, identifier: str, *, namespace: str = "default") -> None:
        value = self.db.get("memory", identifier)
        if value["scope"] != self._scope(namespace):
            raise HarnessError("not_found", "Memory not found in this namespace")
        with self.db.transaction() as db:
            db.execute("DELETE FROM objects WHERE kind='memory' AND id=?", (identifier,))
            self.db.audit(
                self.auth.principal.subject,
                "memory_delete",
                value["scope"],
                "allowed",
                {"memory_id": identifier},
                db,
            )

    def clear(self, *, namespace: str = "default") -> int:
        scope = self._scope(namespace)
        # Deletion is owner-scoped even if the linked ontology permission was revoked.
        with self.db.transaction() as db:
            rows = db.execute("SELECT id,value FROM objects WHERE kind='memory'").fetchall()
            identifiers = [row["id"] for row in rows if json.loads(row["value"])["scope"] == scope]
            db.executemany(
                "DELETE FROM objects WHERE kind='memory' AND id=?", [(i,) for i in identifiers]
            )
            self.db.audit(
                self.auth.principal.subject,
                "memory_clear",
                scope,
                "allowed",
                {"count": len(identifiers)},
                db,
            )
        return len(identifiers)
