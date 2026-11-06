"""Add a small bilingual teaching ontology through the normal versioned repository."""

import argparse
import hashlib
import json
from importlib.resources import files
from pathlib import Path

from ob_harness.common import HarnessError, digest, normalized
from ob_harness.services import Application
from ob_harness.state import WorkspaceLock


def seed(workspace: Path, ontology_id="demo", *, apply=False):
    app = Application(workspace)
    with WorkspaceLock(app.db.root):
        base = app.ontologies.read(ontology_id)
        spec = base["schema"]["relation_types"].get("is_a", {})
        if (
            "concept" not in base["schema"]["concept_types"]
            or spec.get("direction") != "child_to_parent"
        ):
            raise HarnessError(
                "schema_conflict", "Preset requires concept and child-to-parent is_a"
            )
        preset = json.loads(
            files("ob_harness.resources").joinpath("animals20.json").read_text(encoding="utf-8")
        )
        text = "原创教学示例：动物概念的简化分类，非权威生物分类标准。\n" + "\n".join(
            f"{row['name']}（{row['label']}）：{row['definition']} 父概念：{row['parent'] or '无'}。"
            for row in preset
        )
        document = {
            "id": "preset_animals20_" + digest(text)[:16],
            "text": text,
            "path": "resource://ob_harness/animals20.json",
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "metadata": {"source_kind": "curated_example", "authoritative": False},
        }
        document_ref = digest(document)
        evidence = [
            {
                "document_id": document["id"],
                "artifact": document_ref,
                "text_hash": digest(text),
                "start": 0,
                "end": len(text),
                "quote": text,
            }
        ]
        raw = base["ontology"]
        identifiers, operations = {}, []
        used = {node["id"] for node in raw["entities"]}
        for row in preset:
            matches = [
                node
                for node in raw["entities"]
                if {normalized(row["name"]), normalized(row["label"])}
                & {normalized(n) for n in [node["name"], *node.get("aliases", [])]}
            ]
            if len(matches) > 1:
                raise HarnessError("ambiguous_alignment", f"Multiple concepts match {row['name']}")
            if matches:
                node = matches[0]
                if node["type"] != "concept":
                    raise HarnessError("schema_conflict", "Matched concept has a different type")
                identifiers[row["id"]] = node["id"]
                if row["label"] not in node.get("aliases", []) and row["label"] != node["name"]:
                    operations.append(
                        {
                            "op": "enrich_concept",
                            "id": node["id"],
                            "aliases": [row["label"]],
                            "evidence": evidence,
                        }
                    )
            else:
                if row["id"] in used:
                    raise HarnessError("id_conflict", f"Preset ID occupied: {row['id']}")
                identifiers[row["id"]] = row["id"]
                used.add(row["id"])
                operations.append(
                    {
                        "op": "add_concept",
                        "record": {
                            "id": row["id"],
                            "name": row["name"],
                            "type": "concept",
                            "definition": row["definition"],
                            "aliases": [row["label"]],
                        },
                        "evidence": evidence,
                    }
                )
        edges = {(r["source"], r["target"], r["type"]) for r in raw["relations"]}
        edge_ids = {r["id"] for r in raw["relations"]}
        for row in preset:
            if not row["parent"]:
                continue
            source, target = identifiers[row["id"]], identifiers[row["parent"]]
            if (source, target, "is_a") in edges:
                continue
            identifier = "seed_is_a_" + digest([source, target])[:20]
            if identifier in edge_ids:
                raise HarnessError("id_conflict", "Preset relation ID occupied")
            operations.append(
                {
                    "op": "add_relation",
                    "record": {
                        "id": identifier,
                        "source": source,
                        "target": target,
                        "type": "is_a",
                    },
                    "evidence": evidence,
                }
            )
        result = {
            "ontology_id": ontology_id,
            "apply": apply,
            "operations": len(operations),
            "concept_count": len(used),
            "base_version": base["version"],
        }
        if apply and operations:
            app.authorizer.require("propose", ontology_id)
            app.authorizer.require("write", ontology_id)
            app.db.artifact(document)
            try:
                resources = app.db.get("document", document_ref)["ontology_ids"]
            except HarnessError as error:
                if error.code != "not_found":
                    raise
                resources = []
            app.db.put(
                "document",
                document_ref,
                {"artifact": document_ref, "ontology_ids": sorted(set(resources) | {ontology_id})},
            )
            proposal = app.ontologies.propose(ontology_id, base["version"], operations)
            committed = app.ontologies.commit(
                proposal["id"],
                expected_hash=proposal["hash"],
                revision=proposal["revision"],
                idempotency_key="animals20:" + proposal["hash"],
            )
            result["version"] = committed["version"]
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace", type=Path, default=Path(__file__).resolve().parents[1] / "workspace"
    )
    parser.add_argument("--ontology", default="demo")
    parser.add_argument(
        "--apply", action="store_true", help="Publish additions; default only previews"
    )
    args = parser.parse_args()
    print(
        json.dumps(
            seed(args.workspace, args.ontology, apply=args.apply), ensure_ascii=False, indent=2
        )
    )
