"""Load the project's existing JSON ontology without dropping custom fields."""

import hashlib
import json
from pathlib import Path
from typing import Any

from .domain import Ontology


def _index(records: Any, section: str) -> dict[str, dict[str, Any]]:
    if not isinstance(records, list):
        raise ValueError(f"Ontology {section} must be an array")
    result = {}
    for item in records:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ValueError(f"Each {section} record requires a string id")
        if not item["id"] or not isinstance(item.get("type"), str) or not item["type"]:
            raise ValueError(f"Each {section} record requires nonempty id and type")
        if item["id"] in result:
            raise ValueError(f"Duplicate {section} id: {item['id']}")
        result[item["id"]] = item
    return result


def load_ontology(path: Path) -> Ontology:
    path = path.resolve()
    content = path.read_bytes()
    raw = json.loads(content.decode("utf-8-sig"))
    entities = _index(raw["entities"], "entities")
    relations = _index(raw["relations"], "relations")
    for relation in relations.values():
        if relation.get("source") not in entities or relation.get("target") not in entities:
            raise ValueError(f"Unresolved ontology relation endpoint: {relation['id']}")
    return Ontology(path, hashlib.sha256(content).hexdigest(), raw, entities, relations)
