"""Composition root: register or instantiate research implementations here."""

from .config import RunConfig
from .context import TemplateContextBuilder
from .harness.placeholders import TodoContextBuilder, TodoLanguageModel, TodoRetriever
from .harness.runner import ExtractionHarness
from .models import build_model
from .retrieval import SimpleRetriever


def build_harness(config: RunConfig) -> ExtractionHarness:
    # Replace individual mappings to add independent experiment implementations.
    retriever = (
        TodoRetriever(config.retrieval)
        if config.retrieval.get("strategy") == "todo"
        else SimpleRetriever(config.retrieval)
    )
    strategy = config.context.get("strategy", "template")
    if strategy not in {"todo", "template"}:
        raise ValueError("Unregistered context builder")
    builder = (
        TodoContextBuilder(config.context)
        if strategy == "todo"
        else TemplateContextBuilder(config.context)
    )
    model = (
        TodoLanguageModel(config.llm)
        if config.llm.get("provider") == "todo"
        else build_model(config.llm)
    )
    return ExtractionHarness(
        retriever,
        builder,
        model,
    )
