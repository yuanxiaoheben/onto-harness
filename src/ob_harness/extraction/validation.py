"""Validate the output contract, graph references, and verbatim evidence spans.

This checks structural consistency, not engineering validity or semantic entailment.
"""

import json
from typing import Any

from ..domain import Chunk, ExtractionResult, Ontology


def _string(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a nonempty string")
    return value


def _validate_evidence(item: dict[str, Any], chunk: Chunk) -> None:
    evidence = item.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError(f"{item['id']}: at least one evidence span is required")
    for span in evidence:
        if not isinstance(span, dict):
            raise ValueError("Each evidence span must be an object")
        quote = _string(span, "quote")
        start, end = span.get("start"), span.get("end")
        if type(start) is not int or type(end) is not int:
            raise ValueError("Evidence offsets must be integers")
        if not chunk.start <= start < end <= chunk.end:
            raise ValueError(f"{item['id']}: evidence lies outside the current chunk")
        if chunk.text[start - chunk.start : end - chunk.start] != quote:
            raise ValueError(f"{item['id']}: evidence quote does not match document text")


def _validate_items(
    items: Any,
    chunk: Chunk,
    reference: dict[str, dict[str, Any]],
    allowed_types: set[str],
    *,
    entities: bool,
) -> dict[str, dict[str, Any]]:
    if not isinstance(items, list):
        raise ValueError("entities and relations must be arrays")
    indexed = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Each extraction record must be an object")
        identifier = _string(item, "id")
        if identifier in indexed:
            raise ValueError(f"Duplicate extraction id: {identifier}")
        kind = _string(item, "type")
        if kind not in allowed_types:
            raise ValueError(f"Unknown ontology type: {kind}")
        if entities:
            _string(item, "name")
        if not isinstance(item.get("properties", {}), dict):
            raise ValueError(f"{identifier}: properties must be an object")
        ontology_id = item.get("ontology_id")
        if ontology_id is not None:
            if not isinstance(ontology_id, str) or ontology_id not in reference:
                raise ValueError(f"Unknown ontology_id: {ontology_id}")
            if reference[ontology_id]["type"] != kind:
                raise ValueError(f"{identifier}: ontology_id and type disagree")
        _validate_evidence(item, chunk)
        indexed[identifier] = item
    return indexed


def parse_and_validate(text: str, chunk: Chunk, ontology: Ontology) -> ExtractionResult:
    # Require JSON directly; the model adapter can remove provider-specific envelopes.
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"Model response is not JSON: {error.msg}") from error
    if not isinstance(payload, dict):
        raise ValueError("Model response must be a JSON object")
    entities = _validate_items(
        payload.get("entities"),
        chunk,
        ontology.entities_by_id,
        ontology.entity_types,
        entities=True,
    )
    relations = _validate_items(
        payload.get("relations"),
        chunk,
        ontology.relations_by_id,
        ontology.relation_types,
        entities=False,
    )
    for item in relations.values():
        source, target = _string(item, "source"), _string(item, "target")
        if source not in entities or target not in entities:
            raise ValueError(f"{item['id']}: relation endpoints must be entity ids in this chunk")
        if not isinstance(item.get("constraints", []), list):
            raise ValueError(f"{item['id']}: constraints must be an array")
        if not isinstance(item.get("application_conditions", ""), str):
            raise ValueError(f"{item['id']}: application_conditions must be a string")
        ontology_id = item.get("ontology_id")
        if ontology_id is not None:
            reference = ontology.relations_by_id[ontology_id]
            for endpoint in ("source", "target"):
                aligned_id = entities[item[endpoint]].get("ontology_id")
                if aligned_id is not None and aligned_id != reference[endpoint]:
                    raise ValueError(
                        f"{item['id']}: aligned endpoint disagrees with ontology relation"
                    )
    return ExtractionResult(
        chunk.document_id, chunk.id, list(entities.values()), list(relations.values())
    )
