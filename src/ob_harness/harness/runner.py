"""One-chunk orchestration, independent of model providers and retrieval algorithms."""

from dataclasses import asdict
from typing import Any

from ..domain import Chunk, Ontology
from ..extraction.validation import parse_and_validate
from .contracts import ContextBuilder, LanguageModel, SubgraphRetriever


class ExtractionHarness:
    def __init__(
        self,
        retriever: SubgraphRetriever,
        context_builder: ContextBuilder,
        model: LanguageModel,
    ):
        self.retriever = retriever
        self.context_builder = context_builder
        self.model = model

    def extract_chunk(
        self, chunk: Chunk, ontology: Ontology, prompt_template: str
    ) -> dict[str, Any]:
        subgraph = self.retriever.retrieve(chunk, ontology)
        entity_ids = set(subgraph.entity_ids)
        if not entity_ids <= ontology.entities_by_id.keys():
            raise ValueError("Retriever returned unknown ontology entity ids")
        if not set(subgraph.relation_ids) <= ontology.relations_by_id.keys():
            raise ValueError("Retriever returned unknown ontology relation ids")
        for relation_id in subgraph.relation_ids:
            relation = ontology.relations_by_id[relation_id]
            if not {relation["source"], relation["target"]} <= entity_ids:
                raise ValueError(f"Subgraph omits relation endpoints: {relation_id}")
        request = self.context_builder.build(chunk, ontology, subgraph, prompt_template)
        response = self.model.generate(request)
        # Retain raw output even if parsing or structural validation fails.
        record = {
            "document_id": chunk.document_id,
            "chunk_id": chunk.id,
            "subgraph": asdict(subgraph),
            "request": asdict(request),
            "response": asdict(response),
        }
        try:
            result = parse_and_validate(response.text, chunk, ontology)
        except ValueError as error:
            record.update(status="invalid", error=str(error))
        else:
            record.update(status="success", extraction=asdict(result))
        return record
