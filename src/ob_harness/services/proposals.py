from typing import Any

from ..common import HarnessError, digest, normalized
from ..domain import Chunk
from ..ontologies.validation import merge_evidence

BUILD_CONTRACT = {
    "concepts": [
        {
            "id": "local identifier",
            "name": "concept name",
            "type": "declared concept type",
            "definition": "definition",
            "aliases": [],
            "evidence": [{"quote": "exact, case-sensitive verbatim text from this chunk"}],
        }
    ],
    "relations": [
        {
            "source": "existing or local concept ID",
            "target": "existing or local concept ID",
            "type": "declared relation type; follow its direction",
            "constraints": [],
            "application_conditions": "",
            "evidence": [{"quote": "exact, case-sensitive verbatim text from this chunk"}],
        }
    ],
}
BUILD_PROMPT = (
    "Propose ontology concepts and concept-to-concept relations supported by this source. "
    "Use only domain schema types. This task builds an ontology, not an instance database. "
    "Reuse visible concept IDs. New concepts use local IDs. Return concepts and relations arrays, "
    "including empty arrays if nothing is supported. For evidence copy exact source quotes with "
    "original casing and punctuation; choose quotes that occur only once in this chunk. The host "
    "locates character offsets, so do not count offsets yourself. Definitions may paraphrase only "
    "the supplied evidence; do not add external facts. Do not overwrite existing definitions."
)


def candidate_operations(
    payload: dict[str, Any],
    raw: dict[str, Any],
    chunk: Chunk,
    document_ref: str,
    document_text: str,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    if (
        set(payload) != {"concepts", "relations"}
        or not isinstance(payload["concepts"], list)
        or not isinstance(payload["relations"], list)
    ):
        raise HarnessError("invalid_proposal", "Proposal requires concepts and relations arrays")
    if len(payload["concepts"]) + len(payload["relations"]) > 500:
        raise HarnessError("invalid_proposal", "A chunk may propose at most 500 records")
    existing = {item["id"]: item for item in raw["entities"]}
    names: dict[str, set[str]] = {}
    for identifier, item in existing.items():
        for name in [item["name"], *item.get("aliases", [])]:
            names.setdefault(normalized(name), set()).add(identifier)
    local: dict[str, str] = {}
    operations, issues = [], []

    def evidence(item: dict[str, Any]) -> list[dict[str, Any]]:
        spans = item.get("evidence")
        if not isinstance(spans, list) or not spans:
            raise HarnessError("missing_evidence", "Each proposal record needs evidence")
        result = []
        for span in spans:
            if (
                not isinstance(span, dict)
                or "quote" not in span
                or set(span) - {"quote", "start", "end"}
            ):
                raise HarnessError(
                    "invalid_evidence", "Model evidence requires a quote and optional start/end"
                )
            start, end, quote = span.get("start"), span.get("end"), span["quote"]
            if not isinstance(quote, str) or not quote:
                raise HarnessError(
                    "invalid_evidence", "Evidence quote must be nonempty verbatim text"
                )
            if (
                type(start) is not int
                or type(end) is not int
                or not chunk.start <= start < end <= chunk.end
                or document_text[start:end] != quote
            ):
                first = chunk.text.find(quote)
                if first < 0:
                    raise HarnessError(
                        "invalid_evidence", "Evidence quote is absent from the current chunk"
                    )
                if chunk.text.find(quote, first + 1) >= 0:
                    raise HarnessError(
                        "ambiguous_evidence",
                        "Repeated quote needs exact absolute start/end offsets",
                    )
                original_start, original_end = start, end
                start, end = chunk.start + first, chunk.start + first + len(quote)
                issues.append(
                    {
                        "code": "evidence_quote_anchored",
                        "severity": "info",
                        "declared_start": str(original_start),
                        "declared_end": str(original_end),
                        "resolved_start": str(start),
                        "resolved_end": str(end),
                    }
                )
            result.append(
                {
                    "quote": quote,
                    "start": start,
                    "end": end,
                    "document_id": chunk.document_id,
                    "text_hash": digest(document_text),
                    "artifact": document_ref,
                }
            )
        return result

    for item in payload["concepts"]:
        if not isinstance(item, dict) or set(item) - {
            "id",
            "name",
            "type",
            "definition",
            "aliases",
            "evidence",
        }:
            raise HarnessError("invalid_proposal", "Unexpected concept fields")
        if any(
            not isinstance(item.get(key), str) or not item[key].strip()
            for key in ("id", "name", "type")
        ):
            raise HarnessError("invalid_proposal", "Concepts require local ID, name and type")
        if item["id"] in local:
            raise HarnessError("invalid_proposal", "Duplicate local concept ID")
        aliases, definition = item.get("aliases", []), item.get("definition", "")
        if (
            not isinstance(aliases, list)
            or any(not isinstance(a, str) or not a.strip() for a in aliases)
            or not isinstance(definition, str)
        ):
            raise HarnessError("invalid_proposal", "Invalid aliases or definition")
        matched = set().union(
            *(names.get(normalized(name), set()) for name in [item["name"], *aliases])
        )
        if item["id"] in existing:
            matched.add(item["id"])
        if len(matched) > 1:
            raise HarnessError(
                "ambiguous_alignment", "Multiple concepts match the proposed name or aliases"
            )
        spans = evidence(item)
        if matched:
            identifier = next(iter(matched))
            current = existing[identifier]
            if current["type"] != item["type"]:
                raise HarnessError(
                    "alignment_type_conflict", "Matched concept has a different type"
                )
            additions = sorted(
                set([item["name"], *aliases]) - {current["name"], *current.get("aliases", [])}
            )
            if definition and current.get("definition") and definition != current["definition"]:
                issues.append(
                    {
                        "code": "definition_conflict",
                        "concept_id": identifier,
                        "proposed_definition": definition,
                    }
                )
                definition = ""
            operation: dict[str, Any] = {
                "op": "enrich_concept",
                "id": identifier,
                "aliases": additions,
                "definition": definition if not current.get("definition") else "",
                "evidence": spans,
            }
        else:
            identifier = "c_" + digest([item["type"], normalized(item["name"])])[:24]
            record = {
                "id": identifier,
                "name": item["name"],
                "type": item["type"],
                "definition": definition,
                "aliases": sorted(set(aliases)),
            }
            if identifier in existing:
                raise HarnessError(
                    "id_conflict", "Generated concept ID collides with an existing concept"
                )
            existing[identifier] = record
            operation = {"op": "add_concept", "record": record, "evidence": spans}
        local[item["id"]] = identifier
        for name in [item["name"], *aliases]:
            names.setdefault(normalized(name), set()).add(identifier)
        operations.append(operation)
    relation_by_signature = {
        digest(
            [
                item["source"],
                item["type"],
                item["target"],
                item.get("constraints", []),
                item.get("application_conditions", ""),
            ]
        ): item
        for item in raw["relations"]
    }
    for item in payload["relations"]:
        if not isinstance(item, dict) or set(item) - {
            "source",
            "target",
            "type",
            "constraints",
            "application_conditions",
            "evidence",
        }:
            raise HarnessError("invalid_proposal", "Unexpected relation fields")
        if any(
            not isinstance(item.get(key), str) or not item[key].strip()
            for key in ("source", "target", "type")
        ):
            raise HarnessError("invalid_proposal", "Relations require source, target and type")
        source, target = (
            local.get(item["source"], item["source"]),
            local.get(item["target"], item["target"]),
        )
        if source not in existing or target not in existing:
            raise HarnessError(
                "unresolved_endpoint", "Relation endpoints must be existing or proposed concept IDs"
            )
        constraints, conditions = (
            item.get("constraints", []),
            item.get("application_conditions", ""),
        )
        if not isinstance(constraints, list) or not isinstance(conditions, str):
            raise HarnessError("invalid_proposal", "Invalid relation constraints or conditions")
        signature = digest([source, item["type"], target, constraints, conditions])
        matched_record = relation_by_signature.get(signature)
        if matched_record:
            record = {key: value for key, value in matched_record.items() if key != "evidence"}
        else:
            record = {
                "id": "r_" + signature[:24],
                "source": source,
                "target": target,
                "type": item["type"],
                "constraints": constraints,
                "application_conditions": conditions,
            }
        spans = evidence(item)
        operations.append(
            {"op": "add_relation", "record": record, "evidence": merge_evidence([], spans)}
        )
        relation_by_signature[signature] = record
    return operations, issues
