"""Shared contracts; character offsets use Python string indices, end exclusive."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Ontology:
    path: Path
    sha256: str
    raw: dict[str, Any]
    entities_by_id: dict[str, dict[str, Any]]
    relations_by_id: dict[str, dict[str, Any]]

    @property
    def entity_types(self) -> set[str]:
        return {item["type"] for item in self.entities_by_id.values()}

    @property
    def relation_types(self) -> set[str]:
        return {item["type"] for item in self.relations_by_id.values()}


@dataclass
class Document:
    id: str
    path: str
    text: str
    sha256: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Chunk:
    id: str
    document_id: str
    text: str
    start: int
    end: int


@dataclass
class OntologySubgraph:
    """References into the original ontology, preserving its complete records."""

    entity_ids: list[str]
    relation_ids: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMRequest:
    messages: list[dict[str, str]]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    text: str
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtractionResult:
    document_id: str
    chunk_id: str
    entities: list[dict[str, Any]]
    relations: list[dict[str, Any]]


@dataclass(frozen=True)
class OntologyRef:
    ontology_id: str
    version: str
    content_hash: str


@dataclass
class ContextPack:
    schema_version: int
    messages: list[dict[str, str]]
    selected_entity_ids: list[str]
    selected_relation_ids: list[str]
    omitted_ids: list[str]
    input_bytes: int
    input_budget_bytes: int
    template_hash: str
    metadata: dict[str, Any] = field(default_factory=dict)
