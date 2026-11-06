import hashlib
import json
from dataclasses import asdict
from importlib.resources import files
from pathlib import Path
from typing import Any

from .. import __version__
from ..common import HarnessError, digest, positive_int
from ..context import TemplateContextBuilder
from ..documents.artifacts import load_document_artifact
from ..documents.chunking import chunk_document
from ..documents.loaders import discover_documents, load_document
from ..governance import Authorizer, Principal
from ..memory import MemoryStore
from ..models import build_model
from ..models.inference import validate_settings
from ..ontologies import OntologyRepository
from ..retrieval import SimpleRetriever
from ..state import Database, RunStore
from .build import execute_build

DEFAULTS: dict[str, Any] = {
    "schema_version": 1,
    "subject": "local",
    "permissions": {},
    "retrieval": {"strategy": "keyword", "top_k": 10, "max_nodes": 30, "max_edges": 40, "hops": 0},
    "context": {"max_input_bytes": 32000, "history_bytes": 2000},
    "chunking": {"max_chars": 2400, "overlap_chars": 100},
    "budget": {"max_model_calls": 50, "timeout_seconds": 900},
    "tracing": {"save_raw": False},
    "model": {"provider": "replay", "responses_path": "examples/responses.json"},
}


class Application:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        path = self.workspace / "harness.json"
        if not path.is_file():
            raise HarnessError(
                "workspace_missing", "Initialize a workspace with ob-harness init first"
            )
        supplied = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(supplied, dict) or supplied.get("schema_version") != 1:
            raise HarnessError("invalid_config", "Workspace schema_version must be 1")
        self.config = {**DEFAULTS, **supplied}
        for key in ("retrieval", "context", "chunking", "budget", "tracing", "model"):
            if not isinstance(self.config[key], dict):
                raise HarnessError("invalid_config", f"{key} must be an object")
            self.config[key] = {**DEFAULTS[key], **self.config[key]}
        if not isinstance(self.config["subject"], str) or not self.config["subject"].strip():
            raise HarnessError("invalid_config", "A trusted local subject is required")
        if not isinstance(self.config["permissions"], dict):
            raise HarnessError(
                "invalid_config", "permissions must map ontology IDs to allowed actions"
            )
        for actions in self.config["permissions"].values():
            if not isinstance(actions, list) or any(
                action not in {"read", "propose", "write"} for action in actions
            ):
                raise HarnessError("invalid_config", "Supported permissions: read, propose, write")
        validate_settings(self.config["model"])
        if self.config["model"].get("responses_path"):
            self.config["model"]["responses_path"] = str(
                (self.workspace / self.config["model"]["responses_path"]).resolve()
            )
        positive_int(self.config["budget"]["max_model_calls"], "max_model_calls")
        positive_int(self.config["budget"]["timeout_seconds"], "timeout_seconds")
        positive_int(self.config["chunking"]["max_chars"], "max_chars")
        overlap = self.config["chunking"]["overlap_chars"]
        if type(overlap) is not int or not 0 <= overlap < self.config["chunking"]["max_chars"]:
            raise HarnessError("invalid_config", "Require 0 <= overlap_chars < max_chars")
        if type(self.config["tracing"]["save_raw"]) is not bool:
            raise HarnessError("invalid_config", "save_raw must be boolean")
        SimpleRetriever(self.config["retrieval"])
        TemplateContextBuilder(self.config["context"])
        self.db = Database(self.workspace / ".harness")
        principal = Principal(
            self.config["subject"],
            {key: frozenset(value) for key, value in self.config["permissions"].items()},
        )
        self.authorizer = Authorizer(self.db, principal)
        self.ontologies = OntologyRepository(self.db, self.authorizer)
        self.memories = MemoryStore(self.db, self.authorizer)
        self.runs = RunStore(self.db, principal.subject)

    @property
    def execution_hash(self) -> str:
        config = {
            key: self.config[key]
            for key in ("retrieval", "context", "chunking", "budget", "tracing", "model")
        }
        config["model"] = {
            key: value for key, value in self.config["model"].items() if key != "responses_path"
        }
        fixture = self.config["model"].get("responses_path")
        if (
            self.config["model"]["provider"] == "replay"
            and fixture
            and self.config["model"].get("responses") is None
        ):
            config = {
                **config,
                "fixture_hash": digest(json.loads(Path(fixture).read_text(encoding="utf-8-sig"))),
            }
        return digest(config)

    def model(self):
        return build_model(self.config["model"])

    @property
    def conversations(self):
        from .conversation import ConversationService

        return ConversationService(self)

    def search(
        self,
        ontology_id: str,
        query: str,
        *,
        version: str | None = None,
        retrieval: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        snapshot = self.ontologies.read(ontology_id, version)
        subgraph = SimpleRetriever(retrieval or self.config["retrieval"]).search(
            query, snapshot["ontology"], version=snapshot["version"]
        )
        nodes, edges = set(subgraph.entity_ids), set(subgraph.relation_ids)
        self.authorizer.require("read", ontology_id)
        return {
            "schema_version": 1,
            "ontology_id": ontology_id,
            "version": snapshot["version"],
            "entity_ids": subgraph.entity_ids,
            "query": query,
            "relation_ids": subgraph.relation_ids,
            "entities": [node for node in snapshot["ontology"]["entities"] if node["id"] in nodes],
            "relations": [
                edge for edge in snapshot["ontology"]["relations"] if edge["id"] in edges
            ],
            "metadata": subgraph.metadata,
        }

    def answer(self, ontology_id: str, query: str, *, version: str | None = None) -> dict[str, Any]:
        result = self.search(ontology_id, query, version=version)
        return {
            "schema_version": 1,
            "kind": "ontology_answer",
            "ontology_id": ontology_id,
            "version": result["version"],
            "query": query,
            "mode": "deterministic_lookup",
            "status": "found" if result["entities"] else "insufficient_context",
            "concepts": result["entities"],
            "relations": result["relations"],
            "citations": [
                {"ontology_id": ontology_id, "version": result["version"], "id": key}
                for key in [*result["entity_ids"], *result["relation_ids"]]
            ],
            "metadata": result["metadata"],
        }

    def neighbors(
        self, ontology_id: str, identifier: str, *, version: str | None = None, limit: int = 40
    ) -> dict[str, Any]:
        positive_int(limit, "neighbor limit", maximum=1000)
        snapshot = self.ontologies.read(ontology_id, version)
        nodes = {node["id"]: node for node in snapshot["ontology"]["entities"]}
        if identifier not in nodes:
            raise HarnessError("not_found", "Concept not found")
        edges = sorted(
            (
                edge
                for edge in snapshot["ontology"]["relations"]
                if identifier in {edge["source"], edge["target"]}
            ),
            key=lambda e: e["id"],
        )
        selected = edges[:limit]
        ids = {identifier} | {
            endpoint for edge in selected for endpoint in (edge["source"], edge["target"])
        }
        return {
            "schema_version": 1,
            "ontology_id": ontology_id,
            "version": snapshot["version"],
            "entities": [nodes[key] for key in sorted(ids)],
            "relations": selected,
            "truncated": len(edges) > limit,
        }

    def context(
        self,
        ontology_id: str,
        query: str,
        *,
        version: str | None = None,
        use_memory: bool = False,
        namespace: str = "default",
    ) -> dict[str, Any]:
        snapshot = self.ontologies.read(ontology_id, version)
        subgraph = SimpleRetriever(self.config["retrieval"]).search(
            query, snapshot["ontology"], version=snapshot["version"]
        )
        memories = (
            self.memories.search(
                query, namespace=namespace, ontology_id=ontology_id, version=snapshot["version"]
            )
            if use_memory
            else []
        )
        pack = TemplateContextBuilder(self.config["context"]).pack(
            "ontology_query",
            query,
            snapshot["ontology"],
            snapshot["schema"],
            subgraph,
            output_contract={
                "answer": "supported ontology records",
                "citations": "ontology object IDs",
            },
            source={"ontology_id": ontology_id, "version": snapshot["version"]},
            memories=memories,
        )
        return asdict(pack)

    def create_build(
        self,
        ontology_id: str,
        inputs: list[Path],
        *,
        version: str | None = None,
        use_memory: bool = False,
        namespace: str = "default",
        thread_id: str | None = None,
    ) -> str:
        self.authorizer.require("propose", ontology_id)
        snapshot = self.ontologies.read(ontology_id, version)
        documents = []
        ordinary = []
        for path in inputs:
            if path.is_file() and path.suffix.lower() == ".json":
                document = load_document_artifact(path)
                text_hash = document.metadata.get("text_sha256")
                if (
                    text_hash
                    and text_hash != hashlib.sha256(document.text.encode("utf-8")).hexdigest()
                ):
                    raise HarnessError(
                        "artifact_changed", "Parsed document text hash does not match"
                    )
                documents.append(document)
            else:
                ordinary.append(path)
        if ordinary:
            documents.extend(load_document(path) for path in discover_documents(ordinary))
        if not documents:
            raise HarnessError(
                "no_documents", "Provide readable documents or standard parsed artifacts"
            )
        by_id: dict[str, dict[str, Any]] = {}
        for document in documents:
            value = asdict(document)
            if document.id in by_id and by_id[document.id] != value:
                raise HarnessError(
                    "document_id_conflict", "Different document snapshots use the same ID"
                )
            by_id[document.id] = value
        document_refs = [self.db.artifact(value) for value in by_id.values()]
        with self.db.transaction() as db:
            for reference in document_refs:
                present = db.execute(
                    "SELECT 1 FROM objects WHERE kind='document' AND id=?", (reference,)
                ).fetchone()
                registration = (
                    self.db.get("document", reference, db)
                    if present
                    else {"artifact": reference, "ontology_ids": []}
                )
                registration["ontology_ids"] = sorted(
                    set([*registration["ontology_ids"], ontology_id])
                )
                self.db.put("document", reference, registration, db)
        chunks = [
            asdict(chunk)
            for document in documents
            for chunk in chunk_document(document, **self.config["chunking"])
        ]
        # Deduplicate repeated input references while retaining a deterministic order.
        chunks = list({chunk["id"]: chunk for chunk in chunks}.values())
        return self.runs.create(
            "build",
            {
                "ontology_id": ontology_id,
                "base_version": snapshot["version"],
                "package_version": __version__,
                "config_hash": self.execution_hash,
                "authorization_hash": self.authorizer.fingerprint,
                "inputs_ref": self.db.artifact({"documents": document_refs, "chunks": chunks}),
                "operations_ref": self.db.artifact([]),
                "index": 0,
                "phase": "prepare",
                "workspace_revision": 0,
                "invalid_chunks": 0,
                "traces": [],
                "model_calls": 0,
                "usage": {"input_tokens": 0, "output_tokens": 0, "unknown_calls": 0},
                "use_memory": use_memory,
                "memory_namespace": namespace,
            },
            thread_id=thread_id,
        )

    async def build(self, ontology_id: str, inputs: list[Path], **options: Any) -> dict[str, Any]:
        run_id = self.create_build(ontology_id, inputs, **options)
        return await execute_build(self, run_id)

    def inspect(self, run_id: str) -> dict[str, Any]:
        state = self.runs.get(run_id)
        if state.get("ontology_id"):
            self.authorizer.require("read", state["ontology_id"])
        return state

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        positive_int(limit, "run limit", maximum=1000)
        with self.db.connect() as db:
            rows = db.execute(
                "SELECT value FROM runs WHERE subject=? ORDER BY updated DESC LIMIT ?",
                (self.authorizer.principal.subject, limit),
            ).fetchall()
        result = []
        for row in rows:
            state = json.loads(row[0])
            if state.get("ontology_id") and not self.authorizer.allowed(
                "read", state["ontology_id"]
            ):
                continue
            result.append(
                {
                    key: state.get(key)
                    for key in (
                        "run_id",
                        "thread_id",
                        "kind",
                        "status",
                        "ontology_id",
                        "base_version",
                        "created_at",
                        "updated_at",
                    )
                }
            )
        return result

    async def resume(self, run_id: str, *, answer: str | None = None) -> dict[str, Any]:
        state = self.inspect(run_id)
        if state.get("bindings", {}).get("conversation_id"):
            if answer is not None:
                raise HarnessError(
                    "invalid_resume", "Send conversation clarifications as a new user message"
                )
            return await self.conversations.resume(
                state["bindings"]["conversation_id"], turn_id=state["bindings"]["turn_id"]
            )
        if state["status"] in {"completed", "completed_with_errors"}:
            return state
        if state["kind"] == "build":
            if answer:
                raise HarnessError(
                    "invalid_resume", "Build runs do not accept clarification answers"
                )
            return await execute_build(self, run_id, clear_cancel=True)
        if state["kind"] == "agent":
            from ..agent.presets import resume_agent

            return await resume_agent(self, state, answer=answer)
        raise HarnessError("incorrect_run_kind", "No executor is registered for this run kind")

    def doctor(self) -> dict[str, Any]:
        import importlib.util
        import os
        import sys

        from ..tools.registry import ToolRegistry

        key_env = self.config["model"].get(
            "api_key_env",
            "DEEPSEEK_API_KEY" if self.config["model"].get("provider") == "deepseek" else "",
        )
        fixture = self.config["model"].get("responses_path")
        return {
            "schema_version": 1,
            "package_version": __version__,
            "python": sys.version.split()[0],
            "workspace": str(self.workspace),
            "database": "ready",
            "network_checked": False,
            "model_provider": self.config["model"]["provider"],
            "credential_present": bool(os.environ.get(key_env)) if key_env else None,
            "replay_fixture_exists": Path(fixture).is_file() if fixture else None,
            "langgraph_installed": importlib.util.find_spec("langgraph") is not None,
            "tools": ToolRegistry().list(),
            "model_inference_checked": False,
        }


def initialize(workspace: Path, *, writable: bool = False) -> dict[str, Any]:
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    config_path = workspace / "harness.json"
    if config_path.exists() or (workspace / ".harness" / "harness.sqlite3").exists():
        raise HarnessError(
            "already_initialized", "Workspace already exists; initialization will not overwrite it"
        )
    sample = json.loads(
        files("ob_harness").joinpath("resources/minimal.json").read_text(encoding="utf-8")
    )
    examples = workspace / "examples"
    examples.mkdir(exist_ok=True)
    expected_files = ["ontology.json", "schema.json", "responses.json"] + [
        f"part-{index}.txt" for index in range(1, len(sample["documents"]) + 1)
    ]
    if any((examples / name).exists() for name in expected_files):
        raise HarnessError("file_exists", "Example files already exist; use an empty workspace")
    responses = []
    for index, document in enumerate(sample["documents"], start=1):
        text = document["text"]
        target = examples / f"part-{index}.txt"
        if target.exists():
            raise HarnessError("file_exists", "Example files already exist; use an empty workspace")
        target.write_text(text, encoding="utf-8")
        response = document["response"]
        for record in [*response["concepts"], *response["relations"]]:
            record["evidence"] = [{"quote": text, "start": 0, "end": len(text)}]
        # The second fixture references the stable ID assigned to the first concept.
        for relation in response["relations"]:
            if relation["target"] == "@whale":
                relation["target"] = "c_" + digest(["concept", "whale"])[:24]
        responses.append(
            {"match": {"task": "build", "input_hash": digest(text)}, "response": response}
        )
        responses.append(
            {
                "match": {"task": "extract", "input_hash": digest(text)},
                "response": {"entities": [], "relations": []},
            }
        )
    for name, value in (
        ("ontology.json", sample["ontology"]),
        ("schema.json", sample["schema"]),
        ("responses.json", responses),
    ):
        target = examples / name
        if target.exists():
            raise HarnessError("file_exists", "Example files already exist; use an empty workspace")
        target.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    config = {
        **DEFAULTS,
        "permissions": {"demo": ["read", "propose", "write"] if writable else ["read"]},
    }
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    app = Application(workspace)
    bootstrap_auth = Authorizer(
        app.db, Principal("workspace_initializer", {"demo": frozenset({"write"})})
    )
    reference = OntologyRepository(app.db, bootstrap_auth).import_ontology(
        "demo", sample["ontology"], sample["schema"]
    )
    return {
        "schema_version": 1,
        "workspace": str(workspace),
        "ontology": reference,
        "permissions": config["permissions"],
        "model": "replay-fixture",
        "real_inference": False,
    }
