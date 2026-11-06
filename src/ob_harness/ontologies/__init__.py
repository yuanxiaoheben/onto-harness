"""JSON ontology schemas, immutable versions and evidence-backed changes."""

from .presets import schema_preset
from .repository import OntologyRepository
from .validation import apply_operations, validate_ontology, validate_schema

__all__ = [
    "OntologyRepository",
    "apply_operations",
    "validate_ontology",
    "validate_schema",
    "schema_preset",
]
