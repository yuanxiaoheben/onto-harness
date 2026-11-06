"""Version-two net edits. Assertions and editing instructions have separate provenance."""

from copy import deepcopy
from typing import Any, Callable

from ..common import HarnessError, digest
from .validation import validate_ontology

FIELDS = {
    "concept": {"name", "definition", "aliases", "type"},
    "relation": {"constraints", "application_conditions"},
}


def check_spans(spans: Any, loader: Callable, *, instructions: bool = False) -> None:
    if not isinstance(spans, list) or not spans:
        raise HarnessError("missing_evidence", "An exact source span is required")
    for span in spans:
        if not isinstance(span, dict) or set(span) != {
            "document_id",
            "artifact",
            "text_hash",
            "start",
            "end",
            "quote",
        }:
            raise HarnessError("invalid_evidence", "Malformed source reference")
        doc = loader(span["artifact"])
        start, end = span["start"], span["end"]
        if (
            type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= len(doc["text"])
            or doc["id"] != span["document_id"]
            or digest(doc["text"]) != span["text_hash"]
            or doc["text"][start:end] != span["quote"]
        ):
            raise HarnessError("invalid_evidence", "Source does not match its immutable snapshot")
        if instructions and doc.get("metadata", {}).get("source_kind") != "user_message":
            raise HarnessError(
                "invalid_instruction", "Editing instructions must originate in a user message"
            )


def net_operations(before: dict, after: dict, instructions: list) -> list[dict]:
    operations = []
    for section, kind in (("entities", "concept"), ("relations", "relation")):
        old = {r["id"]: r for r in before[section]}
        new = {r["id"]: r for r in after[section]}
        for identifier in sorted(old.keys() | new.keys()):
            if old.get(identifier) == new.get(identifier):
                continue
            op: dict[str, Any] = {"id": identifier, "instruction_refs": instructions}
            if identifier not in new:
                op.update(op=f"delete_{kind}", expected_record_hash=digest(old[identifier]))
            elif identifier not in old:
                op.update(op=f"add_{kind}", record=new[identifier])
            else:
                op.update(
                    op=f"update_{kind}",
                    expected_record_hash=digest(old[identifier]),
                    record=new[identifier],
                )
            operations.append(op)
    return operations


def apply_v2(raw: dict, operations: list, schema: dict, loader: Callable) -> dict:
    if not isinstance(operations, list) or len(operations) > 1000:
        raise HarnessError("invalid_changeset", "Expected at most 1000 edits")
    output = deepcopy(raw)
    indexes = {
        "concept": {r["id"]: r for r in output["entities"]},
        "relation": {r["id"]: r for r in output["relations"]},
    }
    touched = set()
    for op in operations:
        if not isinstance(op, dict) or op.get("op") not in {
            f"{action}_{kind}"
            for action in ("add", "update", "delete")
            for kind in ("concept", "relation")
        }:
            raise HarnessError("invalid_operation", "Unsupported v2 edit")
        action, kind = op["op"].split("_", 1)
        allowed = {"op", "id", "instruction_refs"}
        if action != "add":
            allowed.add("expected_record_hash")
        if action != "delete":
            allowed.add("record")
        if set(op) != allowed or not isinstance(op["id"], str):
            raise HarnessError("invalid_operation", "Unexpected edit fields")
        check_spans(op["instruction_refs"], loader, instructions=True)
        identifier, index = op["id"], indexes[kind]
        if (kind, identifier) in touched:
            raise HarnessError("invalid_operation", "Only one net edit per record is allowed")
        touched.add((kind, identifier))
        old = index.get(identifier)
        if action == "add":
            if old is not None:
                raise HarnessError("id_conflict", "Record already exists")
        elif old is None or digest(old) != op["expected_record_hash"]:
            raise HarnessError("record_conflict", "Record changed since this edit was prepared")
        if action == "delete":
            del index[identifier]
            continue
        record = deepcopy(op["record"])
        if not isinstance(record, dict) or record.get("id") != identifier:
            raise HarnessError("invalid_operation", "Record identity must remain stable")
        if kind == "relation":
            for field, expected in (("constraints", list), ("application_conditions", str)):
                if (
                    field in record
                    and (old is None or old.get(field) != record[field])
                    and not isinstance(record[field], expected)
                ):
                    raise HarnessError(
                        "invalid_operation", "Relation conditions have an invalid type"
                    )
        provenance = {"field_evidence", "assertion_origin"}
        fields = FIELDS[kind] | ({"source", "target", "type"} if kind == "relation" else set())
        if old:
            changed = {k for k in old.keys() | record.keys() if old.get(k) != record.get(k)}
            if changed - FIELDS[kind] - provenance:
                raise HarnessError(
                    "invalid_operation",
                    "Unsupported field change; replace relation endpoints using delete and add",
                )
        elif set(record) - fields - provenance - {"id"}:
            raise HarnessError("invalid_operation", "Unsupported added record fields")
        supports = record.get("field_evidence")
        if not isinstance(supports, dict):
            raise HarnessError("missing_evidence", "New assertions need field-level support")
        for field in fields:
            if field in record and (old is None or old.get(field) != record[field]):
                check_spans(supports.get(field), loader)
        for spans in supports.values():
            check_spans(spans, loader)
        if record.get("assertion_origin") != "user_asserted":
            raise HarnessError(
                "invalid_operation", "Conversation assertions must declare their origin"
            )
        index[identifier] = record
    output["entities"] = sorted(indexes["concept"].values(), key=lambda r: r["id"])
    output["relations"] = sorted(indexes["relation"].values(), key=lambda r: r["id"])
    report = validate_ontology(output, schema)
    if not report["passed"]:
        raise HarnessError(
            "invalid_ontology_proposal", "Edits violate types, relation endpoints or hierarchy DAG"
        )
    return output
