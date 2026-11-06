"""Small FastAPI surface for the local ontology chat UI.

One workspace and one ontology per process. The default is read-only; explicit
editable mode adds persistent drafts and user-confirmed publication, guarded by
the same resource permissions as Python and CLI calls.
"""

import secrets
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..agent.presets import run_agent
from ..common import HarnessError, safe_error
from ..services.application import Application

STATIC_DIR = Path(__file__).with_name("static")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    run_id: str | None = Field(default=None, max_length=200)
    planner: Literal["scripted", "llm"] = "scripted"
    use_memory: bool = False
    namespace: str = Field(default="default", min_length=1, max_length=100)


class ConversationCreate(BaseModel):
    max_model_calls: int = Field(default=200, ge=1, le=10000)


class ConversationMessage(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    client_message_id: str = Field(
        default_factory=lambda: uuid4().hex, min_length=1, max_length=200
    )
    expected_revision: int = Field(ge=0)
    reply_to: str | None = Field(default=None, max_length=200)
    planner: Literal["scripted", "llm"] = "scripted"


def _http_status(code: str) -> int:
    if code == "permission_denied":
        return 403
    if code in {"not_found", "workspace_missing"}:
        return 404
    if code in {
        "already_exists",
        "version_conflict",
        "changeset_conflict",
        "conversation_conflict",
        "conversation_busy",
        "draft_conflict",
        "review_conflict",
        "review_expired",
        "workspace_busy",
    }:
        return 409
    return 400


def _public_state(state: dict[str, Any]) -> dict[str, Any]:
    """Return only the durable agent fields the browser needs."""

    return {
        key: state.get(key)
        for key in (
            "run_id",
            "status",
            "result_kind",
            "result",
            "steps",
            "tool_calls",
            "error",
        )
    }


def create_app(
    workspace: Path,
    ontology_id: str,
    *,
    version: str | None = None,
    planner: str = "scripted",
    title: str = "Ontology Harness",
    editable: bool = False,
    create_ontology: bool = False,
) -> FastAPI:
    """Build the local API and serve the bundled static chat client."""

    if planner not in {"scripted", "llm"}:
        raise HarnessError("invalid_planner", "Planner must be scripted or llm")
    application = Application(workspace)
    # Fail during startup rather than on the first browser request.
    if create_ontology:
        if not editable or version:
            raise HarnessError(
                "invalid_config", "Creating an ontology requires editable mode and no fixed version"
            )
        application.authorizer.require("read", ontology_id)
        application.authorizer.require("propose", ontology_id)
    else:
        application.ontologies.read(ontology_id, version)

    api = FastAPI(title=title, version="0.2.0")
    csrf_token = secrets.token_urlsafe(32)
    if editable:
        api.add_middleware(
            TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"]
        )

    @api.middleware("http")
    async def protect_writes(request: Request, call_next):
        if request.url.path.startswith("/api/conversations") and request.method == "POST":
            if not editable:
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": {
                            "code": "read_only",
                            "message": "Start with --editable to enable drafts",
                        }
                    },
                )
            token = request.headers.get("x-harness-csrf", "")
            if not secrets.compare_digest(token, csrf_token):
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": {
                            "code": "csrf_rejected",
                            "message": "Refresh this local page before editing",
                        }
                    },
                )
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": {
                            "code": "origin_rejected",
                            "message": "Cross-origin editing is disabled",
                        }
                    },
                )
            try:
                size = int(request.headers.get("content-length", "-1"))
            except ValueError:
                size = -1
            if not 0 <= size <= 65536:
                return JSONResponse(
                    status_code=413,
                    content={
                        "error": {
                            "code": "request_too_large",
                            "message": "Send a bounded JSON request",
                        }
                    },
                )
        return await call_next(request)

    def bound_conversation(identifier):
        if not editable:
            raise HarnessError("permission_denied", "Conversation editing is disabled")
        service = Application(workspace).conversations
        conv = service.get(identifier)
        if conv["ontology_id"] != ontology_id:
            raise HarnessError("not_found", "Conversation is outside this server binding")
        return service

    @api.exception_handler(HarnessError)
    async def harness_error(_request: Request, error: HarnessError) -> JSONResponse:
        return JSONResponse(
            status_code=_http_status(error.code), content={"error": error.as_dict()}
        )

    @api.exception_handler(Exception)
    async def unexpected_error(_request: Request, error: Exception) -> JSONResponse:
        return JSONResponse(status_code=500, content={"error": safe_error(error)})

    @api.get("/api/health", tags=["system"])
    def health() -> dict[str, Any]:
        return {"status": "ok", "ontology_id": ontology_id}

    @api.get("/api/info", tags=["system"])
    def info() -> dict[str, Any]:
        current = Application(workspace)
        try:
            snapshot = current.ontologies.read(ontology_id, version)
        except HarnessError as error:
            if not create_ontology or error.code != "not_found":
                raise
            snapshot = {"ontology": {"entities": [], "relations": []}, "version": None}
        ontology = snapshot["ontology"]
        return {
            "schema_version": 1,
            "workspace": str(application.workspace),
            "ontology_id": ontology_id,
            "version": snapshot["version"],
            "concept_count": len(ontology.get("entities", [])),
            "relation_count": len(ontology.get("relations", [])),
            "planner": planner,
            "model_provider": application.config["model"]["provider"],
            "model": application.config["model"].get("model"),
            "permissions": application.config["permissions"].get(ontology_id, []),
            "editable": editable,
            "create_ontology": create_ontology,
            "csrf_token": csrf_token if editable else None,
        }

    @api.get("/api/ontology", tags=["ontology"])
    def ontology_snapshot():
        """Complete bound formal snapshot, with freshly checked read permission."""
        snapshot = Application(workspace).ontologies.read(ontology_id, version)
        return {
            "ontology_id": ontology_id,
            "version": snapshot["version"],
            "view": "formal",
            "ontology": snapshot["ontology"],
            "schema": snapshot["schema"],
        }

    @api.post("/api/conversations", tags=["conversations"])
    def create_conversation(payload: ConversationCreate):
        service = Application(workspace).conversations
        spec: dict[str, Any] | None = None
        if create_ontology:
            with service.db.connect() as db:
                exists = db.execute(
                    "SELECT 1 FROM objects WHERE kind='ontology' AND id=?", (ontology_id,)
                ).fetchone()
            if not exists:
                spec = {}
        return service.create(
            ontology_id=ontology_id,
            base_version=version,
            create_spec=spec,
            max_model_calls=payload.max_model_calls,
        )

    @api.get("/api/conversations/{identifier}", tags=["conversations"])
    def conversation_info(identifier: str):
        return bound_conversation(identifier).get(identifier)

    @api.get("/api/conversations/{identifier}/messages", tags=["conversations"])
    def conversation_messages(identifier: str, after_seq: int = 0):
        return bound_conversation(identifier).messages(identifier, after_seq=after_seq)

    @api.get("/api/conversations/{identifier}/draft", tags=["conversations"])
    def conversation_draft(identifier: str):
        return bound_conversation(identifier).drafts.read(identifier)

    @api.post("/api/conversations/{identifier}/messages", tags=["conversations"])
    async def conversation_send(identifier: str, payload: ConversationMessage):
        return await bound_conversation(identifier).send(
            identifier,
            text=payload.message,
            client_message_id=payload.client_message_id,
            expected_revision=payload.expected_revision,
            reply_to=payload.reply_to,
            planner=payload.planner,
        )

    @api.post("/api/conversations/{identifier}/resume", tags=["conversations"])
    async def conversation_resume(identifier: str):
        return await bound_conversation(identifier).resume(identifier)

    @api.post("/api/conversations/{identifier}/reviews/{review_id}/present", tags=["conversations"])
    def conversation_present(identifier: str, review_id: str):
        return bound_conversation(identifier).present(identifier, review_id)

    @api.post("/api/chat", tags=["chat"])
    async def chat(payload: ChatRequest) -> dict[str, Any]:
        message = payload.message.strip()
        if not message:
            raise HarnessError("invalid_request", "Message must not be empty")
        if payload.run_id:
            previous = application.inspect(payload.run_id)
            if previous.get("ontology_id") != ontology_id:
                raise HarnessError("not_found", "Run belongs to another ontology")
            if previous.get("status") != "waiting_input":
                raise HarnessError("invalid_resume", "Run is not waiting for an answer")
            state = await application.resume(payload.run_id, answer=message)
        else:
            state = await run_agent(
                application,
                ontology_id,
                message,
                planner=payload.planner,
                use_memory=payload.use_memory,
                namespace=payload.namespace,
                version=version,
            )
        return _public_state(state)

    @api.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @api.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        return Response(status_code=204)

    api.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return api


def run_server(
    workspace: Path,
    ontology_id: str,
    *,
    version: str | None = None,
    planner: str = "scripted",
    host: str = "127.0.0.1",
    port: int = 8000,
    log_level: str = "info",
    editable: bool = False,
    create_ontology: bool = False,
) -> None:
    """Run the optional Uvicorn server, with a friendly missing-extra error."""

    try:
        import uvicorn
    except ImportError as error:
        raise HarnessError(
            "web_dependency_missing",
            'Install the web extra first: python -m pip install -e ".[web]"',
        ) from error
    if editable and host not in {"127.0.0.1", "localhost", "::1"}:
        raise HarnessError(
            "invalid_config", "Editable local server must bind to a loopback address"
        )
    uvicorn.run(
        create_app(
            workspace,
            ontology_id,
            version=version,
            planner=planner,
            editable=editable,
            create_ontology=create_ontology,
        ),
        host=host,
        port=port,
        log_level=log_level,
    )
