"""The ontology the knowledge graph is built against, read from ontology/kbase-ontology.yaml.

Node types are the classes annotated `extraction: true`; link types are the Predicate enum. Both are
generic for now (no business-domain rules). The builder constrains the model to these lists, so an
answer outside them can never be stored.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

ONTOLOGY_FILE = Path(__file__).resolve().parent.parent / "ontology" / "kbase-ontology.yaml"
DEFAULT_TYPE = "Term"
DEFAULT_PREDICATE = "related_to"
DOCUMENT_TYPE = "Document"  # a source file itself; set by the builder, never by the model


@lru_cache(maxsize=1)
def ontology() -> dict:
    data = yaml.safe_load(ONTOLOGY_FILE.read_text(encoding="utf-8"))
    types = {name: " ".join(str(spec.get("description", "")).split())
             for name, spec in (data.get("classes") or {}).items()
             if isinstance(spec, dict) and (spec.get("annotations") or {}).get("extraction")}
    predicates = {name: " ".join(str((spec or {}).get("description", "")).split())
                  for name, spec in data["enums"]["Predicate"]["permissible_values"].items()}
    if DEFAULT_TYPE not in types or DEFAULT_PREDICATE not in predicates:
        raise ValueError("The ontology must keep the Term type and the related_to predicate as fallbacks")
    kinds = {}
    for name, spec in ((data["enums"].get("DocumentKind") or {}).get("permissible_values") or {}).items():
        spec = spec or {}
        expected = list((spec.get("annotations") or {}).get("expected_types") or [])
        unknown = [t for t in expected if t not in types]
        if unknown:
            raise ValueError(f"DocumentKind {name} expects types the ontology does not extract: {', '.join(unknown)}")
        kinds[name] = {"label": str(spec.get("title") or name.replace("_", " ")),
                       "description": " ".join(str(spec.get("description", "")).split()), "expected_types": expected}
    return {"version": str(data.get("version", "")), "types": types, "predicates": predicates, "kinds": kinds}


def node_types() -> dict[str, str]:
    return ontology()["types"]


def predicates() -> dict[str, str]:
    return ontology()["predicates"]


def version() -> str:
    return ontology()["version"]


def document_kinds() -> dict[str, dict]:
    """Kinds of document for the entity scan: {name: {label, description, expected_types}}."""
    return ontology()["kinds"]
