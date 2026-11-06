from typing import Protocol

from ..domain import Chunk, LLMRequest, LLMResponse, Ontology, OntologySubgraph


class SubgraphRetriever(Protocol):
    def retrieve(self, chunk: Chunk, ontology: Ontology) -> OntologySubgraph:
        """Select a subgraph; include endpoints for all selected relations."""
        ...


class ContextBuilder(Protocol):
    def build(
        self,
        chunk: Chunk,
        ontology: Ontology,
        subgraph: OntologySubgraph,
        prompt_template: str,
    ) -> LLMRequest:
        """Construct messages, including document offsets and output requirements."""
        ...


class LanguageModel(Protocol):
    def generate(self, request: LLMRequest) -> LLMResponse:
        """Call a provider and return its raw text, model name, and usage."""
        ...
