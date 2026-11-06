import json
import os
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from ..common import HarnessError, canonical, digest, now

DDL = """
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS objects (
    kind TEXT NOT NULL, id TEXT NOT NULL, value TEXT NOT NULL,
    PRIMARY KEY(kind,id));
CREATE TABLE IF NOT EXISTS audit (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL,
    subject TEXT NOT NULL, action TEXT NOT NULL, resource TEXT NOT NULL,
    outcome TEXT NOT NULL, details TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, subject TEXT NOT NULL, kind TEXT NOT NULL,
    status TEXT NOT NULL, revision INTEGER NOT NULL,
    value TEXT NOT NULL, hash TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS checkpoints (
    run_id TEXT NOT NULL, revision INTEGER NOT NULL, value TEXT NOT NULL,
    hash TEXT NOT NULL, created TEXT NOT NULL, PRIMARY KEY(run_id,revision));
CREATE TABLE IF NOT EXISTS events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
    time TEXT NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS controls (run_id TEXT PRIMARY KEY, cancel INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS operations (
    run_id TEXT NOT NULL, id TEXT NOT NULL, signature TEXT NOT NULL,
    status TEXT NOT NULL, value TEXT, PRIMARY KEY(run_id,id));
CREATE TABLE IF NOT EXISTS artifact_pins (hash TEXT PRIMARY KEY, touched REAL NOT NULL);
"""


class Database:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "harness.sqlite3"
        self.blobs = self.root / "artifacts"
        self.blobs.mkdir(exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(DDL)
            version = db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
            if version and version[0] != "1":
                raise HarnessError("incompatible_store", "Unsupported workspace database version")
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('schema_version','1')")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def get(self, kind: str, identifier: str, db: sqlite3.Connection | None = None) -> Any:
        if db is None:
            with self.connect() as connection:
                return self.get(kind, identifier, connection)
        row = db.execute(
            "SELECT value FROM objects WHERE kind=? AND id=?", (kind, identifier)
        ).fetchone()
        if row is None:
            raise HarnessError("not_found", f"{kind} not found")
        return json.loads(row[0])

    def put(
        self, kind: str, identifier: str, value: Any, db: sqlite3.Connection | None = None
    ) -> None:
        if db is None:
            with self.transaction() as connection:
                self.put(kind, identifier, value, connection)
            return
        db.execute(
            "INSERT INTO objects VALUES (?,?,?) ON CONFLICT(kind,id) DO UPDATE SET value=excluded.value",
            (kind, identifier, canonical(value)),
        )

    def list(self, kind: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [
                json.loads(row[0])
                for row in db.execute("SELECT value FROM objects WHERE kind=? ORDER BY id", (kind,))
            ]

    def audit(
        self,
        subject: str,
        action: str,
        resource: str,
        outcome: str,
        details: dict[str, Any] | None = None,
        db: sqlite3.Connection | None = None,
    ) -> None:
        if db is None:
            with self.connect() as connection:
                self.audit(subject, action, resource, outcome, details, connection)
            return
        db.execute(
            "INSERT INTO audit(time,subject,action,resource,outcome,details) VALUES (?,?,?,?,?,?)",
            (now(), subject, action, resource, outcome, canonical(details or {})),
        )

    def artifact(self, value: Any) -> str:
        key = digest(value)
        target = self.blobs / f"{key}.json"
        # Pin before publishing/reusing a blob. Cleanup holds the same SQLite
        # write lock and ignores recently pinned artifacts, preventing races
        # between snapshot creation and its later metadata transaction.
        with self.transaction() as db:
            db.execute(
                "INSERT INTO artifact_pins VALUES (?,?) ON CONFLICT(hash) DO UPDATE SET touched=excluded.touched",
                (key, time.time()),
            )
            if not target.exists():
                temporary = self.blobs / f".{uuid4().hex}.tmp"
                try:
                    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                        stream.write(canonical(value))
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
            self.read_artifact(key)
        return key

    def read_artifact(self, key: str) -> Any:
        if len(key) != 64 or any(char not in "0123456789abcdef" for char in key):
            raise HarnessError("invalid_artifact", "Invalid artifact reference")
        try:
            value = json.loads((self.blobs / f"{key}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise HarnessError("artifact_missing", "Artifact is missing or unreadable") from error
        if digest(value) != key:
            raise HarnessError("artifact_changed", "Artifact checksum does not match")
        return value


class RunStore:
    def __init__(self, database: Database, subject: str):
        self.db, self.subject = database, subject

    def create(
        self,
        kind: str,
        state: dict[str, Any],
        thread_id: str | None = None,
        *,
        run_id: str | None = None,
    ) -> str:
        run_id = run_id or uuid4().hex
        state = {
            **state,
            "schema_version": 1,
            "run_id": run_id,
            "thread_id": thread_id or uuid4().hex,
            "subject": self.subject,
            "kind": kind,
            "status": "pending",
            "revision": 0,
            "created_at": now(),
            "elapsed_seconds": 0.0,
        }
        with self.db.transaction() as db:
            db.execute(
                "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?)",
                (run_id, self.subject, kind, "pending", 0, canonical(state), digest(state), now()),
            )
            db.execute("INSERT INTO controls VALUES (?,0)", (run_id,))
        self.event(run_id, "run_created", {"kind": kind})
        return run_id

    def get(self, run_id: str) -> dict[str, Any]:
        with self.db.connect() as db:
            row = db.execute(
                "SELECT value,hash FROM runs WHERE id=? AND subject=?", (run_id, self.subject)
            ).fetchone()
        if row is None:
            raise HarnessError("not_found", "Run not found")
        state = json.loads(row[0])
        if digest(state) != row[1] or state.get("schema_version") != 1:
            raise HarnessError("checkpoint_changed", "Run checkpoint is corrupt or incompatible")
        return state

    def save(self, state: dict[str, Any], *, db: sqlite3.Connection | None = None) -> None:
        if db is None:
            with self.db.transaction() as connection:
                self.save(state, db=connection)
            return
        next_revision = state["revision"] + 1
        updated = {**state, "revision": next_revision, "updated_at": now()}
        result = db.execute(
            "UPDATE runs SET status=?,revision=?,value=?,hash=?,updated=? WHERE id=? AND subject=? AND revision=?",
            (
                updated["status"],
                next_revision,
                canonical(updated),
                digest(updated),
                now(),
                state["run_id"],
                self.subject,
                state["revision"],
            ),
        )
        if result.rowcount != 1:
            raise HarnessError("run_conflict", "Run checkpoint was modified concurrently")
        db.execute(
            "INSERT INTO checkpoints VALUES (?,?,?,?,?)",
            (state["run_id"], next_revision, canonical(updated), digest(updated), now()),
        )
        state.update(updated)

    def event(self, run_id: str, kind: str, value: dict[str, Any] | None = None) -> None:
        self.get(run_id)
        with self.db.connect() as db:
            db.execute(
                "INSERT INTO events(run_id,time,kind,value) VALUES (?,?,?,?)",
                (run_id, now(), kind, canonical(value or {})),
            )

    def events(self, run_id: str, after: int = 0) -> list[dict[str, Any]]:
        self.get(run_id)
        with self.db.connect() as db:
            return [
                {**dict(row), "value": json.loads(row["value"])}
                for row in db.execute(
                    "SELECT * FROM events WHERE run_id=? AND sequence>? ORDER BY sequence",
                    (run_id, after),
                )
            ]

    def cancel(self, run_id: str) -> None:
        self.get(run_id)
        with self.db.connect() as db:
            db.execute("UPDATE controls SET cancel=1 WHERE run_id=?", (run_id,))
        self.event(run_id, "cancel_requested")

    def cancelled(self, run_id: str) -> bool:
        with self.db.connect() as db:
            row = db.execute("SELECT cancel FROM controls WHERE run_id=?", (run_id,)).fetchone()
        return bool(row and row[0])

    def operation(
        self, run_id: str, identifier: str, signature: str, *, create: bool = True
    ) -> dict[str, Any] | None:
        self.get(run_id)
        with self.db.transaction() as db:
            row = db.execute(
                "SELECT * FROM operations WHERE run_id=? AND id=?", (run_id, identifier)
            ).fetchone()
            if row:
                if row["signature"] != signature:
                    raise HarnessError("operation_conflict", "Operation arguments have changed")
                return {
                    "status": row["status"],
                    "value": json.loads(row["value"]) if row["value"] else None,
                }
            if create:
                db.execute(
                    "INSERT INTO operations VALUES (?,?,?,'running',NULL)",
                    (run_id, identifier, signature),
                )
        return None

    def complete_operation(self, state: dict[str, Any], identifier: str, value: Any) -> None:
        with self.db.transaction() as db:
            db.execute(
                "UPDATE operations SET status='done',value=? WHERE run_id=? AND id=?",
                (canonical(value), state["run_id"], identifier),
            )
            self.save(state, db=db)


class WorkspaceLock:
    """One active mutable executor per workspace; OS releases the lock on process exit."""

    def __init__(self, root: Path):
        self.path = root / ".execution.lock"
        self.stream: Any = None

    def __enter__(self) -> "WorkspaceLock":
        self.stream = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.stream.close()
            raise HarnessError("workspace_busy", "Another executor holds this workspace") from error
        return self

    def __exit__(self, *_args: Any) -> None:
        if sys.platform == "win32":
            import msvcrt

            self.stream.seek(0)
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        self.stream.close()
