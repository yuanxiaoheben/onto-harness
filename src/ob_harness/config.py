"""JSON configuration; file paths are relative to the configuration file."""

import json
from dataclasses import asdict, dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from .models.inference import validate_settings


@dataclass
class RunConfig:
    experiment_name: str
    ontology_path: Path
    output_dir: Path
    prompt_path: Path
    max_chars: int
    overlap_chars: int
    retrieval: dict[str, Any]
    context: dict[str, Any]
    llm: dict[str, Any]

    def snapshot(self) -> dict[str, Any]:
        result = asdict(self)
        for key in ("ontology_path", "output_dir", "prompt_path"):
            result[key] = str(result[key])
        return result


def load_config(path: Path | None = None) -> RunConfig:
    bundled = path is None
    path = Path(str(files("ob_harness").joinpath("resources/legacy.json"))) if bundled else path
    assert path is not None
    path = path.resolve()
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    chunking = data.get("chunking", {})
    max_chars = chunking.get("max_chars", 2400)
    overlap_chars = chunking.get("overlap_chars", 200)
    if type(max_chars) is not int or type(overlap_chars) is not int:
        raise ValueError("chunking parameters must be integers")
    if not 0 <= overlap_chars < max_chars:
        raise ValueError("Require 0 <= overlap_chars < max_chars")
    components = {key: data.get(key, {}) for key in ("retrieval", "context", "llm")}
    if any(not isinstance(value, dict) for value in components.values()):
        raise ValueError("retrieval, context, and llm must be JSON objects")
    if components["llm"].get("provider") != "todo":
        validate_settings(components["llm"])
    if components["llm"].get("responses_path"):
        components["llm"]["responses_path"] = str(
            (path.parent / components["llm"]["responses_path"]).resolve()
        )
    return RunConfig(
        experiment_name=data.get("experiment_name", "ontology_extraction"),
        ontology_path=(path.parent / data["ontology_path"]).resolve(),
        output_dir=(Path.cwd() / "outputs").resolve()
        if bundled
        else (path.parent / data["output_dir"]).resolve(),
        prompt_path=(path.parent / data["prompt_path"]).resolve(),
        max_chars=max_chars,
        overlap_chars=overlap_chars,
        **components,
    )
