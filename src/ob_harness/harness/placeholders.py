"""The three research components are deliberately unimplemented."""

from typing import Any

from ..domain import Chunk, LLMRequest, LLMResponse, Ontology, OntologySubgraph


class TodoRetriever:
    def __init__(self, settings: dict[str, Any]):
        self.settings = settings

    def retrieve(self, chunk: Chunk, ontology: Ontology) -> OntologySubgraph:
        # TODO: keyword / embedding / graph expansion / learned retrieval.
        raise NotImplementedError("Implement TodoRetriever.retrieve in harness/placeholders.py")


class TodoContextBuilder:
    def __init__(self, settings: dict[str, Any]):
        self.settings = settings

    def build(
        self,
        chunk: Chunk,
        ontology: Ontology,
        subgraph: OntologySubgraph,
        prompt_template: str,
    ) -> LLMRequest:
        # TODO: selection, serialization, token budget, instructions, and evidence offsets.
        raise NotImplementedError("Implement TodoContextBuilder.build in harness/placeholders.py")


class TodoLanguageModel:
    def __init__(self, settings: dict[str, Any]):
        self.settings = settings

    def generate(self, request: LLMRequest) -> LLMResponse:
        # TODO: provider SDK, structured output, timeout, retries, and usage accounting.
        # Read credentials from environment variables; never include them in settings.
        raise NotImplementedError("Implement TodoLanguageModel.generate in harness/placeholders.py")
