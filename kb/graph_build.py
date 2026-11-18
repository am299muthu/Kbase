"""Build a folder's knowledge graph with graphify, align it to the ontology, and store it for review.

Steps, each shown as the build's stage:
  1. Stage the chosen indexed documents of one folder as text files (from their stored chunks, so the
     graph sees exactly what search sees). One folder per build: a graph never mixes folders, so
     every node and link inherits that folder's access.
  2. graphify extracts entities and links (demo backend: the local `claude` CLI; production points
     the same step at the internal model with KBASE_GRAPH_BACKEND=openai).
  3. Alignment: one constrained Claude call maps each entity to an ontology type and each link to an
     ontology predicate. It sees the labels, graphify's relation name and each link's evidence
     sentence (text graphify has already read). Anything outside the ontology falls back to
     Term / related_to.
  4. Evidence: each link is tied to the indexed chunk (and page) where both entities appear, with
     the supporting sentence.
  With KBASE_GRAPH_EXTRACTOR=gliner, steps 2 and 3 are one local GLiNER2.5 pass instead (no LLM).
  5. Save. Links start as proposed; a review decision on the same (source, predicate, target) is
     carried over from earlier builds. Links found only in documents outside this build are kept.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import traceback
from collections import defaultdict
from pathlib import Path

from sqlalchemy import select

from kb.config import ROOT, settings
from kb.db import (Chunk, Document, GraphBuild, GraphEvidence, GraphLink, GraphNode, audit, db_session, now)
from kb.ontology import DEFAULT_PREDICATE, DEFAULT_TYPE, DOCUMENT_TYPE, node_types, predicates, version

log = logging.getLogger(__name__)
GRAPHIFY = ROOT / ".venv-graphify" / "Scripts" / "graphify.exe"
if not GRAPHIFY.exists():
    GRAPHIFY = ROOT / ".venv-graphify" / "bin" / "graphify"
BACKEND = os.environ.get("KBASE_GRAPH_BACKEND", "claude-cli")
MODEL = os.environ.get("KBASE_GRAPH_MODEL", "sonnet")
# graphify: graphify + LLM extraction, then LLM alignment (steps 2 and 3 above).
# gliner: GLiNER2.5 on this machine reads each chunk with the ontology's own types and predicates, so
# steps 2 and 3 become one local step with no LLM (kb/gliner_extract.py).
EXTRACTOR = os.environ.get("KBASE_GRAPH_EXTRACTOR", "graphify").lower()
try:  # files graphify sends to the model at the same time (claude-cli backend)
    CONCURRENCY = max(1, min(16, int(os.environ.get("KBASE_GRAPH_CONCURRENCY", "1"))))
except ValueError:
    CONCURRENCY = 1
LARGE_DOCUMENT_CHUNKS = 200  # the build page leaves larger documents unticked by default
_running = threading.Lock()


class BuildError(RuntimeError):
    pass


# ----------------------------------------------------------------------------------- helpers

def normalise(label: str) -> str:
    text = re.sub(r"[^\w%$]+", " ", (label or "").casefold())
    return re.sub(r"\s+", " ", text).strip()


def _hash(*parts: str, size: int = 16) -> str:
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:size]


def node_id(folder_id: str, key: str) -> str:
    return f"{folder_id}:{_hash(key)}"


def link_id(folder_id: str, source_key: str, predicate: str, target_key: str) -> str:
    return f"{folder_id}:{_hash(source_key, predicate, target_key, size=20)}"


def _set_stage(build_id: str, stage: str, **fields) -> None:
    with db_session() as db:
        build = db.get(GraphBuild, build_id)
        build.stage = stage
        for key, value in fields.items():
            setattr(build, key, value)


# ----------------------------------------------------------------------------------- 1. staging

def stage_documents(db, folder_id: str, document_ids: list[str], target: Path) -> dict[str, dict]:
    """Write each document as <title>-<id8>.md from its indexed chunks. Returns file name -> source."""
    files = {}
    for document in db.scalars(select(Document).where(Document.folder_id == folder_id, Document.id.in_(document_ids),
                                                       Document.status == "indexed")):
        chunks = list(db.scalars(select(Chunk).where(Chunk.document_id == document.id).order_by(Chunk.id)))
        if not chunks:
            continue
        stem = re.sub(r"[^A-Za-z0-9]+", "-", Path(document.title).stem).strip("-")[:60] or "document"
        name = f"{stem}-{document.id[:8]}.md"
        lines, section = [f"# {Path(document.title).stem}", ""], None
        for chunk in chunks:
            if chunk.section and chunk.section != section:
                section = chunk.section
                lines += [f"## {section}", ""]
            lines += [chunk.text.strip(), ""]
        (target / name).write_text("\n".join(lines), encoding="utf-8")
        files[name] = {"document_id": document.id, "title": document.title,
                       "chunks": [{"id": c.id, "page": c.page, "text": c.text} for c in chunks]}
    return files


# ----------------------------------------------------------------------------------- 2. graphify

def run_graphify(staged: Path, out: Path, timeout: int) -> dict:
    if not GRAPHIFY.exists():
        raise BuildError("graphify is not installed (.venv-graphify)")
    env = {**os.environ, "GRAPHIFY_QUERY_LOG_DISABLE": "1", "PYTHONIOENCODING": "utf-8"}
    # No --force: graphify's per-file semantic cache (in `out`, keyed on file content) is reused, so an
    # unchanged document gives the same extraction, keeps its reviews, and costs no new model calls.
    cmd = [str(GRAPHIFY), "extract", str(staged), "--out", str(out), "--no-gitignore"]
    if BACKEND == "claude-cli":
        env["GRAPHIFY_CLAUDE_CLI_MODEL"] = MODEL
        cmd += ["--backend", "claude-cli", "--max-concurrency", str(CONCURRENCY)]
    else:
        cmd += ["--backend", BACKEND]
    try:
        proc = subprocess.run(cmd, cwd=str(staged), env=env, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise BuildError(f"graphify did not finish within its time limit ({timeout} s)") from exc
    if proc.returncode != 0:
        log.error("graphify failed: %s", (proc.stderr or proc.stdout)[-3000:])
        raise BuildError(f"graphify exited with code {proc.returncode}; the server log has the details")
    graph_file = out / "graphify-out" / "graph.json"
    if not graph_file.exists():
        raise BuildError("graphify finished without writing graph.json")
    return json.loads(graph_file.read_text(encoding="utf-8"))


def parse_graph(graph: dict, files: dict[str, dict]) -> tuple[dict, list[dict]]:
    """graphify's graph.json -> entities {key: {label, files, document}} and links [{source, target, ...}].
    A node that is a staged file itself becomes a Document entity named after the document title."""
    # graphify may name a file's own node after the file name or after its H1 (the document title).
    by_stem = {Path(name).stem.casefold(): name for name in files}
    by_stem.update({Path(f["title"]).stem.casefold(): name for name, f in files.items()})
    by_stem.update({f["title"].casefold(): name for name, f in files.items()})
    entities: dict[str, dict] = {}
    id_to_key: dict[str, str] = {}
    for node in graph.get("nodes", []):
        label = str(node.get("label") or node.get("id") or "").strip()
        source = Path(str(node.get("source_file") or "")).name
        file_name = by_stem.get(label.casefold()) or by_stem.get(Path(label).stem.casefold())
        if file_name is None and node.get("file_type") == "document" and normalise(node.get("id", "")) == normalise(Path(source).stem):
            file_name = source if source in files else None
        if file_name:  # the document node itself
            label = files[file_name]["title"]
        key = normalise(label)
        if not key or len(key) > 190:
            continue
        entity = entities.setdefault(key, {"label": label, "files": set(), "document": None})
        if file_name:
            entity["document"] = files[file_name]["document_id"]
        if source in files:
            entity["files"].add(source)
        id_to_key[str(node.get("id"))] = key
    links: dict[tuple, dict] = {}
    for edge in graph.get("links", graph.get("edges", [])):
        source_key, target_key = id_to_key.get(str(edge.get("source"))), id_to_key.get(str(edge.get("target")))
        if not source_key or not target_key or source_key == target_key:
            continue
        relation = str(edge.get("relation") or "related_to")
        file_name = Path(str(edge.get("source_file") or "")).name
        key = (source_key, relation, target_key)
        score = float(edge.get("confidence_score") or 0)
        current = links.get(key)
        if current is None or score > current["score"]:
            links[key] = {"source": source_key, "target": target_key, "relation": relation,
                          "confidence": str(edge.get("confidence") or ""), "score": round(score, 3),
                          "file": file_name if file_name in files else None}
    return entities, list(links.values())


# ----------------------------------------------------------------------------------- 3. alignment

def _claude_json(prompt: str, schema: dict, timeout: int) -> dict:
    claude = shutil.which("claude")
    if not claude:
        raise BuildError("the claude CLI is not on PATH")
    cmd = [claude, "-p", "--output-format", "json", "--no-session-persistence", "--model", MODEL,
           "--json-schema", json.dumps(schema)]
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise BuildError(f"ontology alignment did not finish within its time limit ({timeout} s)") from exc
    if proc.returncode != 0:
        raise BuildError(f"claude exited with code {proc.returncode}: {(proc.stderr or '')[:300]}")
    envelope = json.loads(proc.stdout)
    if isinstance(envelope, list):
        results = [e for e in envelope if isinstance(e, dict) and e.get("type") == "result"]
        envelope = results[-1] if results else envelope[-1]
    if envelope.get("is_error"):
        raise BuildError(f"claude reported an error: {str(envelope.get('result'))[:300]}")
    structured = envelope.get("structured_output")
    if isinstance(structured, dict):
        return structured
    text = str(envelope.get("result") or "").strip().strip("`")
    text = text[4:] if text.lower().startswith("json") else text
    return json.loads(text)


def align(entities: dict, links: list[dict], timeout: int, call=_claude_json, batch: int = 200,
          quotes: dict[int, str] | None = None, known_types: dict[str, str] | None = None,
          known_links: dict[tuple, str] | None = None) -> tuple[dict, dict, list[str]]:
    """Ontology types for entities and predicates for links. Returns (types by key, predicate by
    link index, notes). Each link is shown with its evidence sentence when there is one, so a specific
    predicate can be chosen. Entities and links that existed before keep their earlier type and
    predicate (known_types by key, known_links by (source, relation, target)), so a rebuild of
    unchanged documents is stable and only new items go to the model. Values outside the ontology,
    or a failed call, fall back to the defaults."""
    quotes, known_types, known_links = quotes or {}, known_types or {}, known_links or {}
    types_allowed, predicates_allowed = node_types(), predicates()
    types = {k: DOCUMENT_TYPE for k, e in entities.items() if e["document"]}
    types.update({k: t for k, t in known_types.items() if k in entities and k not in types and t in types_allowed})
    keys = [k for k in entities if k not in types]
    chosen_predicates: dict[int, str] = {}
    for index, link in enumerate(links):
        earlier = known_links.get((link["source"], link["relation"], link["target"]))
        if earlier in predicates_allowed:
            chosen_predicates[index] = earlier
    pending_links = [i for i in range(len(links)) if i not in chosen_predicates]
    notes: list[str] = []
    type_lines = "\n".join(f"- {name}: {text}" for name, text in types_allowed.items())
    predicate_lines = "\n".join(f"- {name}: {text}" for name, text in predicates_allowed.items())
    schema = {
        "type": "object", "additionalProperties": False, "required": ["types", "links"],
        "properties": {
            "types": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["id", "type"],
                                                 "properties": {"id": {"type": "integer"}, "type": {"enum": list(types_allowed)}}}},
            "links": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["id", "predicate"],
                                                 "properties": {"id": {"type": "integer"}, "predicate": {"enum": list(predicates_allowed)}}}},
        },
    }
    entity_batches = [keys[i:i + batch] for i in range(0, len(keys), batch)] or [[]]
    link_batches = [pending_links[i:i + batch] for i in range(0, len(pending_links), batch)] or [[]]
    for b in range(max(len(entity_batches), len(link_batches))):
        batch_keys = entity_batches[b] if b < len(entity_batches) else []
        batch_links = link_batches[b] if b < len(link_batches) else []
        if not batch_keys and not batch_links:
            continue
        entity_list = "\n".join(f"{i}: {entities[k]['label']}" for i, k in enumerate(batch_keys))
        link_list = "\n".join(
            f"{i}: {entities[links[j]['source']]['label']} --{links[j]['relation']}--> {entities[links[j]['target']]['label']}"
            + (f"\n   evidence: \"{quotes[j][:240]}\"" if quotes.get(j) else "")
            for i, j in enumerate(batch_links))
        prompt = (
            "You align a knowledge graph extracted from documents to a fixed ontology. Do not add entities or links.\n\n"
            f"Entity types (choose exactly one per entity):\n{type_lines}\n\n"
            f"Link predicates (choose exactly one per link; the link goes from the left entity to the right one):\n{predicate_lines}\n\n"
            "Read each link's evidence sentence and choose the most specific predicate it supports, "
            "turning the link around in your head if needed (only the predicate is returned). "
            "Use Term when no other type fits, and related_to only when no other predicate fits.\n\n"
            f"Entities (id: label):\n{entity_list or '(none)'}\n\n"
            f"Links (id: source --extracted relation--> target):\n{link_list or '(none)'}\n\n"
            "Return JSON with `types` (one per entity id) and `links` (one per link id)."
        )
        try:
            answer = call(prompt, schema, timeout)
        except Exception as exc:  # noqa: BLE001  alignment is an improvement, not a requirement
            notes.append(f"Ontology alignment batch {b + 1} failed ({type(exc).__name__}); its items use Term / related_to")
            log.warning("graph alignment failed: %s", exc)
            continue
        for row in answer.get("types", []):
            index, value = row.get("id"), row.get("type")
            if isinstance(index, int) and 0 <= index < len(batch_keys) and value in types_allowed:
                types[batch_keys[index]] = value
        for row in answer.get("links", []):
            index, value = row.get("id"), row.get("predicate")
            if isinstance(index, int) and 0 <= index < len(batch_links) and value in predicates_allowed:
                chosen_predicates[batch_links[index]] = value
    for key in keys:
        types.setdefault(key, DEFAULT_TYPE)
    return types, chosen_predicates, notes


# ----------------------------------------------------------------------------------- 4. evidence

_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")


def find_evidence(source_label: str, target_label: str, chunks: list[dict]) -> dict | None:
    """The chunk where both labels appear (else the source label), and the best sentence in it."""
    s, t = source_label.casefold(), target_label.casefold()
    best, best_rank = None, 0
    for chunk in chunks:
        text = chunk["text"].casefold()
        rank = 2 if s in text and t in text else 1 if (s in text or t in text) else 0
        if rank > best_rank:
            best, best_rank = chunk, rank
            if rank == 2:
                break
    if best is None:
        return None
    sentences = [x.strip() for x in _SENTENCE.split(best["text"]) if x.strip()]
    quote = next((x for x in sentences if s in x.casefold() and t in x.casefold()), None) \
        or next((x for x in sentences if s in x.casefold() or t in x.casefold()), sentences[0] if sentences else "")
    return {"chunk_id": best["id"], "page": best.get("page"), "quote": quote[:600]}


# ----------------------------------------------------------------------------------- 5. save

def save_graph(db, build: GraphBuild, entities: dict, types: dict, links: list[dict], chosen_predicates: dict,
               files: dict) -> dict:
    folder_id = build.folder_id
    built_documents = {f["document_id"] for f in files.values()}
    chunks_by_file = {name: f["chunks"] for name, f in files.items()}
    all_chunks = [c for f in files.values() for c in f["chunks"]]
    document_of_file = {name: f["document_id"] for name, f in files.items()}

    old_nodes = {n.id: n for n in db.scalars(select(GraphNode).where(GraphNode.folder_id == folder_id))}
    old_links = list(db.scalars(select(GraphLink).where(GraphLink.folder_id == folder_id)))
    old_evidence = defaultdict(list)
    for row in db.scalars(select(GraphEvidence).where(GraphEvidence.link_id.in_([l.id for l in old_links]))):
        old_evidence[row.link_id].append(row)
    reviews = {l.id: l for l in old_links if l.status != "proposed"}
    # A reviewed link whose predicate changed in this build: the same two entities, a different claim.
    key_of = {nid: n.key for nid, n in old_nodes.items()}
    reviews_by_pair = {(key_of.get(l.source_id), key_of.get(l.target_id)): l for l in old_links if l.status != "proposed"}
    human_types = {n.key: n.node_type for n in old_nodes.values() if n.type_source == "human"}

    # Links whose evidence comes only from documents in this build are replaced; the rest stay.
    replaced = [l for l in old_links if old_evidence[l.id] and {e.document_id for e in old_evidence[l.id]} <= built_documents]
    for link in replaced:
        for row in old_evidence[link.id]:
            db.delete(row)
        db.delete(link)
    db.flush()
    kept_links = [l for l in old_links if l not in replaced]

    new_links: dict[str, dict] = {}
    for index, link in enumerate(links):
        predicate = chosen_predicates.get(index, DEFAULT_PREDICATE)
        lid = link_id(folder_id, link["source"], predicate, link["target"])
        chunks = chunks_by_file.get(link["file"]) if link["file"] else None
        if link.get("chunk_id") and chunks:  # the extractor read this exact chunk (GLiNER)
            chunks = [c for c in chunks if c["id"] == link["chunk_id"]] or chunks
        evidence = find_evidence(entities[link["source"]]["label"], entities[link["target"]]["label"], chunks or all_chunks)
        if evidence is None:
            continue
        evidence["document_id"] = document_of_file.get(link["file"]) or next(
            f["document_id"] for f in files.values() if any(c["id"] == evidence["chunk_id"] for c in f["chunks"]))
        entry = new_links.setdefault(lid, {**link, "predicate": predicate, "evidence": []})
        if evidence["chunk_id"] not in {e["chunk_id"] for e in entry["evidence"]}:
            entry["evidence"].append(evidence)

    used_keys = {k for l in new_links.values() for k in (l["source"], l["target"])}
    kept_node_ids = {nid for l in kept_links for nid in (l.source_id, l.target_id)}
    for nid, node in old_nodes.items():
        if nid not in kept_node_ids and node.key not in used_keys:
            db.delete(node)
    db.flush()
    nodes_now = {n.id: n for n in db.scalars(select(GraphNode).where(GraphNode.folder_id == folder_id))}
    for key in used_keys:
        entity = entities[key]
        nid = node_id(folder_id, key)
        documents = sorted({document_of_file[f] for f in entity["files"] if f in document_of_file}
                           | ({entity["document"]} if entity["document"] else set()))
        node_type = human_types.get(key) or types.get(key, DEFAULT_TYPE)
        source = "human" if key in human_types else ("rule" if entity["document"] else "model")
        node = nodes_now.get(nid)
        if node is None:
            db.add(GraphNode(id=nid, folder_id=folder_id, key=key, label=entity["label"], node_type=node_type,
                             type_source=source, document_ids=documents, build_id=build.id))
        else:
            node.label, node.node_type, node.type_source, node.build_id = entity["label"], node_type, source, build.id
            node.document_ids = sorted(set(node.document_ids or []) | set(documents))
    db.flush()

    carried = changed = 0
    for lid, link in new_links.items():
        previous = reviews.get(lid)
        status, reviewer, reviewed_at, note = "proposed", "", "", ""
        if previous:  # same source, predicate and target: the decision still applies
            status, reviewer, reviewed_at, note = previous.status, previous.reviewed_by, previous.reviewed_at, previous.review_note
            carried += 1
        else:
            earlier = reviews_by_pair.get((link["source"], link["target"]))
            if earlier is not None and earlier.status == "rejected":  # a rejection of the pair still stands
                status, reviewer, reviewed_at = "rejected", earlier.reviewed_by, earlier.reviewed_at
                note = f"Rejected earlier as '{earlier.predicate}'"
                carried += 1
            elif earlier is not None:  # approved as a different claim: back to review, with the history
                note = f"Was approved as '{earlier.predicate}' by {earlier.reviewed_by}; the rebuild changed it to '{link['predicate']}'"
                changed += 1
        db.add(GraphLink(id=lid, folder_id=folder_id, source_id=node_id(folder_id, link["source"]),
                         target_id=node_id(folder_id, link["target"]), predicate=link["predicate"],
                         relation=link["relation"][:60], confidence=link["confidence"][:12], score=link["score"],
                         status=status, reviewed_by=reviewer, reviewed_at=reviewed_at, review_note=note,
                         build_id=build.id))
    db.flush()  # links must exist before their evidence rows (foreign key)
    for lid, link in new_links.items():
        for evidence in link["evidence"]:
            db.add(GraphEvidence(link_id=lid, document_id=evidence["document_id"], chunk_id=evidence["chunk_id"],
                                 page=evidence["page"], quote=evidence["quote"]))
    db.flush()
    by_type = defaultdict(int)
    for key in used_keys:
        by_type[human_types.get(key) or types.get(key, DEFAULT_TYPE)] += 1
    by_predicate = defaultdict(int)
    for link in new_links.values():
        by_predicate[link["predicate"]] += 1
    return {"documents": len(files), "nodes": len(used_keys), "links": len(new_links), "kept_links": len(kept_links),
            "reviews_carried": carried, "reviews_reopened": changed, "types": dict(by_type), "predicates": dict(by_predicate)}


# ----------------------------------------------------------------------------------- run

def _backend_label() -> str:
    if EXTRACTOR == "gliner":
        from kb.gliner_extract import MODEL as GLINER_MODEL
        return f"gliner:{GLINER_MODEL.rsplit('/', 1)[-1]}"
    return f"{BACKEND}:{MODEL}" if BACKEND == "claude-cli" else BACKEND


def start_build(db, folder_id: str, document_ids: list[str], employee_id: str) -> GraphBuild:
    if db.scalar(select(GraphBuild).where(GraphBuild.status.in_(["queued", "running"])).limit(1)):
        raise BuildError("A graph build is already running. Wait for it to finish.")
    build = GraphBuild(id=secrets.token_hex(16), folder_id=folder_id, status="queued", stage="queued",
                       started_by=employee_id, document_ids=list(document_ids),
                       backend=_backend_label(), ontology_version=version())
    db.add(build)
    db.flush()
    return build


def launch(build_id: str) -> None:
    threading.Thread(target=run_build, args=(build_id,), name=f"graph-build-{build_id[:8]}", daemon=True).start()


def run_build(build_id: str) -> None:
    from kb.processing_limits import load_limits

    if not _running.acquire(blocking=False):
        _set_stage(build_id, "failed", status="failed", error="Another graph build is running", finished_at=now())
        return
    try:
        limits = load_limits()
        _set_stage(build_id, "staging documents", status="running")
        with db_session() as db:
            build = db.get(GraphBuild, build_id)
            # One persistent work folder per graph folder: the staged files are rewritten each build, and
            # graphify's cache in `out` survives, so unchanged documents are not read again.
            work = settings().data_dir / "graph-work" / re.sub(r"[^A-Za-z0-9_-]", "_", build.folder_id)
            stage_dir = work / "stage"
            shutil.rmtree(stage_dir, ignore_errors=True)
            stage_dir.mkdir(parents=True)
            files = stage_documents(db, build.folder_id, build.document_ids, stage_dir)
        if not files:
            raise BuildError("None of the chosen documents is indexed in this folder")
        if EXTRACTOR == "gliner":
            types, chosen, notes, entities, links = _gliner_extract(build_id, files, work, limits["GRAPH_EXTRACTION_SECONDS"])
            _set_stage(build_id, "finding evidence and saving")
            with db_session() as db:
                build = db.get(GraphBuild, build_id)
                counts = save_graph(db, build, entities, types, links, chosen, files)
                counts["notes"] = notes
                build.counts, build.status, build.stage, build.finished_at = counts, "done", "done", now()
                audit(db, build.started_by, "graph.build_done", build.folder_id,
                      {"build_id": build_id, **{k: counts[k] for k in ("documents", "nodes", "links", "reviews_carried")}})
            return
        _set_stage(build_id, f"graphify reading {len(files)} document(s)")
        graph = run_graphify(work / "stage", work / "out", limits["GRAPH_EXTRACTION_SECONDS"])
        entities, links = parse_graph(graph, files)
        all_chunks = [c for f in files.values() for c in f["chunks"]]
        quotes = {}
        for index, link in enumerate(links):
            found = find_evidence(entities[link["source"]]["label"], entities[link["target"]]["label"],
                                  files[link["file"]]["chunks"] if link["file"] else all_chunks)
            if found:
                quotes[index] = found["quote"]
        with db_session() as db:  # earlier decisions keep rebuilds of unchanged documents stable
            folder_id = db.get(GraphBuild, build_id).folder_id
            old_nodes = {n.id: n for n in db.scalars(select(GraphNode).where(GraphNode.folder_id == folder_id))}
            known_types = {n.key: n.node_type for n in old_nodes.values()}
            known_links = {(old_nodes[l.source_id].key, l.relation, old_nodes[l.target_id].key): l.predicate
                           for l in db.scalars(select(GraphLink).where(GraphLink.folder_id == folder_id))
                           if l.source_id in old_nodes and l.target_id in old_nodes}
        _set_stage(build_id, f"aligning {len(entities)} entities and {len(links)} links to the ontology")
        types, chosen, notes = align(entities, links, limits["GRAPH_ALIGNMENT_SECONDS"], quotes=quotes,
                                     known_types=known_types, known_links=known_links)
        _set_stage(build_id, "finding evidence and saving")
        with db_session() as db:
            build = db.get(GraphBuild, build_id)
            counts = save_graph(db, build, entities, types, links, chosen, files)
            counts["notes"] = notes
            build.counts, build.status, build.stage, build.finished_at = counts, "done", "done", now()
            audit(db, build.started_by, "graph.build_done", build.folder_id,
                  {"build_id": build_id, **{k: counts[k] for k in ("documents", "nodes", "links", "reviews_carried")}})
    except Exception as exc:  # noqa: BLE001
        log.error("graph build %s failed: %s\n%s", build_id, exc, traceback.format_exc())
        message = str(exc) if isinstance(exc, BuildError) else f"{type(exc).__name__}; the server log has the details"
        _set_stage(build_id, "failed", status="failed", error=message[:500], finished_at=now())
    finally:
        _running.release()


def _gliner_extract(build_id: str, files: dict, work: Path, timeout: int):
    """Steps 2 and 3 with GLiNER: entities, types, links and predicates in one local pass."""
    from kb.gliner_extract import GlinerError, extract, to_graph

    chunks = [c for f in files.values() for c in f["chunks"]]
    _set_stage(build_id, f"GLiNER reading {len(chunks)} chunk(s) from {len(files)} document(s)")

    def progress(done: int, total: int) -> None:
        _set_stage(build_id, f"GLiNER reading chunk {done} of {total} (new or changed only)")

    try:
        results = extract(chunks, work / "gliner", timeout, progress)
    except GlinerError as exc:
        raise BuildError(str(exc)) from exc
    entities, types, links, chosen = to_graph(files, results)
    return types, chosen, [], entities, links


def facts_for(db, employee_id: str, query: str, limit: int = 8) -> list[dict]:
    """Approved links about entities named in the question, from the employee's entitled folders only.
    A link whose evidence chunk no longer exists (the document was reprocessed) is skipped."""
    from kb.security import permitted_folders

    folders = permitted_folders(db, employee_id)
    text = f" {normalise(query)} "
    if not folders or not text.strip():
        return []
    matched = {}
    for node in db.scalars(select(GraphNode).where(GraphNode.folder_id.in_(folders))):
        if len(node.key) >= 4 and f" {node.key} " in text:
            matched[node.id] = node
    if not matched:
        return []
    links = db.scalars(select(GraphLink).where(GraphLink.status == "approved", GraphLink.folder_id.in_(folders),
                                               (GraphLink.source_id.in_(matched)) | (GraphLink.target_id.in_(matched))))
    facts = []
    for link in links:
        source, target = db.get(GraphNode, link.source_id), db.get(GraphNode, link.target_id)
        for evidence in db.scalars(select(GraphEvidence).where(GraphEvidence.link_id == link.id)):
            if evidence.chunk_id and db.get(Chunk, evidence.chunk_id) is not None:
                specificity = max(len(n.key) for n in (source, target) if n.id in matched)
                facts.append({"link_id": link.id, "source": source.label, "predicate": link.predicate, "target": target.label,
                              "chunk_id": evidence.chunk_id, "document_id": evidence.document_id, "page": evidence.page,
                              "quote": evidence.quote, "folder_id": link.folder_id, "_rank": (specificity, link.score)})
                break
    facts.sort(key=lambda f: f["_rank"], reverse=True)
    return [{k: v for k, v in f.items() if k != "_rank"} for f in facts[:limit]]


def recover_interrupted() -> None:
    """A build that was running when the server stopped can never finish; mark it failed."""
    with db_session() as db:
        for build in db.scalars(select(GraphBuild).where(GraphBuild.status.in_(["queued", "running"]))):
            build.status, build.stage, build.finished_at = "failed", "failed", now()
            build.error = "The server restarted during this build. Start it again."
