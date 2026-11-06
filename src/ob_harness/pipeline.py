"""Prepare reproducible inputs and orchestrate one extraction experiment."""

import platform
from dataclasses import asdict, dataclass
from pathlib import Path
from shutil import copyfile
from typing import Any

from . import __version__
from .bootstrap import build_harness
from .config import RunConfig
from .documents.chunking import chunk_document
from .documents.loaders import discover_documents, load_document
from .domain import Chunk, Ontology
from .ontology import load_ontology
from .storage import create_run_dir, json_line, utc_now, write_json, write_jsonl


@dataclass
class PreparedRun:
    directory: Path
    ontology: Ontology
    chunks: list[Chunk]
    prompt_template: str
    manifest: dict[str, Any]


def prepare_run(config: RunConfig, inputs: list[Path]) -> PreparedRun:
    ontology = load_ontology(config.ontology_path)
    prompt_template = config.prompt_path.read_text(encoding="utf-8-sig")
    documents = [load_document(path) for path in discover_documents(inputs)]
    chunks = [
        chunk
        for document in documents
        for chunk in chunk_document(document, config.max_chars, config.overlap_chars)
    ]
    directory = create_run_dir(config.output_dir)
    manifest = {
        "run_id": directory.name,
        "experiment_name": config.experiment_name,
        "status": "prepared",
        "created_at": utc_now(),
        "package_version": __version__,
        "python_version": platform.python_version(),
        "source_ontology_path": str(ontology.path),
        "source_ontology_sha256": ontology.sha256,
        "document_count": len(documents),
        "chunk_count": len(chunks),
        "successful_chunks": 0,
        "invalid_chunks": 0,
        "entity_count": 0,
        "relation_count": 0,
        "aggregation": "per_chunk; cross-chunk entity resolution is not implemented",
    }
    write_json(directory / "config.json", config.snapshot())
    copyfile(ontology.path, directory / "ontology.json")
    (directory / "prompt.md").write_text(prompt_template, encoding="utf-8")
    write_jsonl(directory / "documents.jsonl", (asdict(document) for document in documents))
    write_jsonl(directory / "chunks.jsonl", (asdict(chunk) for chunk in chunks))
    write_json(directory / "manifest.json", manifest)
    return PreparedRun(directory, ontology, chunks, prompt_template, manifest)


def extract_prepared(config: RunConfig, prepared: PreparedRun) -> None:
    manifest, directory = prepared.manifest, prepared.directory
    manifest.update(status="running", started_at=utc_now())
    write_json(directory / "manifest.json", manifest)
    current_chunk_id = None
    try:
        harness = build_harness(config)
        with (
            (directory / "traces.jsonl").open("w", encoding="utf-8") as traces,
            (directory / "predictions.jsonl").open("w", encoding="utf-8") as predictions,
        ):
            for chunk in prepared.chunks:
                current_chunk_id = chunk.id
                record = harness.extract_chunk(chunk, prepared.ontology, prepared.prompt_template)
                traces.write(json_line(record))
                traces.flush()
                if record["status"] == "success":
                    extraction = record["extraction"]
                    predictions.write(json_line(extraction))
                    predictions.flush()
                    manifest["successful_chunks"] += 1
                    manifest["entity_count"] += len(extraction["entities"])
                    manifest["relation_count"] += len(extraction["relations"])
                else:
                    manifest["invalid_chunks"] += 1
        manifest["status"] = "completed_with_errors" if manifest["invalid_chunks"] else "completed"
    except Exception as error:
        manifest.update(status="failed", failed_chunk_id=current_chunk_id)
        manifest["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        manifest["finished_at"] = utc_now()
        write_json(directory / "manifest.json", manifest)
