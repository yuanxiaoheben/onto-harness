from copy import deepcopy
from typing import Any, Callable

from ..common import HarnessError, canonical, digest


def validate_schema(schema: dict[str, Any]) -> None:
    if not isinstance(schema, dict) or schema.get("schema_version") != 1:
        raise HarnessError("invalid_schema", "Ontology schema_version must be 1")
    concepts, relations = schema.get("concept_types"), schema.get("relation_types")
    if (
        not isinstance(concepts, list)
        or not concepts
        or any(not isinstance(x, str) or not x.strip() for x in concepts)
    ):
        raise HarnessError("invalid_schema", "concept_types must contain nonempty strings")
    if len(set(concepts)) != len(concepts) or not isinstance(relations, dict):
        raise HarnessError("invalid_schema", "Duplicate concept types or invalid relation_types")
    for name, specification in relations.items():
        if not name or not isinstance(specification, dict):
            raise HarnessError("invalid_schema", "Relation types require named specifications")
        if type(specification.get("hierarchical", False)) is not bool:
            raise HarnessError("invalid_schema", "hierarchical must be boolean")
        if type(specification.get("acyclic", False)) is not bool:
            raise HarnessError("invalid_schema", "acyclic must be boolean")
        if "description" in specification and not isinstance(specification["description"], str):
            raise HarnessError("invalid_schema", "Relation description must be a string")
        for field in ("source_types", "target_types"):
            allowed = specification.get(field, concepts)
            if (
                not isinstance(allowed, list)
                or not allowed
                or any(not isinstance(value, str) for value in allowed)
                or not set(allowed) <= set(concepts)
            ):
                raise HarnessError(
                    "invalid_schema", "Relation endpoint types must exist in concept_types"
                )
        if specification.get("direction") not in ("child_to_parent", "parent_to_child", "directed"):
            raise HarnessError("invalid_schema", "Each relation type must declare its direction")
        if specification.get("hierarchical") and specification["direction"] == "directed":
            raise HarnessError(
                "invalid_schema", "Hierarchical relations must declare parent/child direction"
            )


def validate_ontology(raw: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    validate_schema(schema)
    issues: list[dict[str, str]] = []

    def issue(code: str, location: str, severity: str = "error") -> None:
        issues.append({"code": code, "location": location, "severity": severity})

    if not isinstance(raw, dict):
        return {
            "schema_version": 1,
            "passed": False,
            "issues": [{"code": "invalid_ontology", "severity": "error", "location": "$"}],
        }
    entities: dict[str, dict[str, Any]] = {}
    relations: dict[str, dict[str, Any]] = {}
    for section, record_index in (("entities", entities), ("relations", relations)):
        records = raw.get(section)
        if not isinstance(records, list):
            issue("expected_array", section)
            continue
        for index, item in enumerate(records):
            location = f"{section}[{index}]"
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("id"), str)
                or not item["id"].strip()
            ):
                issue("invalid_record_id", location)
                continue
            if item["id"] in record_index:
                issue("duplicate_id", location)
            record_index[item["id"]] = item
            if section == "entities":
                if (
                    not isinstance(item.get("type"), str)
                    or item["type"] not in schema["concept_types"]
                ):
                    issue("unknown_concept_type", location)
                if not isinstance(item.get("name"), str) or not item["name"].strip():
                    issue("missing_concept_name", location)
                if not isinstance(item.get("definition", ""), str):
                    issue("invalid_definition", location)
                if not isinstance(item.get("aliases", []), list) or any(
                    not isinstance(a, str) or not a.strip() for a in item.get("aliases", [])
                ):
                    issue("invalid_aliases", location)
    # Kahn's algorithm avoids recursion limits on deep taxonomies. Check the union
    # of declared hierarchical types as well as each type separately.
    graphs: dict[tuple[str, str], list[tuple[str, str]]] = {("hierarchy", "union"): []}
    for identifier, relation in relations.items():
        kind = relation.get("type")
        specification = schema["relation_types"].get(kind) if isinstance(kind, str) else None
        if specification is None:
            issue("unknown_relation_type", identifier)
            continue
        source, target = relation.get("source"), relation.get("target")
        if (
            not isinstance(source, str)
            or not isinstance(target, str)
            or source not in entities
            or target not in entities
        ):
            issue("unresolved_endpoint", identifier)
            continue
        for endpoint, field in ((source, "source_types"), (target, "target_types")):
            if entities[endpoint].get("type") not in specification.get(
                field, schema["concept_types"]
            ):
                issue("invalid_endpoint_type", identifier)
        if specification.get("hierarchical"):
            edge = (
                (target, source)
                if specification["direction"] == "parent_to_child"
                else (source, target)
            )
            graphs.setdefault(("hierarchy", str(kind)), []).append(edge)
            graphs[("hierarchy", "union")].append(edge)
        elif specification.get("acyclic"):
            graphs.setdefault(("acyclic", str(kind)), []).append((source, target))
        if relation.get("constraints") or relation.get("application_conditions"):
            issue("unformalized_constraint", identifier, "unknown")
    for (graph_type, kind), edges in graphs.items():
        outgoing: dict[str, set[str]] = {}
        degree: dict[str, int] = {}
        for source, target in edges:
            outgoing.setdefault(source, set()).add(target)
            degree.setdefault(source, 0)
            degree.setdefault(target, 0)
        for targets in outgoing.values():
            for target in targets:
                degree[target] += 1
        ready = [node for node, count in degree.items() if count == 0]
        visited = 0
        while ready:
            node = ready.pop()
            visited += 1
            for target in outgoing.get(node, ()):
                degree[target] -= 1
                if degree[target] == 0:
                    ready.append(target)
        if visited != len(degree):
            issue("hierarchy_cycle" if graph_type == "hierarchy" else "relation_cycle", kind)
    return {
        "schema_version": 1,
        "passed": not any(i["severity"] == "error" for i in issues),
        "issues": issues,
        "checks": ["records", "types", "endpoints", "hierarchy_dag"],
        "semantic_entailment": "unknown",
    }


def merge_evidence(existing: list[Any], added: list[Any]) -> list[Any]:
    merged = {canonical(item): item for item in [*existing, *added]}
    return [merged[key] for key in sorted(merged)]


def apply_operations(
    raw: dict[str, Any],
    operations: list[dict[str, Any]],
    evidence_loader: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(operations, list) or len(operations) > 10_000:
        raise HarnessError("invalid_changeset", "Expected at most 10000 operations")
    output = deepcopy(raw)
    entities = {item["id"]: item for item in output["entities"]}
    relations = {item["id"]: item for item in output["relations"]}
    for operation in operations:
        if not isinstance(operation, dict) or operation.get("op") not in {
            "add_concept",
            "add_relation",
            "enrich_concept",
        }:
            raise HarnessError("invalid_operation", "Unsupported ontology operation")
        evidence = operation.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise HarnessError("missing_evidence", "Each ontology operation needs evidence")
        for span in evidence:
            if (
                not isinstance(span, dict)
                or not {"document_id", "text_hash", "artifact", "start", "end", "quote"}
                <= span.keys()
            ):
                raise HarnessError(
                    "invalid_evidence", "Evidence requires a document snapshot and span"
                )
            start, end = span["start"], span["end"]
            if (
                type(start) is not int
                or type(end) is not int
                or not 0 <= start < end
                or not isinstance(span["quote"], str)
            ):
                raise HarnessError("invalid_evidence", "Invalid evidence span")
            if evidence_loader:
                document = evidence_loader(span["artifact"])
                if (
                    document["id"] != span["document_id"]
                    or end > len(document["text"])
                    or digest(document["text"]) != span["text_hash"]
                    or document["text"][start:end] != span["quote"]
                ):
                    raise HarnessError(
                        "invalid_evidence", "Evidence does not match the immutable document"
                    )
        if operation["op"] == "enrich_concept":
            if set(operation) - {"op", "id", "aliases", "definition", "evidence"}:
                raise HarnessError("invalid_operation", "Unexpected enrichment fields")
            identifier = operation.get("id")
            if not isinstance(identifier, str) or identifier not in entities:
                raise HarnessError("invalid_operation", "Enriched concept does not exist")
            item = entities[identifier]
            definition, aliases = operation.get("definition", ""), operation.get("aliases", [])
            if (
                not isinstance(definition, str)
                or not isinstance(aliases, list)
                or any(not isinstance(a, str) or not a.strip() for a in aliases)
            ):
                raise HarnessError("invalid_operation", "Invalid definition or aliases")
            if definition:
                if item.get("definition") and item["definition"] != definition:
                    raise HarnessError(
                        "definition_conflict", "Existing definitions cannot be overwritten"
                    )
                item["definition"] = definition
            item["aliases"] = sorted(set(item.get("aliases", []) + aliases))
        else:
            if set(operation) != {"op", "record", "evidence"}:
                raise HarnessError("invalid_operation", "Unexpected add-operation fields")
            record = operation.get("record")
            if isinstance(record, dict) and "evidence" in record:
                raise HarnessError(
                    "invalid_operation", "Attach evidence to the operation, not inside its record"
                )
            if (
                not isinstance(record, dict)
                or not isinstance(record.get("id"), str)
                or not record["id"]
            ):
                raise HarnessError("invalid_operation", "Added records require an ID")
            target = entities if operation["op"] == "add_concept" else relations
            identifier = record["id"]
            if identifier in target:
                without_evidence = {k: v for k, v in target[identifier].items() if k != "evidence"}
                if without_evidence != {k: v for k, v in record.items() if k != "evidence"}:
                    raise HarnessError("id_conflict", "An existing ID has different content")
                item = target[identifier]
            else:
                item = deepcopy(record)
                target[identifier] = item
        item["evidence"] = merge_evidence(item.get("evidence", []), evidence)
    output["entities"], output["relations"] = list(entities.values()), list(relations.values())
    return output
