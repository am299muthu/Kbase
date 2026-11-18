"""Entity scan: main entities, the classifier's type check, document kinds and document fit."""
import json

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from kb import entities as en
from kb.db import (Base, Chunk, Document, DocumentEntity, DocumentProfile, Employee, EntityMention, EntityScan, Folder,
                   GraphBuild)
from kb.ontology import document_kinds, node_types


def chunk(doc, number, text):
    return {"id": f"{doc}:r1:{number:06d}", "page": 1, "text": text}


FILES = {
    "doc-a": {"title": "card-policy.pdf", "chunks": [
        chunk("doc-a", 0, "The Card Dispute Policy covers the Platinum Card. The dispute rate is reported monthly."),
        chunk("doc-a", 1, "Under the Card Dispute Policy, Platinum Card holders in Singapore may dispute a charge. The dispute rate fell."),
        chunk("doc-a", 2, "We apply the Card Dispute Policy to every Platinum Card issued in Singapore."),
        chunk("doc-a", 3, "The Disputes Team handles each case within 30 days."),
        chunk("doc-a", 4, "Contact details are on the back of the card."),
    ]},
    "doc-b": {"title": "card-faq.pdf", "chunks": [
        chunk("doc-b", 0, "The Card Dispute Policy explains how to raise a dispute."),
    ]},
}


def spans(*items):
    return {"entities": [{"type": t, "text": x, "confidence": c} for t, x, c in items], "relations": []}


RESULTS = {
    "doc-a:r1:000000": spans(("policy", "Card Dispute Policy", 0.97), ("product", "Platinum Card", 0.95), ("metric", "dispute rate", 0.9)),
    "doc-a:r1:000001": spans(("policy", "Card Dispute Policy", 0.96), ("product", "Platinum Card", 0.93), ("location", "Singapore", 0.9),
                             ("metric", "dispute rate", 0.88)),
    "doc-a:r1:000002": spans(("policy", "Card Dispute Policy", 0.95), ("product", "Platinum Card", 0.94), ("location", "Singapore", 0.91),
                             ("organisation", "We", 0.8)),                        # a pronoun: left out
    "doc-a:r1:000003": spans(("organisation", "Disputes Team", 0.92), ("metric", "30", 0.9),  # no letters: left out
                             ("process", "handles", 0.3)),                         # below ENTITY_MIN: left out
    "doc-b:r1:000000": spans(("policy", "Card Dispute Policy", 0.9)),
}
SIGNS = {"policy": "Policy", "product": 'Card"', "metric": "rate", "organisation": "Team"}  # how fake_answers spots a type


def fake_answers(items):
    """Classifier answers: agrees with every extracted type except Singapore's (it leans to policy)."""
    answers = {}
    for item in items:
        if item["task"] == "document":
            answers[item["id"]] = {"label": "policy or procedure", "confidence": 0.95,
                                   "probabilities": {"policy or procedure": 0.95, "other": 0.05}}
            continue
        entity = item["text"].split(". Sentence:")[0]
        if "Singapore" in entity:
            probabilities = {"location": 0.2, "policy": 0.7, "not an entity": 0.1}
        else:
            probabilities = {"location": 0.05, "policy": 0.05, "not an entity": 0.05}
            probabilities.update({label: 0.9 for label, sign in SIGNS.items() if sign in entity})
        answers[item["id"]] = {"label": max(probabilities, key=probabilities.get), "confidence": max(probabilities.values()),
                               "probabilities": probabilities}
    return answers


def run_steps():
    found = en.collect(FILES, RESULTS)
    items, plan = en.classifier_items(found, FILES)
    answers = fake_answers(items)
    checks = en.type_checks(found, plan, answers)
    kinds = en.document_kinds_of(FILES, answers)
    mains = en.main_entities(found, checks, FILES, kinds)
    return found, items, plan, checks, kinds, mains


@pytest.fixture
def db():
    from sqlalchemy import event

    engine = create_engine("sqlite://")
    event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
    tables = [t.__table__ for t in (Employee, Folder, Document, Chunk, GraphBuild, EntityScan, EntityMention,
                                    DocumentEntity, DocumentProfile)]
    Base.metadata.create_all(engine, tables=tables)
    session = sessionmaker(engine)()
    session.add(Employee(id="E001", name="Owner", business_unit="RB", password_hash="x"))
    session.flush()
    session.add(Folder(id="f1", name="F1", path="f1", owner_id="E001", business_unit="RB", division="D", department="Dep"))
    session.commit()
    yield session
    session.close()


def test_the_ontology_document_kinds_are_usable_by_the_classifier():
    kinds = document_kinds()
    assert {"research_note", "results_release", "policy_procedure", "other"} <= set(kinds)
    for kind in kinds.values():
        assert set(kind["expected_types"]) <= set(node_types())
    tasks = en.classifier_tasks()
    for task in tasks.values():
        for label, description in task["labels"].items():
            assert not set("()[]") & set(label + description)  # the classifier refuses brackets
    assert en.NOT_AN_ENTITY in tasks["entity"]["labels"]


def test_mentions_skip_pronouns_numbers_and_weak_spans():
    found, items, plan, *_ = run_steps()
    assert {key for _, key in found} == {"card dispute policy", "platinum card", "dispute rate", "singapore", "disputes team"}
    # Three sentences for an entity in three or more chunks, one otherwise; one opening text per document.
    assert len(plan[("doc-a", "card dispute policy")]) == 3 and len(plan[("doc-a", "dispute rate")]) == 1
    assert sum(item["task"] == "document" for item in items) == 2
    assert all(item["text"].startswith('Entity: "') for item in items if item["task"] == "entity")


def test_classifier_confirms_or_disputes_but_never_retypes():
    found, _, _, checks, kinds, _ = run_steps()
    assert checks[("doc-a", "platinum card")]["check"] == "confirmed"
    singapore = checks[("doc-a", "singapore")]
    assert singapore == {"check": "disputed", "agreement": 0.2, "hint": "Policy"}
    assert found[("doc-a", "singapore")]["type"] == "Location"  # the extracted type stays
    # The classifier's own pick counts as agreement even when its probability is below TYPE_AGREE.
    item = {"type": "Organisation", "chunks": {}}
    low = en.type_checks({("d", "k"): item}, {("d", "k"): ["x"]},
                         {"x": {"probabilities": {"organisation": 0.4, "location": 0.3, "not an entity": 0.2}}})
    assert low[("d", "k")]["check"] == "confirmed"
    assert kinds["doc-a"]["kind"] == "policy_procedure" and kinds["doc-a"]["confidence"] == 0.95


def test_main_entities_rank_confirmed_entities_and_flag_types_the_kind_does_not_expect():
    *_, mains = run_steps()
    top = [(r["label"], r["fit"]) for r in mains["doc-a"]]
    # Platinum Card (3 chunks, this document only) beats Card Dispute Policy (3 chunks, both documents).
    # Singapore is disputed; the Disputes Team is in one chunk of a five-chunk document.
    assert top == [("Platinum Card", "expected"), ("dispute rate", "unexpected"), ("Card Dispute Policy", "expected")]
    assert [r["rank"] for r in mains["doc-a"]] == [1, 2, 3]
    assert mains["doc-b"][0]["label"] == "Card Dispute Policy"  # one-chunk document: one chunk is enough


def test_periods_are_never_main_entities():
    from kb.graph_build import normalise
    for label in ("1Q26", "31 December 2025", "First Quarter 2026", "7-May", "November-December", "2Q 2024", "1Q2018",
                  "FY 2023", "2H 2025", "Mar 2026", "2024"):
        assert en.is_period(normalise(label)), label
    for label in ("Annual General Meeting", "Q1 results", "May Bank", "Covid-19", "Basel III"):
        assert not en.is_period(normalise(label)), label


def test_classify_reads_only_items_missing_from_the_cache(tmp_path, monkeypatch):
    from kb import gliner_extract

    sent = []

    def fake_worker(arguments, work, timeout, progress, log_name):
        inputs = arguments[arguments.index("--input") + 1]
        out = arguments[arguments.index("--out") + 1]
        rows = [json.loads(line) for line in open(inputs, encoding="utf-8")]
        sent.append(len(rows))
        with open(out, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps({"id": row["id"], "label": "policy", "confidence": 0.9,
                                         "probabilities": {"policy": 0.9}}) + "\n")

    monkeypatch.setattr(gliner_extract, "run_worker_process", fake_worker)
    items = [{"id": "a", "task": "entity", "text": "x"}, {"id": "b", "task": "entity", "text": "x"},
             {"id": "c", "task": "entity", "text": "y"}]
    first = en.classify(items, tmp_path, 60)
    assert sent == [2] and first["a"] == first["b"]          # the same text is read once
    en.classify(items, tmp_path, 60)
    assert sent == [2]                                        # all cached
    en.classify(items + [{"id": "d", "task": "document", "text": "x"}], tmp_path, 60)
    assert sent == [2, 1]                                     # only the new item


def test_saving_a_scan_replaces_the_last_one_and_feeds_search(db):
    found, _, _, checks, kinds, mains = run_steps()
    for document_id, source in FILES.items():
        db.add(Document(id=document_id, title=source["title"], folder_id="f1", status="indexed", latest_date="2026-01-01",
                        maker_id="E001", staging_key="k", sha256="0" * 64))
    db.flush()
    for document_id, source in FILES.items():
        for c in source["chunks"]:
            db.add(Chunk(id=c["id"], document_id=document_id, folder_id="f1", text=c["text"], section="", page=1, payload={}))
    for number in (1, 2):
        scan = EntityScan(id=f"s{number}", folder_id="f1", status="done", started_by="E001", finished_at=f"2026-11-18T0{number}")
        db.add(scan)
        db.flush()
        counts = en.save_scan(db, scan, FILES, found, checks, kinds, mains)
    db.commit()
    mentions = db.scalar(select(func.count()).select_from(EntityMention))
    assert mentions == counts["chunk_tags"] == 12 and set(db.scalars(select(EntityMention.scan_id))) == {"s2"}
    assert counts["main_entities"] == 4 and counts["unexpected"] == 1 and counts["disputed"] == 1
    profile = db.get(DocumentProfile, "doc-a")
    assert (profile.kind, profile.confirmed, profile.disputed, profile.unexpected) == ("policy_procedure", 4, 1, 1)

    assert en.named_in(db, ["f1"], "Is the Platinum Card covered in Singapore?") == {"platinum card"}  # Singapore is disputed
    assert en.named_in(db, ["other-folder"], "Platinum Card") == set()
    tags = en.chunk_entities(db, ["doc-a:r1:000001"])
    assert set(tags["doc-a:r1:000001"]) == {"Card Dispute Policy", "Platinum Card", "dispute rate"}

    view = en.folder_view(db, "f1")
    row = next(d for d in view["documents"] if d["id"] == "doc-a")
    assert row["kind_label"] == "policy or procedure" and [m["label"] for m in row["main"]][0] == "Platinum Card"
    detail = en.document_view(db, "doc-a")
    assert detail["total"] == 5 and detail["entities"][0]["chunks"] == 3
    assert next(e for e in detail["entities"] if e["label"] == "Singapore")["hint"] == "Policy"


def test_only_one_scan_or_graph_build_at_a_time(db):
    db.add(Document(id="doc-a", title="a.pdf", folder_id="f1", status="indexed", latest_date="2026-01-01",
                    maker_id="E001", staging_key="k", sha256="0" * 64))
    db.add(GraphBuild(id="running", folder_id="f1", status="running", started_by="E001"))
    db.commit()
    with pytest.raises(en.ScanError, match="running"):
        en.start_scan(db, "f1", "E001")
