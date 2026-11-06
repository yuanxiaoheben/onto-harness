"""Owner-scoped run retention and reference-aware artifact collection."""

import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from ..common import HarnessError, canonical, positive_int, validate_json
from ..state import WorkspaceLock

if TYPE_CHECKING:
    from .application import Application


def resolve_operation(app: "Application", run_id: str, result: dict[str, Any]) -> dict[str, Any]:
    with WorkspaceLock(app.db.root):
        state = app.inspect(run_id)
        if (
            state["kind"] != "agent"
            or state["status"] != "waiting_review"
            or not state.get("pending_operation")
        ):
            raise HarnessError("invalid_resolution", "Run is not waiting for an operation review")
        if set(result) != {"payload", "summary"} or not all(
            isinstance(result[key], dict) for key in result
        ):
            raise HarnessError(
                "invalid_resolution", "Provide a ToolResult object with payload and summary"
            )
        if len(canonical(result).encode("utf-8")) > 1_000_000:
            raise HarnessError("invalid_resolution", "Inspected result exceeds tool output limit")
        tool_name = state["decision"]["tool"]
        manifest = state["tool_manifest"][tool_name]
        if manifest["effect"] in {"propose", "write"}:
            app.authorizer.require(manifest["effect"], state["ontology_id"])
        if manifest.get("output_schema"):
            validate_json(result["payload"], manifest["output_schema"])
        with app.db.transaction() as db:
            changed = db.execute(
                "UPDATE operations SET status='done',value=? WHERE run_id=? AND id=? AND status='running'",
                (canonical(result), run_id, state["pending_operation"]),
            )
            if changed.rowcount != 1:
                raise HarnessError("operation_conflict", "Operation is no longer awaiting review")
            state["status"] = "pending"
            app.runs.save(state, db=db)
            app.db.audit(
                app.authorizer.principal.subject,
                "resolve_operation",
                state.get("ontology_id") or "run:" + run_id,
                "allowed",
                {"run_id": run_id, "operation_id": state["pending_operation"], "tool": tool_name},
                db,
            )
        return state


def cleanup(
    app: "Application", *, older_than_days: int = 30, apply: bool = False
) -> dict[str, Any]:
    positive_int(older_than_days, "older_than_days", maximum=36500)
    threshold = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    cutoff = time.time() - older_than_days * 86400
    with WorkspaceLock(app.db.root), app.db.transaction() as db:
        objects = db.execute("SELECT value FROM objects").fetchall()
        # Experience and proposals keep the provenance run available.
        protected: set[str] = set()
        for row in objects:
            value = json.loads(row[0])
            if isinstance(value, dict):
                protected.update(
                    str(value[key])
                    for key in ("source_run", "run_id", "parent_run_id", "child_run_id")
                    if value.get(key)
                )
        candidates = []
        for row in db.execute(
            "SELECT id,status,value,updated FROM runs WHERE subject=?",
            (app.authorizer.principal.subject,),
        ):
            state = json.loads(row["value"])
            child = state.get("bindings", {}).get("child_run_id")
            if child:
                protected.add(child)
            if (
                row["status"]
                in {"completed", "completed_with_errors", "cancelled", "failed", "budget_exhausted"}
                and datetime.fromisoformat(row["updated"]) < threshold
            ):
                candidates.append(row["id"])
        removed = sorted(set(candidates) - protected)
        roots: list[Any] = [json.loads(row[0]) for row in objects]
        for table, field in (
            ("runs", "value"),
            ("checkpoints", "value"),
            ("operations", "value"),
            ("events", "value"),
        ):
            id_column = "id" if table == "runs" else "run_id"
            for row in db.execute(f"SELECT {id_column},{field} FROM {table}"):
                if row[0] not in removed and row[1]:
                    roots.append(json.loads(row[1]))
        referenced: set[str] = set()
        pending = roots
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
            elif (
                isinstance(value, str)
                and re.fullmatch(r"[0-9a-f]{64}", value)
                and value not in referenced
            ):
                referenced.add(value)
                path = app.db.blobs / f"{value}.json"
                if path.is_file():
                    pending.append(app.db.read_artifact(value))
        blobs = [
            row[0]
            for row in db.execute("SELECT hash FROM artifact_pins WHERE touched<?", (cutoff,))
            if row[0] not in referenced
        ]
        if apply:
            for run_id in removed:
                for table in ("checkpoints", "operations", "events", "controls"):
                    db.execute(f"DELETE FROM {table} WHERE run_id=?", (run_id,))
                db.execute(
                    "DELETE FROM runs WHERE id=? AND subject=?",
                    (run_id, app.authorizer.principal.subject),
                )
            for key in blobs:
                if not re.fullmatch(r"[0-9a-f]{64}", key):
                    raise HarnessError("invalid_artifact", "Unsafe artifact name in store")
                target = (app.db.blobs / f"{key}.json").resolve()
                if target.parent != app.db.blobs.resolve():
                    raise HarnessError("invalid_artifact", "Artifact path escapes workspace")
                target.unlink(missing_ok=True)
                db.execute("DELETE FROM artifact_pins WHERE hash=?", (key,))
            app.db.audit(
                app.authorizer.principal.subject,
                "cleanup",
                "workspace",
                "allowed",
                {"runs": removed, "artifact_count": len(blobs)},
                db,
            )
        return {
            "schema_version": 1,
            "dry_run": not apply,
            "older_than_days": older_than_days,
            "run_ids": removed,
            "artifact_count": len(blobs),
            "protected_provenance_runs": len(protected),
        }
