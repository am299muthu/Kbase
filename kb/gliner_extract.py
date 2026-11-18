"""Graph extraction with GLiNER2.5, a local model, instead of graphify and an LLM.

The graph build (kb/graph_build.py, KBASE_GRAPH_EXTRACTOR=gliner) hands this module the indexed chunks
of the chosen documents. GLiNER reads each chunk once and returns entity spans and typed links. Its
labels are the ontology's own types and predicates, so no alignment step is needed: an entity's type
and a link's predicate come straight from the model, and a link can only join the types the ontology
describes for it (for example `measures` only from a Metric).

Results are cached per chunk (keyed on model, schema and text), so a rebuild only reads new or changed
chunks. The model runs in the local Docling Python (LOCAL_GLINER_PYTHON, else DOCLING_PYTHON) through
scripts/gliner/worker.py; nothing leaves the machine.

Left out on purpose: Rule (a whole checkable sentence, not a name) and Term (the catch-all type), and
the structural or vague predicates (contains, has_section, evidenced_by, defines, states, broader,
in_domain, mentions, related_to). Those come from document structure, rules or reviewers.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import threading
from collections import defaultdict
from pathlib import Path
from typing import Callable

from kb.config import ROOT
from kb.ontology import node_types, predicates

log = logging.getLogger(__name__)
WORKER = ROOT / "scripts" / "gliner" / "worker.py"
MODEL = os.environ.get("KBASE_GLINER_MODEL", "fastino/gliner2.5-base-v1")


def _float_env(name: str, default: float) -> float:
    try:
        return min(1.0, max(0.0, float(os.environ.get(name, default))))
    except ValueError:
        return default


ENTITY_MIN = _float_env("KBASE_GLINER_ENTITY_MIN", 0.5)  # weaker entity spans are dropped
# Weaker links are dropped; the rest go to review. On the Finance folder (14,070 chunks) a random sample
# at 0.3 was mostly wrong, at 0.7 about 60% right and at 0.9 about 75% right; 0.85 keeps ~600 links.
LINK_MIN = _float_env("KBASE_GLINER_LINK_MIN", 0.85)
MAX_EVIDENCE = 5                                          # chunks kept as evidence for one link

# Ontology type -> the label GLiNER is given. Descriptions come from the ontology file.
TYPES = {"Organisation": "organisation", "Person": "person", "Product": "product", "Policy": "policy",
         "Process": "process", "Metric": "metric", "Event": "event", "Location": "location"}
# Each row restates the predicate's own description in kbase-ontology.yaml as allowed head and tail types.
RELATIONS = {
    "owned_by": (["Product", "Organisation", "Process", "Policy"], ["Organisation", "Person"]),
    "part_of": (["Organisation", "Person", "Location", "Product", "Process"], ["Organisation", "Location", "Product", "Process"]),
    "located_in": (["Organisation", "Person", "Event", "Product"], ["Location"]),
    "measures": (["Metric"], ["Organisation", "Product", "Process", "Location"]),
    "occurs_in": (["Event", "Metric"], ["Event", "Location"]),
    "governed_by": (["Product", "Process", "Organisation"], ["Policy"]),
    "applies_to": (["Policy"], ["Product", "Organisation", "Person", "Process"]),
    "supersedes": (["Policy"], ["Policy"]),
}
_LETTER = re.compile(r"[^\W\d_]")


class GlinerError(RuntimeError):
    pass


def schema() -> dict:
    """The GLiNER joint schema, built from the ontology. Fails loudly if the ontology loses a type or
    predicate this module relies on, rather than silently extracting something else."""
    types, links = node_types(), predicates()
    missing = [t for t in TYPES if t not in types] + [p for p in RELATIONS if p not in links]
    if missing:
        raise GlinerError(f"the ontology no longer has {', '.join(missing)}; update kb/gliner_extract.py")
    return {
        "entities": {label: {"description": types[name]} for name, label in TYPES.items()},
        "relations": {name: {"head": [TYPES[t] for t in heads], "tail": [TYPES[t] for t in tails],
                             "description": links[name]}
                      for name, (heads, tails) in RELATIONS.items()},
        "constraints": [{"type": "NoSelfLoops", "relation": None}],
    }


def _python() -> str:
    path = os.environ.get("LOCAL_GLINER_PYTHON") or os.environ.get("DOCLING_PYTHON") or ""
    if not path or not Path(path).is_file():
        raise GlinerError("GLiNER needs LOCAL_GLINER_PYTHON or DOCLING_PYTHON: a Python with torch and gliner2")
    return path


def _key(schema_text: str, text: str) -> str:
    return hashlib.sha1("\x1f".join((MODEL, schema_text, text)).encode("utf-8")).hexdigest()


def extract(chunks: list[dict], work: Path, timeout: int,
            progress: Callable[[int, int], None] | None = None) -> dict[str, dict]:
    """GLiNER results by chunk id for `chunks` ({id, text}). Only chunks not in the cache are read."""
    work.mkdir(parents=True, exist_ok=True)
    schema_text = json.dumps(schema(), sort_keys=True)
    cache_file = work / "gliner-cache.jsonl"
    cache: dict[str, dict] = {}
    if cache_file.exists():
        for line in cache_file.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                cache[row["key"]] = row
            except (ValueError, KeyError):
                continue
    keys = {chunk["id"]: _key(schema_text, chunk["text"]) for chunk in chunks}
    pending = [chunk for chunk in chunks if keys[chunk["id"]] not in cache]
    if pending:
        results = work / "gliner-results.jsonl"
        results.unlink(missing_ok=True)
        try:
            _run_worker(pending, work, schema_text, timeout, progress)
        finally:
            # The worker writes results as it goes; keep what it finished even if the run failed, so a
            # retry only reads the rest.
            texts = {chunk["id"]: chunk["text"] for chunk in pending}
            lines = results.read_text(encoding="utf-8").splitlines() if results.exists() else []
            with cache_file.open("a", encoding="utf-8") as out:
                for line in lines:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue  # a line cut off when the worker stopped
                    if row.get("id") not in texts:
                        continue
                    entry = {"key": _key(schema_text, texts[row["id"]]), "entities": row["entities"], "relations": row["relations"]}
                    cache[entry["key"]] = entry
                    out.write(json.dumps(entry) + "\n")
    missing = [cid for cid, key in keys.items() if key not in cache]
    if missing:
        raise GlinerError(f"GLiNER returned no result for {len(missing)} chunk(s)")
    return {cid: cache[key] for cid, key in keys.items()}


def _run_worker(chunks: list[dict], work: Path, schema_text: str, timeout: int,
                progress: Callable[[int, int], None] | None) -> None:
    (work / "gliner-input.jsonl").write_text("".join(json.dumps({"id": c["id"], "text": c["text"]}) + "\n" for c in chunks),
                                             encoding="utf-8")
    (work / "gliner-schema.json").write_text(schema_text, encoding="utf-8")
    run_worker_process(["--input", str(work / "gliner-input.jsonl"), "--schema", str(work / "gliner-schema.json"),
                        "--out", str(work / "gliner-results.jsonl"),
                        "--optimizer", os.environ.get("KBASE_GLINER_OPTIMIZER", "beam")],
                       work, timeout, progress, "gliner-worker.log")


def run_worker_process(arguments: list[str], work: Path, timeout: int,
                       progress: Callable[[int, int], None] | None, log_name: str) -> None:
    """Run scripts/gliner/worker.py with `arguments` (plus model, batch and dtype) in the local Docling
    Python, offline, under a time limit. Its standard error goes to `log_name` in `work`."""
    from kb.config import worker_python_flags
    command = [_python(), *worker_python_flags(), str(WORKER), *arguments, "--model", MODEL,
               "--batch", os.environ.get("KBASE_GLINER_BATCH", "8"),
               "--dtype", os.environ.get("KBASE_GLINER_DTYPE", "fp16")]
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "PYTHONIOENCODING": "utf-8",
           "TOKENIZERS_PARALLELISM": "false"}
    timed_out = threading.Event()
    with (work / log_name).open("wb") as log_file:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=log_file, env=env, text=True,
                                   encoding="utf-8", errors="replace")

        def kill() -> None:
            timed_out.set()
            process.kill()

        timer = threading.Timer(timeout, kill)
        timer.start()
        try:
            for line in process.stdout:
                try:
                    step = json.loads(line)
                except ValueError:
                    continue
                if progress and isinstance(step, dict) and "completed" in step:
                    progress(int(step["completed"]), int(step["total"]))
            code = process.wait()
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
    if timed_out.is_set():
        raise GlinerError(f"GLiNER did not finish within its time limit ({timeout} s); finished chunks are kept for the next build")
    if code != 0:
        tail = (work / log_name).read_bytes()[-3000:].decode("utf-8", "replace")
        log.error("GLiNER worker failed (exit %s):\n%s", code, tail)
        raise GlinerError(f"the GLiNER worker exited with code {code}; {log_name} in the graph work folder has the details")


def to_graph(files: dict[str, dict], results: dict[str, dict]) -> tuple[dict, dict, list[dict], dict[int, str]]:
    """GLiNER results -> (entities, types, links, predicate by link index), in the shapes save_graph takes.
    An entity's type is the one with the highest total confidence over all its mentions. A link keeps up
    to MAX_EVIDENCE chunks, each becoming one piece of evidence."""
    from kb.graph_build import normalise  # here, not at import time: graph_build imports this module

    type_of_label = {label: name for name, label in TYPES.items()}
    labels: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    type_votes: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    entity_files: dict[str, set] = defaultdict(set)
    occurrences: dict[tuple, list[dict]] = defaultdict(list)

    def keep(text: str) -> str | None:
        key = normalise(text)
        return key if 2 <= len(key) <= 190 and _LETTER.search(text) else None

    for file_name, source in files.items():
        for chunk in source["chunks"]:
            result = results.get(chunk["id"])
            if not result:
                continue
            for entity in result["entities"]:
                key = keep(entity["text"])
                kind = type_of_label.get(entity["type"])
                if key and kind and entity["confidence"] >= ENTITY_MIN:
                    labels[key][entity["text"].strip()] += entity["confidence"]
                    type_votes[key][kind] += entity["confidence"]
                    entity_files[key].add(file_name)
            for relation in result["relations"]:
                head, tail = keep(relation["head"]), keep(relation["tail"])
                if (not head or not tail or head == tail or relation["type"] not in RELATIONS
                        or relation["confidence"] < LINK_MIN):
                    continue
                for key, text, kind in ((head, relation["head"], relation["head_type"]), (tail, relation["tail"], relation["tail_type"])):
                    labels[key][text.strip()] += relation["confidence"]
                    if type_of_label.get(kind):
                        type_votes[key][type_of_label[kind]] += relation["confidence"]
                    entity_files[key].add(file_name)
                occurrences[(head, relation["type"], tail)].append(
                    {"file": file_name, "chunk_id": chunk["id"], "score": relation["confidence"]})

    entities = {key: {"label": max(variants, key=variants.get), "files": entity_files[key], "document": None}
                for key, variants in labels.items()}
    types = {key: max(votes, key=votes.get) for key, votes in type_votes.items()}
    links: list[dict] = []
    chosen: dict[int, str] = {}
    for (head, predicate, tail), found in occurrences.items():
        # An entity's final type is voted over all its mentions, so it can differ from the type GLiNER gave
        # it in this one link. Keep the link only if the final types are ones the predicate allows.
        heads, tails = RELATIONS[predicate]
        if types.get(head) not in heads or types.get(tail) not in tails:
            continue
        best = max(found, key=lambda o: o["score"])["score"]
        for occurrence in sorted(found, key=lambda o: -o["score"])[:MAX_EVIDENCE]:
            chosen[len(links)] = predicate
            links.append({"source": head, "target": tail, "relation": predicate, "confidence": "gliner",
                          "score": round(best, 3), "file": occurrence["file"], "chunk_id": occurrence["chunk_id"]})
    return entities, types, links, chosen
