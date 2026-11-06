"""Resource-bound ontology tools. Formal commit is intentionally a trusted API/CLI action."""

from typing import TYPE_CHECKING, Any

from ..agent.contracts import Tool, ToolResult
from ..common import HarnessError
from ..services.build import execute_build

if TYPE_CHECKING:
    from ..services.application import Application


def ontology_tools(
    app: "Application",
    ontology_id: str,
    version: str,
    *,
    child_run_id: str | None = None,
    use_memory: bool = False,
    namespace: str = "default",
) -> list[Tool]:
    app.authorizer.require("read", ontology_id)
    string = {"type": "string", "minLength": 1, "maxLength": 8000}

    def schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

    def result(payload: dict[str, Any]) -> ToolResult:
        nodes = payload.get("entities", payload.get("concepts", []))
        edges = payload.get("relations", [])
        if payload.get("section") == "entities":
            nodes = [payload["record"]]
        if payload.get("section") == "relations":
            edges = [payload["record"]]
        report = payload.get("validation", payload)
        return ToolResult(
            payload,
            {
                "kind": payload.get("kind", "ontology_result"),
                "ontology_id": ontology_id,
                "version": version,
                "status": payload.get("status"),
                "entity_ids": [node["id"] for node in nodes][:10],
                "concepts": [
                    {
                        "id": node["id"],
                        "name": node.get("name", "")[:80],
                        "definition_excerpt": node.get("definition", "")[:120],
                    }
                    for node in nodes[:3]
                ],
                "relations": [
                    {key: edge[key] for key in ("id", "type", "source", "target")}
                    | {
                        "conditions_present": bool(
                            edge.get("constraints") or edge.get("application_conditions")
                        )
                    }
                    for edge in edges[:4]
                ],
                "query": payload.get("query"),
                "found_count": len(nodes),
                "passed": report.get("passed"),
                "summary_is_projection": True,
                "changeset_id": payload.get("changeset_id", payload.get("id")),
            },
        )

    async def get(arguments: dict[str, Any]) -> ToolResult:
        snapshot = app.ontologies.read(ontology_id, version)
        for section in ("entities", "relations"):
            for item in snapshot["ontology"][section]:
                if item["id"] == arguments["id"]:
                    return result(
                        {
                            "schema_version": 1,
                            "ontology_id": ontology_id,
                            "version": version,
                            "section": section,
                            "record": item,
                        }
                    )
        raise HarnessError("not_found", "Ontology object not found")

    async def search(arguments: dict[str, Any]) -> ToolResult:
        return result(app.search(ontology_id, arguments["query"], version=version))

    async def neighbors(arguments: dict[str, Any]) -> ToolResult:
        return result(
            app.neighbors(
                ontology_id, arguments["id"], version=version, limit=arguments.get("limit", 40)
            )
        )

    async def answer(arguments: dict[str, Any]) -> ToolResult:
        payload = app.answer(ontology_id, arguments["query"], version=version)
        if use_memory:
            context = app.context(
                ontology_id,
                arguments["query"],
                version=version,
                use_memory=True,
                namespace=namespace,
            )
            payload["context_summary"] = {
                "memory_ids": context["metadata"]["selected_memory_ids"],
                "input_bytes": context["input_bytes"],
                "lookup_uses_ontology_facts_only": True,
            }
        return result(payload)

    def bound_changeset(arguments: dict[str, Any]) -> str:
        identifier = arguments["changeset_id"]
        value = app.ontologies.changeset(identifier)
        if value["ontology_id"] != ontology_id or value["base_version"] != version:
            raise HarnessError(
                "resource_not_bound", "ChangeSet is outside this tool's bound version"
            )
        return identifier

    async def validate(arguments: dict[str, Any]) -> ToolResult:
        return result(app.ontologies.validate(bound_changeset(arguments)))

    async def diff(arguments: dict[str, Any]) -> ToolResult:
        return result(app.ontologies.diff(bound_changeset(arguments)))

    async def propose(arguments: dict[str, Any]) -> ToolResult:
        return result(app.ontologies.propose(ontology_id, version, arguments["operations"]))

    common: dict[str, Any] = {"effect": "read", "idempotent": True, "version": "3"}
    tools = [
        Tool(
            "ontology_get",
            "Read one bound ontology object by ID.",
            schema({"id": string}, ["id"]),
            get,
            **common,
        ),
        Tool(
            "ontology_search",
            "Intermediate concept search for planning. For a final concept lookup, use ontology_answer directly; it already performs search.",
            schema({"query": string}, ["query"]),
            search,
            **common,
        ),
        Tool(
            "ontology_neighbors",
            "Read adjacent ontology relations and endpoints.",
            schema(
                {"id": string, "limit": {"type": "integer", "minimum": 1, "maximum": 1000}}, ["id"]
            ),
            neighbors,
            **common,
        ),
        Tool(
            "ontology_answer",
            "Final tool for concept lookup questions: performs search and returns supported concept records and versioned citations. No preceding ontology_search is required.",
            schema({"query": string}, ["query"]),
            answer,
            terminal=True,
            result_kind="ontology_answer",
            **common,
        ),
        Tool(
            "ontology_validate",
            "Validate an evidence-backed ChangeSet for the bound version.",
            schema({"changeset_id": string}, ["changeset_id"]),
            validate,
            **common,
        ),
        Tool(
            "ontology_diff",
            "Inspect the operations and validation of a ChangeSet.",
            schema({"changeset_id": string}, ["changeset_id"]),
            diff,
            **common,
        ),
    ]
    if app.authorizer.allowed("propose", ontology_id):
        tools.append(
            Tool(
                "ontology_propose",
                "Save an evidence-backed ontology ChangeSet; does not commit it.",
                schema(
                    {
                        "operations": {
                            "type": "array",
                            "items": {"type": "object"},
                            "maxItems": 10000,
                        }
                    },
                    ["operations"],
                ),
                propose,
                effect="propose",
                idempotent=True,
                version="2",
            )
        )
    if child_run_id:
        app.authorizer.require("propose", ontology_id)

        async def build(_arguments: dict[str, Any]) -> ToolResult:
            state = app.inspect(child_run_id)
            if state["ontology_id"] != ontology_id or state["base_version"] != version:
                raise HarnessError(
                    "resource_not_bound", "Build run does not match the bound ontology"
                )
            state = await execute_build(app, child_run_id, locked=True)
            if state["status"] not in {"completed", "completed_with_errors"}:
                detail = state.get("error", {})
                raise HarnessError(
                    detail.get("code", "build_incomplete"),
                    detail.get(
                        "message", "Child build did not complete; inspect its run before resuming"
                    ),
                    retryable=detail.get("retryable", False),
                )
            return result(state["result"])

        tools.append(
            Tool(
                "ontology_build",
                "Build a draft from the documents already bound by the caller; finish with its ChangeSet.",
                schema({}, []),
                build,
                terminal=True,
                result_kind="ontology_build",
                effect="propose",
                idempotent=True,
                version="2",
                timeout_seconds=app.config["budget"]["timeout_seconds"],
            )
        )
    return tools
