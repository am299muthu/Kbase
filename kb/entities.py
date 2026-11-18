"""Main entities of each document, checked by a second model, all with GLiNER2.5 on this machine.

A scan reads one folder's indexed documents:

1. Mentions. GLiNER2.5's entity spans for every chunk, from the per-chunk cache the GLiNER graph build
   fills (kb/gliner_extract.py), so a folder that already has a GLiNER graph needs no new extraction.
   Spans below gliner_extract.ENTITY_MIN, without letters, or pronouns are left out.
2. Type check. The same model's classifier reads each entity in up to MAX_SENTENCES sentences where it
   appears and gives the probability of every ontology type, and of "not an entity". When the
   extracted type gets TYPE_AGREE or more on average the entity is confirmed, otherwise disputed, with
   the classifier's own pick kept as a hint. Nothing is retyped: on a Finance sample the classifier's
   own pick was wrong about as often as right, but the entities it agreed with were right about 17
   times in 20, against about half for the rest.
3. Document kind. The classifier reads the title and the first three chunks and picks one of the
   ontology's DocumentKind values.
4. Main entities. Per document, confirmed entities ranked by the chunks that mention them, weighted
   down when most documents of the folder mention them too. The top MAIN_PER_DOCUMENT are kept.
5. Document fit. A main entity whose type is not among its document kind's expected types is
   flagged "unexpected". Flags are for people to look at; nothing is dropped because of them.
6. Save. Chunk tags and main entities replace the earlier scan of the same documents. With search on
   OpenSearch, each chunk's confirmed entity keys also go to its `entities` field, so Ask can boost
   chunks about an entity the question names (kb/search.py).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import secrets
import threading
import traceback
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable

from sqlalchemy import delete, func, insert, select

from kb.config import settings
from kb.db import (Chunk, Document, DocumentEntity, DocumentProfile, EntityMention, EntityScan, GraphBuild, audit,
                   db_session, now)

log = logging.getLogger(__name__)
TYPE_AGREE = 0.5          # classifier probability of the extracted type needed to confirm it
MAX_SENTENCES = 3         # sentences read per entity when it is in 3 or more chunks (else 1)
MAIN_PER_DOCUMENT = 10
DOCUMENT_CHARS = 3000     # title and opening text read to choose the document kind
NOT_AN_ENTITY = "not an entity"
_PRONOUNS = {"we", "our", "us", "it", "its", "they", "their", "them", "you", "your", "i", "he", "she", "his", "her",
             "this", "that", "these", "those"}
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_BRACKETS = re.compile(r"[()\[\]]")  # the classifier refuses brackets in labels and descriptions
_LETTER = re.compile(r"[^\W\d_]")
_tag_cache: dict[tuple, list[str]] = {}
# A period ("1Q26", "31 December 2025", "first quarter 2026") says when, not what a document is about,
# so period-like Event entities stay chunk tags but are never main entities.
_MONTHS = ("jan january feb february mar march apr april may jun june jul july aug august sep sept september "
           "oct october nov november dec december").split()
_PERIOD_WORDS = set(_MONTHS) | {"first", "second", "third", "fourth", "quarter", "half", "year", "fy", "to", "and"}
_PERIOD_TOKEN = re.compile(r"\d+|[1-4]q\d*|q[1-4]\d*|[12]h\d*|h[12]\d*|fy\d+|\d+[1-4]q")


def is_period(key: str) -> bool:
    tokens = key.split()
    return bool(tokens) and all(t in _PERIOD_WORDS or _PERIOD_TOKEN.fullmatch(t) for t in tokens)


class ScanError(RuntimeError):
    pass


def _plain(text: str) -> str:
    return " ".join(_BRACKETS.sub(" ", text).split())


def _work_dir(folder_id: str) -> Path:
    # The GLiNER graph build's folder (kb/graph_build.py), so both share one extraction cache.
    return settings().data_dir / "graph-work" / re.sub(r"[^A-Za-z0-9_-]", "_", folder_id) / "gliner"


# ----------------------------------------------------------------------------------- classifier

def classifier_tasks() -> dict:
    """The two classification tasks, worded from the ontology: entity type and document kind."""
    from kb.gliner_extract import TYPES
    from kb.ontology import document_kinds, node_types

    types, kinds = node_types(), document_kinds()
    if not kinds:
        raise ScanError("the ontology has no DocumentKind values")
    entity_labels = {label: _plain(types[name]) for name, label in TYPES.items()}
    entity_labels[NOT_AN_ENTITY] = ("A generic word, a pronoun, a number, a date fragment or a piece of a sentence, "
                                    "not a named thing.")
    return {
        "entity": {"labels": entity_labels, "instruction": "What kind of thing is the quoted entity in this sentence?"},
        "document": {"labels": {_plain(k["label"]): _plain(k["description"] or k["label"]) for k in kinds.values()},
                     "instruction": "What kind of document is this?"},
    }


def classify(items: list[dict], work: Path, timeout: int,
             progress: Callable[[int, int], None] | None = None) -> dict[str, dict]:
    """Classifier results by item id for `items` ({id, text, task}). Results are cached on the model, the
    task wording and the text, so only new items are read."""
    from kb.gliner_extract import MODEL, GlinerError, run_worker_process

    work.mkdir(parents=True, exist_ok=True)
    tasks_text = json.dumps(classifier_tasks(), sort_keys=True)

    def key(item: dict) -> str:
        return hashlib.sha1("\x1f".join((MODEL, tasks_text, item["task"], item["text"])).encode("utf-8")).hexdigest()

    cache_file = work / "classify-cache.jsonl"
    cache: dict[str, dict] = {}
    if cache_file.exists():
        for line in cache_file.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                cache[row["id"]] = row
            except (ValueError, KeyError):
                continue
    keys = {item["id"]: key(item) for item in items}
    pending = {}
    for item in items:
        if keys[item["id"]] not in cache:
            pending.setdefault(keys[item["id"]], item)  # the same text is read once
    if pending:
        results = work / "classify-results.jsonl"
        results.unlink(missing_ok=True)
        (work / "classify-input.jsonl").write_text(
            "".join(json.dumps({"id": k, "text": i["text"], "task": i["task"]}) + "\n" for k, i in pending.items()),
            encoding="utf-8")
        (work / "classify-tasks.json").write_text(tasks_text, encoding="utf-8")
        try:
            run_worker_process(["--task", "classify", "--input", str(work / "classify-input.jsonl"),
                                "--schema", str(work / "classify-tasks.json"), "--out", str(results)],
                               work, timeout, progress, "classify-worker.log")
        except GlinerError as exc:
            raise ScanError(str(exc)) from exc
        finally:  # keep what the worker finished, so a retry only reads the rest
            lines = results.read_text(encoding="utf-8").splitlines() if results.exists() else []
            with cache_file.open("a", encoding="utf-8") as out:
                for line in lines:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if row.get("id") in pending:
                        cache[row["id"]] = row
                        out.write(json.dumps(row) + "\n")
    missing = [i for i, k in keys.items() if k not in cache]
    if missing:
        raise ScanError(f"the classifier returned no result for {len(missing)} item(s)")
    return {i: cache[k] for i, k in keys.items()}


# ----------------------------------------------------------------------------------- steps

def load_documents(db, folder_id: str, document_ids: list[str]) -> dict[str, dict]:
    """document id -> {title, chunks: [{id, page, text}]} for the indexed documents among `document_ids`."""
    files = {}
    for document in db.scalars(select(Document).where(Document.folder_id == folder_id, Document.id.in_(document_ids),
                                                       Document.status == "indexed")):
        chunks = [{"id": c.id, "page": c.page, "text": c.text}
                  for c in db.scalars(select(Chunk).where(Chunk.document_id == document.id).order_by(Chunk.id))]
        if chunks:
            files[document.id] = {"title": document.title, "chunks": chunks}
    return files


def collect(files: dict[str, dict], results: dict[str, dict]) -> dict[tuple[str, str], dict]:
    """(document id, entity key) -> {label, type, chunks: {chunk id: best mention}} from GLiNER spans.
    An entity's type and label are the ones with the most support over its mentions in the document."""
    from kb.gliner_extract import ENTITY_MIN, TYPES
    from kb.graph_build import normalise

    type_of_label = {label: name for name, label in TYPES.items()}
    found: dict[tuple[str, str], dict] = {}
    for document_id, source in files.items():
        for chunk in source["chunks"]:
            for entity in (results.get(chunk["id"]) or {}).get("entities", []):
                kind, text = type_of_label.get(entity["type"]), entity["text"].strip()
                key = normalise(text)
                if (not kind or entity["confidence"] < ENTITY_MIN or not _LETTER.search(text)
                        or not 2 <= len(key) <= 190 or key in _PRONOUNS):
                    continue
                item = found.setdefault((document_id, key), {"labels": Counter(), "types": defaultdict(float), "chunks": {}})
                item["labels"][text] += 1
                item["types"][kind] += entity["confidence"]
                best = item["chunks"].get(chunk["id"])
                if best is None or entity["confidence"] > best["confidence"]:
                    item["chunks"][chunk["id"]] = {"text": text, "confidence": entity["confidence"], "chunk_text": chunk["text"]}
    for item in found.values():
        item["label"] = item["labels"].most_common(1)[0][0]
        item["type"] = max(item["types"], key=item["types"].get)
    return found


def sentence_for(text: str, chunk_text: str) -> str:
    """The sentence (or table row) of the chunk that contains `text`, else the text around it."""
    lowered = text.casefold()
    for sentence in _SENTENCE.split(chunk_text):
        if lowered in sentence.casefold():
            return " ".join(sentence.split())[:400]
    at = chunk_text.casefold().find(lowered)
    start = max(0, at - 150) if at >= 0 else 0
    return " ".join(chunk_text[start:start + 400].split())


def classifier_items(found: dict, files: dict) -> tuple[list[dict], dict]:
    """Classifier inputs: up to MAX_SENTENCES sentences per entity, and one opening text per document.
    Returns (items, item ids by (document id, key))."""
    items, plan = [], {}
    for index, ((document_id, key), item) in enumerate(found.items()):
        mentions = sorted(item["chunks"].values(), key=lambda m: -m["confidence"])
        mentions = mentions[:MAX_SENTENCES if len(mentions) >= 3 else 1]
        ids = []
        for number, mention in enumerate(mentions):
            sentence = sentence_for(mention["text"], mention["chunk_text"])
            ids.append(f"e{index}:{number}")
            items.append({"id": ids[-1], "task": "entity", "text": f'Entity: "{mention["text"]}". Sentence: "{sentence}"'})
        plan[(document_id, key)] = ids
    for document_id, source in files.items():
        opening = " ".join(c["text"] for c in source["chunks"][:3])
        text = f"Title: {Path(source['title']).stem}. {' '.join(opening.split())}"[:DOCUMENT_CHARS]
        items.append({"id": f"d:{document_id}", "task": "document", "text": text})
    return items, plan


def type_checks(found: dict, plan: dict, answers: dict) -> dict[tuple[str, str], dict]:
    """(document id, key) -> {check, agreement, hint}: the classifier's view of each entity's type."""
    from kb.gliner_extract import TYPES

    name_of_label = {label: name for name, label in TYPES.items()}
    checks = {}
    for pair, item in found.items():
        rows = [answers[i]["probabilities"] for i in plan[pair]]
        average = {label: sum(r.get(label, 0.0) for r in rows) / len(rows) for label in rows[0]}
        agreement = average.get(TYPES[item["type"]], 0.0)
        pick = name_of_label.get(max(average, key=average.get), NOT_AN_ENTITY)
        # Confirmed when the classifier gives the extracted type enough probability, or picks it itself
        # (its probabilities are scored per label, so the top label can sit below TYPE_AGREE).
        confirmed = agreement >= TYPE_AGREE or pick == item["type"]
        checks[pair] = {"check": "confirmed" if confirmed else "disputed", "agreement": round(agreement, 3), "hint": pick}
    return checks


def document_kinds_of(files: dict, answers: dict) -> dict[str, dict]:
    """document id -> {kind, confidence, scores} from the classifier's document answers."""
    from kb.ontology import document_kinds

    name_of_label = {_plain(k["label"]): name for name, k in document_kinds().items()}
    kinds = {}
    for document_id in files:
        answer = answers[f"d:{document_id}"]
        kinds[document_id] = {"kind": name_of_label.get(answer["label"], ""), "confidence": round(answer["confidence"], 3),
                              "scores": {name_of_label.get(l, l): round(p, 3) for l, p in answer["probabilities"].items()}}
    return kinds


def main_entities(found: dict, checks: dict, files: dict, kinds: dict) -> dict[str, list[dict]]:
    """document id -> its top confirmed entities, each with its document-fit flag.
    score = chunks mentioning the entity x log(1 + documents / documents mentioning it)."""
    from kb.ontology import document_kinds

    expected = {name: set(k["expected_types"]) for name, k in document_kinds().items()}
    documents = len(files)
    spread = Counter(key for _, key in found)
    ranked: dict[str, list[dict]] = defaultdict(list)
    for (document_id, key), item in found.items():
        check = checks[(document_id, key)]
        chunks = len(item["chunks"])
        if (check["check"] != "confirmed" or (len(files[document_id]["chunks"]) >= 4 and chunks < 2)
                or (item["type"] == "Event" and is_period(key))):
            continue
        ranked[document_id].append({"key": key, "label": item["label"], "type": item["type"], "chunks": chunks,
                                    "agreement": check["agreement"],
                                    "score": round(chunks * math.log(1 + documents / spread[key]), 3)})
    result = {}
    for document_id, rows in ranked.items():
        allowed = expected.get(kinds.get(document_id, {}).get("kind", ""), set())
        top = sorted(rows, key=lambda r: (-r["score"], r["key"]))[:MAIN_PER_DOCUMENT]
        for rank, row in enumerate(top, 1):
            row["rank"] = rank
            row["fit"] = "unchecked" if not allowed else "expected" if row["type"] in allowed else "unexpected"
        result[document_id] = top
    return result


def save_scan(db, scan: EntityScan, files: dict, found: dict, checks: dict, kinds: dict, mains: dict) -> dict:
    """Replace the earlier scan of these documents with this one. Returns the scan's counts."""
    folder_id, ids = scan.folder_id, list(files)
    for table in (EntityMention, DocumentEntity, DocumentProfile):
        db.execute(delete(table).where(table.folder_id == folder_id, table.document_id.in_(ids)))
    rows = []
    for (document_id, key), item in found.items():
        check = checks[(document_id, key)]
        for chunk_id, mention in item["chunks"].items():
            rows.append({"folder_id": folder_id, "document_id": document_id, "chunk_id": chunk_id, "key": key,
                         "label": item["label"], "entity_type": item["type"], "confidence": mention["confidence"],
                         "type_check": check["check"], "agreement": check["agreement"], "hint": check["hint"], "scan_id": scan.id})
    for start in range(0, len(rows), 5000):
        db.execute(insert(EntityMention), rows[start:start + 5000])
    main_rows = [{"folder_id": folder_id, "document_id": document_id, "key": r["key"], "label": r["label"],
                  "entity_type": r["type"], "rank": r["rank"], "chunks": r["chunks"], "score": r["score"],
                  "agreement": r["agreement"], "fit": r["fit"], "scan_id": scan.id}
                 for document_id, top in mains.items() for r in top]
    if main_rows:
        db.execute(insert(DocumentEntity), main_rows)
    per_document = defaultdict(Counter)
    for (document_id, _), check in checks.items():
        per_document[document_id][check["check"]] += 1
    for document_id in ids:
        counted = per_document[document_id]
        db.add(DocumentProfile(document_id=document_id, folder_id=folder_id, kind=kinds[document_id]["kind"],
                               kind_confidence=kinds[document_id]["confidence"], kind_scores=kinds[document_id]["scores"],
                               entities=sum(counted.values()), confirmed=counted["confirmed"], disputed=counted["disputed"],
                               unexpected=sum(r["fit"] == "unexpected" for r in mains.get(document_id, [])),
                               scan_id=scan.id, scanned_at=now()))
    db.flush()
    _tag_cache.clear()
    totals = Counter(c["check"] for c in checks.values())
    return {"documents": len(ids), "entities": len(found), "confirmed": totals["confirmed"], "disputed": totals["disputed"],
            "chunk_tags": len(rows), "main_entities": len(main_rows),
            "unexpected": sum(r["fit"] == "unexpected" for r in main_rows),
            "kinds": dict(Counter(k["kind"] for k in kinds.values()))}


def search_tags(files: dict, found: dict, checks: dict) -> dict[str, dict[str, list[str]]]:
    """document id -> {chunk id: confirmed entity keys}, every chunk of the document included."""
    tags: dict[str, set] = defaultdict(set)
    for (document_id, key), item in found.items():
        if checks[(document_id, key)]["check"] == "confirmed":
            for chunk_id in item["chunks"]:
                tags[chunk_id].add(key)
    return {document_id: {c["id"]: sorted(tags.get(c["id"], ())) for c in source["chunks"]}
            for document_id, source in files.items()}


# ----------------------------------------------------------------------------------- run

def _set(scan_id: str, stage: str, **fields) -> None:
    with db_session() as db:
        scan = db.get(EntityScan, scan_id)
        scan.stage = stage[:120]
        for key, value in fields.items():
            setattr(scan, key, value)


def start_scan(db, folder_id: str, employee_id: str) -> EntityScan:
    from kb.gliner_extract import MODEL
    from kb.ontology import version

    busy = (db.scalar(select(EntityScan.id).where(EntityScan.status.in_(["queued", "running"])).limit(1))
            or db.scalar(select(GraphBuild.id).where(GraphBuild.status.in_(["queued", "running"])).limit(1)))
    if busy:
        raise ScanError("An entity scan or graph build is running. Wait for it to finish.")
    ids = list(db.scalars(select(Document.id).where(Document.folder_id == folder_id, Document.status == "indexed")))
    if not ids:
        raise ScanError("This folder has no indexed documents.")
    scan = EntityScan(id=secrets.token_hex(16), folder_id=folder_id, status="queued", stage="queued", started_by=employee_id,
                      document_ids=ids, model=MODEL.rsplit("/", 1)[-1], ontology_version=version())
    db.add(scan)
    db.flush()
    return scan


def launch(scan_id: str) -> None:
    threading.Thread(target=run_scan, args=(scan_id,), name=f"entity-scan-{scan_id[:8]}", daemon=True).start()


def run_scan(scan_id: str) -> None:
    from kb.gliner_extract import GlinerError, extract
    from kb.graph_build import _running  # one GLiNER job on the GPU at a time
    from kb.processing_limits import load_limits

    if not _running.acquire(blocking=False):
        _set(scan_id, "failed", status="failed", error="A graph build or entity scan is running", finished_at=now())
        return
    try:
        timeout = load_limits()["GRAPH_EXTRACTION_SECONDS"]
        _set(scan_id, "reading documents", status="running")
        with db_session() as db:
            scan = db.get(EntityScan, scan_id)
            folder_id = scan.folder_id
            files = load_documents(db, folder_id, scan.document_ids)
        if not files:
            raise ScanError("None of the folder's documents is indexed")
        chunks = [{"id": c["id"], "text": c["text"]} for f in files.values() for c in f["chunks"]]
        _set(scan_id, f"GLiNER reading {len(chunks)} chunk(s) of {len(files)} document(s)")
        try:
            results = extract(chunks, _work_dir(folder_id), timeout,
                              lambda done, total: _set(scan_id, f"GLiNER reading chunk {done} of {total} (new or changed only)"))
        except GlinerError as exc:
            raise ScanError(str(exc)) from exc
        found = collect(files, results)
        items, plan = classifier_items(found, files)
        _set(scan_id, f"checking {len(found)} entities and {len(files)} document kinds")
        answers = classify(items, _work_dir(folder_id), timeout,
                           lambda done, total: _set(scan_id, f"classifier checking item {done} of {total} (new only)"))
        checks = type_checks(found, plan, answers)
        kinds = document_kinds_of(files, answers)
        mains = main_entities(found, checks, files, kinds)
        _set(scan_id, "saving")
        with db_session() as db:
            scan = db.get(EntityScan, scan_id)
            counts = save_scan(db, scan, files, found, checks, kinds, mains)
            scan.counts, scan.stage = counts, "writing entity tags to the search index"
            audit(db, scan.started_by, "entities.scan_done", folder_id,
                  {"scan_id": scan_id, **{k: counts[k] for k in ("documents", "entities", "confirmed", "main_entities")}})
        if settings().search_backend == "opensearch":
            from kb.search import write_entity_tags
            try:  # the scan is saved; a search index that is down only loses the boost until the next scan
                with db_session() as db:
                    counts = {**counts, "search_tag_failures": write_entity_tags(db, search_tags(files, found, checks))}
            except Exception as exc:  # noqa: BLE001
                log.warning("entity scan %s: writing search tags failed: %s", scan_id, exc)
                counts = {**counts, "notes": [f"Entity tags were not written to the search index ({type(exc).__name__})"]}
        _set(scan_id, "done", status="done", counts=counts, finished_at=now())
    except Exception as exc:  # noqa: BLE001
        log.error("entity scan %s failed: %s\n%s", scan_id, exc, traceback.format_exc())
        message = str(exc) if isinstance(exc, ScanError) else f"{type(exc).__name__}; the server log has the details"
        _set(scan_id, "failed", status="failed", error=message[:500], finished_at=now())
    finally:
        _running.release()


def recover_interrupted() -> None:
    """A scan that was running when the server stopped can never finish; mark it failed."""
    with db_session() as db:
        for scan in db.scalars(select(EntityScan).where(EntityScan.status.in_(["queued", "running"]))):
            scan.status, scan.stage, scan.finished_at = "failed", "failed", now()
            scan.error = "The server restarted during this scan. Start it again."


# ----------------------------------------------------------------------------------- read

def named_in(db, folders: list[str], query: str) -> set[str]:
    """Keys of confirmed entities in these folders that the question names word for word."""
    from kb.graph_build import normalise

    text = f" {normalise(query)} "
    if not folders or not text.strip():
        return set()
    latest = db.scalar(select(func.max(EntityScan.finished_at)).where(EntityScan.folder_id.in_(folders),
                                                                      EntityScan.status == "done"))
    if not latest:
        return set()
    cache_key = (tuple(sorted(folders)), latest)
    keys = _tag_cache.get(cache_key)
    if keys is None:
        keys = [k for k in db.scalars(select(EntityMention.key).where(EntityMention.folder_id.in_(folders),
                                                                      EntityMention.type_check == "confirmed").distinct())
                if len(k) >= 4]
        _tag_cache.clear()
        _tag_cache[cache_key] = keys
    return {k for k in keys if f" {k} " in text}


def chunk_entities(db, chunk_ids: list[str], limit: int = 8) -> dict[str, list[str]]:
    """chunk id -> labels of its confirmed entities, most confident first."""
    tags: dict[str, list[str]] = defaultdict(list)
    if chunk_ids:
        for row in db.scalars(select(EntityMention).where(EntityMention.chunk_id.in_(chunk_ids), EntityMention.type_check == "confirmed")
                              .order_by(EntityMention.confidence.desc())):
            if len(tags[row.chunk_id]) < limit and row.label not in tags[row.chunk_id]:
                tags[row.chunk_id].append(row.label)
    return dict(tags)


def folder_view(db, folder_id: str) -> dict:
    from kb.ontology import document_kinds

    kinds = document_kinds()
    profiles = {p.document_id: p for p in db.scalars(select(DocumentProfile).where(DocumentProfile.folder_id == folder_id))}
    mains = defaultdict(list)
    for row in db.scalars(select(DocumentEntity).where(DocumentEntity.folder_id == folder_id)
                          .order_by(DocumentEntity.document_id, DocumentEntity.rank)):
        mains[row.document_id].append({"label": row.label, "type": row.entity_type, "chunks": row.chunks,
                                       "score": row.score, "agreement": row.agreement, "fit": row.fit})
    documents = []
    for document in db.scalars(select(Document).where(Document.folder_id == folder_id, Document.status == "indexed")
                               .order_by(Document.title)):
        profile = profiles.get(document.id)
        documents.append({
            "id": document.id, "title": document.title, "scanned": profile is not None,
            "kind": profile.kind if profile else "", "kind_label": kinds.get(profile.kind, {}).get("label", "") if profile else "",
            "kind_confidence": profile.kind_confidence if profile else None,
            "entities": profile.entities if profile else 0, "confirmed": profile.confirmed if profile else 0,
            "disputed": profile.disputed if profile else 0, "unexpected": profile.unexpected if profile else 0,
            "main": mains.get(document.id, [])})
    scans = [{"id": s.id, "status": s.status, "stage": s.stage, "started_by": s.started_by, "created_at": s.created_at,
              "finished_at": s.finished_at, "model": s.model, "ontology_version": s.ontology_version,
              "documents": len(s.document_ids or []), "counts": s.counts, "error": s.error}
             for s in db.scalars(select(EntityScan).where(EntityScan.folder_id == folder_id)
                                 .order_by(EntityScan.created_at.desc()).limit(5))]
    return {"folder_id": folder_id, "documents": documents, "scans": scans,
            "kinds": {name: {"label": k["label"], "expected_types": k["expected_types"]} for name, k in kinds.items()}}


def document_view(db, document_id: str, limit: int = 80) -> dict:
    """Every entity of one document with its check, most mentioned first."""
    from kb.ontology import document_kinds

    rows: dict[str, dict] = {}
    for row in db.scalars(select(EntityMention).where(EntityMention.document_id == document_id)):
        entry = rows.setdefault(row.key, {"label": row.label, "type": row.entity_type, "check": row.type_check,
                                          "agreement": row.agreement, "hint": row.hint, "chunks": 0, "confidence": 0.0})
        entry["chunks"] += 1
        entry["confidence"] = max(entry["confidence"], row.confidence)
    profile = db.get(DocumentProfile, document_id)
    kinds = document_kinds()
    main = {r.key: r.fit for r in db.scalars(select(DocumentEntity).where(DocumentEntity.document_id == document_id))}
    entities = sorted(rows.items(), key=lambda kv: (-kv[1]["chunks"], kv[0]))
    scores = sorted((profile.kind_scores or {}).items(), key=lambda kv: -kv[1])[:4] if profile else []
    return {
        "document_id": document_id, "total": len(rows),
        "kind": profile.kind if profile else "", "kind_label": kinds.get(profile.kind, {}).get("label", "") if profile else "",
        "kind_confidence": profile.kind_confidence if profile else None,
        "kind_scores": [{"kind": kinds.get(k, {}).get("label", k), "p": p} for k, p in scores],
        "entities": [{**e, "main": key in main, "fit": main.get(key, "")} for key, e in entities[:limit]],
    }
