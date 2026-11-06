"""Discover tool plugins through package metadata, then bind application-owned resources."""

from dataclasses import dataclass, replace
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path
from typing import Any, Mapping, Protocol

from ..agent.contracts import Tool
from ..common import HarnessError

ENTRY_POINT_GROUP = "ob_harness.tools"
BUILTINS = {
    "ontology_get",
    "ontology_search",
    "ontology_neighbors",
    "ontology_answer",
    "ontology_propose",
    "ontology_validate",
    "ontology_diff",
    "ontology_build",
}
CATALOG = {
    "mineru": {
        "description": "Parse PDF documents with the optional MinerU plugin",
        "package": "ob-harness-mineru",
        "install": 'python -m pip install -e . -e "plugins/mineru"',
    },
}


@dataclass(frozen=True)
class ToolContext:
    """Trusted application configuration; paths are never supplied by the planner."""

    documents: Mapping[str, Path]
    output_dir: Path
    timeout_seconds: int = 3600
    application: Any = None
    ontology_id: str | None = None
    ontology_version: str | None = None
    child_run_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.timeout_seconds) is not int or self.timeout_seconds <= 0:
            raise ValueError("Tool timeout must be a positive integer")


class ToolPlugin(Protocol):
    name: str

    def status(self) -> dict[str, Any]: ...

    def bind(self, context: ToolContext) -> Tool: ...


class BuiltinOntologyPlugin:
    def __init__(self, name: str):
        self.name = name

    def status(self) -> dict[str, Any]:
        return {
            "ready": True,
            "builtin": True,
            "requires_binding": "application, ontology_id, fixed version",
        }

    def bind(self, context: ToolContext) -> Tool:
        from ..common import HarnessError
        from .ontology import ontology_tools

        if context.application is None or not context.ontology_id:
            raise HarnessError("resource_not_bound", "Bind a workspace application and ontology ID")
        snapshot = context.application.ontologies.read(
            context.ontology_id, context.ontology_version
        )
        tools = ontology_tools(
            context.application,
            context.ontology_id,
            snapshot["version"],
            child_run_id=context.child_run_id,
        )
        for tool in tools:
            if tool.name == self.name:
                return tool
        raise HarnessError(
            "capability_unavailable", "Tool requires proposal permission or bound build documents"
        )


class ToolRegistry:
    """Installation enables discovery, not automatic registration into every agent."""

    def __init__(self) -> None:
        self.entries: dict[str, EntryPoint] = {}
        for entry in entry_points(group=ENTRY_POINT_GROUP):
            if entry.name in self.entries or entry.name in BUILTINS:
                raise ValueError(f"Duplicate installed tool name: {entry.name}")
            self.entries[entry.name] = entry

    def list(self) -> list[dict[str, Any]]:
        # Do not import third-party plugin code just to enumerate installations.
        return [
            {
                "name": name,
                **CATALOG.get(name, {}),
                "installed": name in self.entries or name in BUILTINS,
                "builtin": name in BUILTINS,
            }
            for name in sorted(set(CATALOG) | set(self.entries) | BUILTINS)
        ]

    def _load(self, name: str) -> ToolPlugin:
        if name in BUILTINS:
            return BuiltinOntologyPlugin(name)
        if name not in self.entries:
            hint = CATALOG.get(name, {}).get(
                "install", "Install a plugin exposing ob_harness.tools"
            )
            raise HarnessError("missing_tool", f"Tool {name!r} is not installed. {hint}")
        plugin = self.entries[name].load()
        if plugin.name != name:
            raise ValueError("Entry point name and plugin name disagree")
        return plugin

    def info(self, name: str) -> dict[str, Any]:
        known = next((row for row in self.list() if row["name"] == name), None)
        if known is None:
            raise HarnessError("unknown_tool", f"Unknown tool: {name}")
        return {**known, **(self._load(name).status() if known["installed"] else {"ready": False})}

    def bind(self, name: str, context: ToolContext) -> Tool:
        plugin = self._load(name)
        status = plugin.status()
        if not status.get("ready"):
            raise HarnessError(
                "tool_not_ready", f"Tool {name!r} is not ready: {status.get('reason')}"
            )
        tool = plugin.bind(context)
        tool = replace(
            tool,
            timeout_seconds=min(tool.timeout_seconds, context.timeout_seconds)
            if name in BUILTINS
            else context.timeout_seconds,
        )
        if tool.name != name:
            raise ValueError("Bound tool name and plugin name disagree")
        if context.application is not None:
            original = tool.invoke

            async def audited(arguments: dict[str, Any]):
                auth = context.application.authorizer
                resource = context.ontology_id or "bound_documents"
                auth.record("tool_started", resource, {"tool": name})
                try:
                    result = await original(arguments)
                except BaseException:
                    auth.db.audit(
                        auth.principal.subject, "tool_finished", resource, "failed", {"tool": name}
                    )
                    raise
                auth.record("tool_finished", resource, {"tool": name})
                return result

            tool = replace(tool, invoke=audited)
        return tool
