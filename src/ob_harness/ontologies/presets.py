"""Reusable concept-level schema templates; callers always receive a fresh copy."""

from copy import deepcopy


def _relation(description, *, hierarchical=False, acyclic=False):
    return {
        "direction": "child_to_parent" if hierarchical else "directed",
        "hierarchical": hierarchical,
        "acyclic": acyclic,
        "source_types": ["concept"],
        "target_types": ["concept"],
        "description": description,
    }


_TAXONOMY = {
    "schema_version": 1,
    "concept_types": ["concept"],
    "relation_types": {
        "is_a": _relation(
            "Subtype -> supertype. Every member of the source class is a member of the "
            "target class. Not instance membership or part-whole composition.",
            hierarchical=True,
        ),
    },
}
_GENERAL = deepcopy(_TAXONOMY)
_GENERAL["relation_types"].update(
    {
        "part_of": _relation(
            "Part concept -> whole concept; a domain-level structural composition "
            "restriction, not subclassing or an assertion about named individuals.",
            acyclic=True,
        ),
        "has_property": _relation(
            "Bearer concept -> quality/property concept; target is a concept, not a "
            "literal value or an observed measurement."
        ),
        "has_function": _relation(
            "Bearer concept -> function concept; describes intended function, not "
            "a guarantee that it is always realized."
        ),
        "participates_in": _relation(
            "Participant concept -> process concept; describe the applicable class-level "
            "participation restriction, not a concrete event."
        ),
        "depends_on": _relation(
            "Dependent concept -> prerequisite concept; requires explicit source support, "
            "does not imply causation."
        ),
        "precedes": _relation(
            "Earlier process/stage concept -> later process/stage concept; strict order "
            "within the stated scope, not a timestamp or a causal assertion.",
            acyclic=True,
        ),
        "related_to": _relation(
            "Source concept -> associated concept; weak explicitly supported association. "
            "No automatic reverse edge, equivalence, inheritance or transitive inference."
        ),
    }
)


def schema_preset(name: str = "general") -> dict:
    """Return general or taxonomy schema; never mutate an existing ontology schema."""
    presets = {"general": _GENERAL, "taxonomy": _TAXONOMY}
    if name not in presets:
        raise ValueError(f"Unknown schema preset: {name}")
    return deepcopy(presets[name])
