from typing import Any

from ..common import HarnessError, normalized, positive_int, terms
from ..domain import Chunk, Ontology, OntologySubgraph


class SimpleRetriever:
    def __init__(self, settings: dict[str, Any] | None = None):
        self.settings = dict(settings or {})
        self.strategy = self.settings.get("strategy", "keyword")
        if self.strategy not in {"none", "keyword", "all"}:
            raise HarnessError("invalid_retriever", "Supported retrieval: none, keyword, all")
        self.top_k = positive_int(self.settings.get("top_k", 10), "top_k", maximum=10_000)
        self.max_nodes = positive_int(
            self.settings.get("max_nodes", 30), "max_nodes", maximum=10_000
        )
        self.max_edges = positive_int(
            self.settings.get("max_edges", 40), "max_edges", maximum=10_000
        )
        self.hops = self.settings.get("hops", 0)
        if type(self.hops) is not int or self.hops not in (0, 1):
            raise HarnessError("invalid_retriever", "hops must be 0 or 1")

    def search(self, query: str, raw: dict[str, Any], *, version: str = "") -> OntologySubgraph:
        nodes = {item["id"]: item for item in raw["entities"]}
        query_terms, normalized_query = terms(query), normalized(query)
        scores: dict[str, float] = {}
        hit_fields: dict[str, list[str]] = {}
        for identifier, item in nodes.items():
            aliases = item.get("aliases", [])
            aliases = aliases if isinstance(aliases, list) else []
            names = [value for value in [item.get("name", ""), *aliases] if isinstance(value, str)]
            name_terms = terms(" ".join(names))
            definition = item.get("definition", item.get("description", ""))
            definition_terms = terms(definition if isinstance(definition, str) else "")
            exact = any(normalized(name) and normalized(name) in normalized_query for name in names)
            score = (
                10.0 * exact
                + 2.0 * len(query_terms & name_terms)
                + len(query_terms & definition_terms)
            )
            if score > 0 or self.strategy == "all":
                scores[identifier] = score
                hit_fields[identifier] = (
                    (["name_or_alias_exact"] if exact else [])
                    + (["name_or_alias_terms"] if query_terms & name_terms else [])
                    + (["definition_terms"] if query_terms & definition_terms else [])
                )
        ranked = sorted(scores, key=lambda key: (-scores[key], key))
        if self.strategy == "none":
            ranked = []
        limit = self.max_nodes if self.strategy == "all" else min(self.top_k, self.max_nodes)
        selected = ranked[:limit]
        seeds = set(selected)
        relations: list[str] = []
        edges = sorted(
            raw["relations"],
            key=lambda edge: (
                -scores.get(edge["source"], 0) - scores.get(edge["target"], 0),
                edge["id"],
            ),
        )
        truncated = len(ranked) > len(selected)
        for edge in edges:
            endpoints = {edge["source"], edge["target"]}
            eligible = endpoints <= set(selected) or (self.hops == 1 and bool(endpoints & seeds))
            if self.strategy == "none" or not eligible:
                continue
            missing = endpoints - set(selected)
            if len(relations) >= self.max_edges or len(selected) + len(missing) > self.max_nodes:
                truncated = True
                continue
            selected.extend(sorted(missing))
            relations.append(edge["id"])
        return OntologySubgraph(
            selected,
            relations,
            {
                "strategy": self.strategy,
                "ontology_version": version,
                "scores": {key: scores.get(key, 0) for key in selected},
                "hit_fields": {key: hit_fields.get(key, []) for key in selected},
                "seed_ids": [key for key in selected if key in seeds],
                "expanded_ids": [key for key in selected if key not in seeds],
                "truncated": truncated,
                "empty_reason": "no_context"
                if self.strategy == "none"
                else ("no_match" if not selected else None),
                "normalization": "NFKC+casefold+whitespace; latin-words+cjk-bigrams/v1",
            },
        )

    def retrieve(self, chunk: Chunk, ontology: Ontology) -> OntologySubgraph:
        return self.search(chunk.text, ontology.raw, version=ontology.sha256)
