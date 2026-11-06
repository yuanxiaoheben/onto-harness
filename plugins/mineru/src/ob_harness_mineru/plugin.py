"""MinerU 4.x stateless file conversion, exposed as an explicitly bound tool."""

import asyncio
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import Any
from uuid import uuid4

from ob_harness.agent.contracts import InvalidDecision, Tool, ToolResult
from ob_harness.domain import Document
from ob_harness.tools.registry import ToolContext

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "document_id": {"type": "string", "minLength": 1},
        "tier": {
            "type": "string",
            "enum": ["flash", "basic", "standard", "advanced"],
            "default": "standard",
        },
        "ocr_mode": {"type": "string", "enum": ["auto", "txt", "ocr"], "default": "auto"},
    },
    "required": ["document_id"],
    "additionalProperties": False,
}


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


async def stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if sys.platform == "win32":
        # No shell. Terminate this worker's process tree, including OCR/model children.
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(process.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            await asyncio.wait_for(killer.wait(), timeout=10)
        except asyncio.TimeoutError:
            killer.kill()
            await killer.wait()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.wait()


class MinerUPlugin:
    name = "mineru"

    def status(self) -> dict[str, Any]:
        try:
            package = distribution("mineru")
        except PackageNotFoundError:
            return {"ready": False, "reason": "MinerU dependency is missing"}
        ready = package.version.split(".")[0] == "4" and any(
            entry.group == "console_scripts" and entry.name == "mineru-kit"
            for entry in package.entry_points
        )
        return {
            "ready": ready,
            "version": package.version,
            "input_schema": SCHEMA,
            "reason": None if ready else "Requires MinerU 4.x with mineru-kit",
            "readiness_scope": "Package metadata only; model weights and hardware not checked",
        }

    def bind(self, context: ToolContext) -> Tool:
        documents = {key: Path(value).resolve() for key, value in context.documents.items()}
        output_dir = Path(context.output_dir).resolve()
        schema = deepcopy(SCHEMA)
        schema["properties"]["document_id"]["enum"] = sorted(documents)

        async def invoke(arguments: dict[str, Any]) -> ToolResult:
            if not isinstance(arguments, dict) or set(arguments) - SCHEMA["properties"].keys():
                raise InvalidDecision("Unexpected MinerU tool arguments")
            identifier = arguments.get("document_id")
            tier, mode = arguments.get("tier", "standard"), arguments.get("ocr_mode", "auto")
            if not isinstance(identifier, str) or identifier not in documents:
                raise InvalidDecision("Select a document ID bound by the application")
            if tier not in ("flash", "basic", "standard", "advanced") or mode not in (
                "auto",
                "txt",
                "ocr",
            ):
                raise InvalidDecision("Unsupported MinerU tier or OCR mode")
            source = documents[identifier]
            if not source.is_file() or source.suffix.lower() != ".pdf":
                raise ValueError("MinerU tool currently accepts a single local PDF")
            status = self.status()
            if not status["ready"]:
                raise RuntimeError(str(status["reason"]))
            job = output_dir / ("mineru_" + uuid4().hex)
            job.mkdir(parents=True, exist_ok=False)
            snapshot, markdown = job / "source.pdf", job / "document.md"
            manifest: dict[str, Any] = {
                "schema": "ob-harness.tool-run/v1",
                "tool": self.name,
                "status": "running",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "arguments": arguments,
                "mineru_version": status["version"],
                "source_path": str(source),
                "timeout_seconds": context.timeout_seconds,
            }
            write_json(job / "manifest.json", manifest)
            process = None
            try:
                shutil.copyfile(source, snapshot)
                source_hash = digest(snapshot)
                manifest["source_sha256"] = source_hash
                argv = [
                    sys.executable,
                    "-m",
                    "ob_harness_mineru.worker",
                    "parse",
                    str(snapshot),
                    "-o",
                    str(markdown),
                    "--format",
                    "markdown",
                    "--pages",
                    "all",
                    "--tier",
                    tier,
                    "--ocr-mode",
                    mode,
                ]
                manifest["argv"] = argv
                write_json(job / "manifest.json", manifest)
                options = (
                    {"creationflags": subprocess.CREATE_NO_WINDOW}
                    if os.name == "nt"
                    else {"start_new_session": True}
                )
                with (job / "mineru.log").open("wb") as log:
                    starting = asyncio.create_task(
                        asyncio.create_subprocess_exec(
                            *argv,
                            stdin=asyncio.subprocess.DEVNULL,
                            stdout=log,
                            stderr=log,
                            **options,
                        )
                    )
                    try:
                        process = await asyncio.shield(starting)
                        code = await asyncio.wait_for(
                            process.wait(), timeout=context.timeout_seconds
                        )
                    finally:
                        if process is None:
                            process = await asyncio.shield(starting)
                        if process.returncode is None:
                            await asyncio.shield(stop_process(process))
                if code != 0:
                    raise RuntimeError(
                        f"MinerU exited with code {code}; inspect {job / 'mineru.log'}"
                    )
                if not markdown.is_file():
                    raise RuntimeError("MinerU did not produce the requested Markdown artifact")
                text = markdown.read_text(encoding="utf-8-sig")
                if not text.strip():
                    raise ValueError("MinerU produced no readable text")
                if digest(snapshot) != source_hash:
                    raise RuntimeError("Input snapshot changed during parsing")
                text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                document_hash = hashlib.sha256(
                    json.dumps(
                        [source_hash, text_hash, status["version"], tier, mode],
                    ).encode("utf-8")
                ).hexdigest()
                document = Document(
                    "doc_" + document_hash[:20],
                    str(snapshot),
                    text,
                    source_hash,
                    {
                        "format": ".pdf",
                        "parser": "mineru",
                        "parser_version": status["version"],
                        "original_path": str(source),
                        "offset_basis": "mineru_markdown",
                        "text_sha256": text_hash,
                        "tier": tier,
                        "ocr_mode": mode,
                        "requested_pages": "all",
                        "coverage_verified": False,
                    },
                )
                write_json(
                    job / "document.json",
                    {"schema": "ob-harness.document/v1", "document": asdict(document)},
                )
                manifest.update(
                    status="complete",
                    characters=len(text),
                    artifacts={
                        p.relative_to(job).as_posix(): {
                            "sha256": digest(p),
                            "bytes": p.stat().st_size,
                        }
                        for p in job.rglob("*")
                        if p.is_file() and p.name != "manifest.json"
                    },
                )
            except BaseException as error:
                manifest.update(
                    status="cancelled" if isinstance(error, asyncio.CancelledError) else "failed",
                    error={"type": type(error).__name__, "message": str(error)},
                )
                raise
            finally:
                manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
                write_json(job / "manifest.json", manifest)
            return ToolResult(
                {
                    "schema": "ob-harness.parsed-document/v1",
                    "tool": self.name,
                    "status": "complete",
                    "document_id": document.id,
                    "directory": str(job),
                    "manifest": str(job / "manifest.json"),
                    "document": str(job / "document.json"),
                    "markdown": str(markdown),
                    "source_sha256": source_hash,
                    "characters": len(text),
                },
                {
                    "document_id": document.id,
                    "characters": len(text),
                    "artifact": str(job / "document.json"),
                },
            )

        return Tool(
            self.name,
            "Parse a bound PDF into a text artifact; does not construct an ontology.",
            schema,
            invoke,
            False,
            "parsed_document",
        )
