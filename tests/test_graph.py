"""Knowledge graph builder: parsing graphify output, ontology alignment, evidence, saving and reviews."""
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from kb import graph_build as gb
from kb.db import Base, Chunk, Document, Employee, Folder, GraphBuild, GraphEvidence, GraphLink, GraphNode
from kb.ontology import node_types, predicates

FILES = {
    "card-policy-aaaaaaaa.md": {"document_id": "doc-a", "title": "card-policy.md", "chunks": [
        {"id": "doc-a:r1:000000", "page": 1, "text": "The Card Dispute Policy applies to Platinum Card holders. Disputes close in 30 days."},
        {"id": "doc-a:r1:000001", "page": 2, "text": "The Disputes Team reports to Retail Operations."},
    ]},
}
GRAPH = {
    "nodes": [
        {"id": "card_policy", "label": "card-policy", "file_type": "document", "source_file": "card-policy-aaaaaaaa.md"},
        {"id": "p", "label": "Card Dispute Policy", "file_type": "concept", "source_file": "card-policy-aaaaaaaa.md"},
        {"id": "c", "label": "Platinum Card", "file_type": "concept", "source_file": "card-policy-aaaaaaaa.md"},
        {"id": "t", "label": "Disputes Team", "file_type": "concept", "source_file": "card-policy-aaaaaaaa.md"},
        {"id": "o", "label": "Retail Operations", "file_type": "concept", "source_file": "card-policy-aaaaaaaa.md"},
    ],
    "links": [
        {"source": "p", "target": "c", "relation": "references", "confidence": "EXTRACTED", "confidence_score": 0.95,
         "source_file": "card-policy-aaaaaaaa.md"},
        {"source": "t", "target": "o", "relation": "conceptually_related_to", "confidence": "INFERRED", "confidence_score": 0.7,
         "source_file": "card-policy-aaaaaaaa.md"},
        {"source": "card_policy", "target": "p", "relation": "references", "confidence": "EXTRACTED", "confidence_score": 1.0,
         "source_file": "card-policy-aaaaaaaa.md"},
        {"source": "p", "target": "p", "relation": "references", "confidence": "EXTRACTED", "confidence_score": 1.0},
    ],
}


@pytest.fixture
def db():
    from sqlalchemy import event

    engine = create_engine("sqlite://")
    event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))  # as in the app
    tables = [t.__table__ for t in (Employee, Folder, Document, Chunk, GraphBuild, GraphNode, GraphLink, GraphEvidence)]
    Base.metadata.create_all(engine, tables=tables)
    session = sessionmaker(engine)()
    session.add(Employee(id="E001", name="Owner", business_unit="RB", password_hash="x"))
    session.flush()
    session.add(Folder(id="f1", name="F1", path="f1", owner_id="E001", business_unit="RB", division="D", department="Dep"))
    session.commit()
    yield session
    session.close()


def fake_model(prompt, schema, timeout):
    """Types by label and predicates by relation, as a model constrained to the ontology would."""
    types = {"Card Dispute Policy": "Policy", "Platinum Card": "Product", "Disputes Team": "Organisation",
             "Retail Operations": "Organisation"}
    entity_part = prompt.split("Entities (id: label):\n", 1)[1].split("\n\nLinks", 1)[0]
    link_part = prompt.split("Links (id: source --extracted relation--> target):\n", 1)[1].split("\n\nReturn", 1)[0]
    entities = [line.split(": ", 1) for line in entity_part.splitlines() if ": " in line]
    links = [line.split(": ", 1) for line in link_part.splitlines() if ": " in line]
    return {"types": [{"id": int(i), "type": types.get(label, "Bogus")} for i, label in entities],
            "links": [{"id": int(i), "predicate": "applies_to" if "Card Dispute Policy --references--> Platinum" in text
                       else "part_of" if "Disputes Team" in text else "not_a_predicate"} for i, text in links]}


def test_parse_graph_turns_the_file_node_into_a_document_and_drops_self_links():
    entities, links = gb.parse_graph(GRAPH, FILES)
    assert entities["card policy md"]["document"] == "doc-a" and entities["card policy md"]["label"] == "card-policy.md"
    assert {(l["source"], l["target"]) for l in links} == {
        ("card dispute policy", "platinum card"), ("disputes team", "retail operations"), ("card policy md", "card dispute policy")}


def test_alignment_keeps_only_ontology_values_and_falls_back():
    entities, links = gb.parse_graph(GRAPH, FILES)
    types, chosen, notes = gb.align(entities, links, 10, call=fake_model)
    assert types["card dispute policy"] == "Policy" and types["platinum card"] == "Product"
    assert types["card policy md"] == "Document"  # set by rule, not by the model
    assert set(types.values()) <= set(node_types()) | {"Document"}
    assert set(chosen.values()) <= set(predicates())  # "not_a_predicate" was dropped
    assert notes == []

    def broken(prompt, schema, timeout):
        raise RuntimeError("model down")
    types, chosen, notes = gb.align(entities, links, 10, call=broken)
    assert types["platinum card"] == "Term" and chosen == {} and "failed" in notes[0]


def test_alignment_reuses_earlier_types_and_predicates_and_only_asks_about_new_items():
    entities, links = gb.parse_graph(GRAPH, FILES)
    asked = []

    def recording(prompt, schema, timeout):
        asked.append(prompt)
        return fake_model(prompt, schema, timeout)

    known_types = {k: "Organisation" for k in entities if k != "card policy md"}
    known_links = {(l["source"], l["relation"], l["target"]): "owned_by" for l in links}
    types, chosen, _ = gb.align(entities, links, 10, call=recording, known_types=known_types, known_links=known_links)
    assert asked == []  # everything was known: no model call at all
    assert types["platinum card"] == "Organisation" and set(chosen.values()) == {"owned_by"}

    del known_types["platinum card"]
    types, chosen, _ = gb.align(entities, links, 10, call=recording, known_types=known_types, known_links=known_links)
    assert len(asked) == 1 and "Platinum Card" in asked[0] and "Disputes Team" not in asked[0]
    assert types["platinum card"] == "Product"


def test_evidence_prefers_the_chunk_and_sentence_with_both_entities():
    evidence = gb.find_evidence("Disputes Team", "Retail Operations", FILES["card-policy-aaaaaaaa.md"]["chunks"])
    assert evidence == {"chunk_id": "doc-a:r1:000001", "page": 2, "quote": "The Disputes Team reports to Retail Operations."}
    assert gb.find_evidence("Nothing", "Here", FILES["card-policy-aaaaaaaa.md"]["chunks"]) is None


def test_save_carries_reviews_and_human_types_across_rebuilds(db):
    entities, links = gb.parse_graph(GRAPH, FILES)
    types, chosen, _ = gb.align(entities, links, 10, call=fake_model)
    first = GraphBuild(id="b1", folder_id="f1", started_by="E002", document_ids=["doc-a"])
    db.add(first)
    counts = gb.save_graph(db, first, entities, types, links, chosen, FILES)
    db.commit()
    assert counts["links"] == 3 and counts["nodes"] == 5 and counts["reviews_carried"] == 0
    assert {l.status for l in db.scalars(select(GraphLink))} == {"proposed"}
    applies = db.scalar(select(GraphLink).where(GraphLink.predicate == "applies_to"))
    applies.status, applies.reviewed_by = "approved", "E002"
    team = db.scalar(select(GraphNode).where(GraphNode.key == "disputes team"))
    team.node_type, team.type_source = "Process", "human"
    db.commit()

    second = GraphBuild(id="b2", folder_id="f1", started_by="E002", document_ids=["doc-a"])
    db.add(second)
    counts = gb.save_graph(db, second, entities, types, links, chosen, FILES)
    db.commit()
    assert counts["reviews_carried"] == 1
    assert db.get(GraphLink, applies.id).status == "approved" and db.get(GraphLink, applies.id).reviewed_by == "E002"
    assert db.scalar(select(GraphNode).where(GraphNode.key == "disputes team")).node_type == "Process"
    evidence = db.scalars(select(GraphEvidence).where(GraphEvidence.link_id == applies.id)).all()
    assert [(e.chunk_id, e.page) for e in evidence] == [("doc-a:r1:000000", 1)]
    assert db.query(GraphLink).count() == 3  # replaced, not duplicated


def test_a_changed_predicate_reopens_an_approval_but_keeps_a_rejection(db):
    entities, links = gb.parse_graph(GRAPH, FILES)
    types, chosen, _ = gb.align(entities, links, 10, call=fake_model)
    first = GraphBuild(id="b1", folder_id="f1", started_by="E002", document_ids=["doc-a"])
    db.add(first)
    gb.save_graph(db, first, entities, types, links, chosen, FILES)
    db.commit()
    applies = db.scalar(select(GraphLink).where(GraphLink.predicate == "applies_to"))
    part = db.scalar(select(GraphLink).where(GraphLink.predicate == "part_of"))
    applies.status, applies.reviewed_by = "approved", "E002"
    part.status, part.reviewed_by = "rejected", "E002"
    db.commit()

    renamed = {i: ("governed_by" if p == "applies_to" else "owned_by" if p == "part_of" else p) for i, p in chosen.items()}
    second = GraphBuild(id="b2", folder_id="f1", started_by="E002", document_ids=["doc-a"])
    db.add(second)
    counts = gb.save_graph(db, second, entities, types, links, renamed, FILES)
    db.commit()
    governed = db.scalar(select(GraphLink).where(GraphLink.predicate == "governed_by"))
    owned = db.scalar(select(GraphLink).where(GraphLink.predicate == "owned_by"))
    assert governed.status == "proposed" and "Was approved as 'applies_to'" in governed.review_note
    assert owned.status == "rejected" and "Rejected earlier as 'part_of'" in owned.review_note
    assert counts["reviews_reopened"] == 1 and counts["reviews_carried"] == 1


def test_only_one_build_at_a_time(db):
    db.add(GraphBuild(id="running", folder_id="f1", status="running", started_by="E002"))
    db.commit()
    with pytest.raises(gb.BuildError, match="already running"):
        gb.start_build(db, "f1", ["doc-a"], "E002")


# ----------------------------------------------------------------------------------- GLiNER extraction

GLINER_RESULTS = {
    "doc-a:r1:000000": {"entities": [
        {"type": "policy", "text": "Card Dispute Policy", "confidence": 0.97},
        {"type": "product", "text": "Platinum Card", "confidence": 0.91},
        {"type": "metric", "text": "30", "confidence": 0.9},          # no letters: a value, not an entity
        {"type": "process", "text": "Disputes", "confidence": 0.2}],  # below the entity threshold
        "relations": [
        {"type": "applies_to", "head": "Card Dispute Policy", "head_type": "policy", "tail": "Platinum Card",
         "tail_type": "product", "confidence": 0.93},
        {"type": "applies_to", "head": "Card Dispute Policy", "head_type": "policy", "tail": "Card Dispute Policy",
         "tail_type": "policy", "confidence": 0.9},                   # a link to itself
        {"type": "owned_by", "head": "Platinum Card", "head_type": "product", "tail": "Disputes Team",
         "tail_type": "organisation", "confidence": 0.1}]},           # below the link threshold
    "doc-a:r1:000001": {"entities": [
        {"type": "organisation", "text": "Disputes Team", "confidence": 0.95},
        {"type": "organisation", "text": "Retail Operations", "confidence": 0.99}],
        "relations": [
        {"type": "part_of", "head": "Disputes Team", "head_type": "organisation", "tail": "Retail Operations",
         "tail_type": "organisation", "confidence": 0.9},
        {"type": "located_in", "head": "Disputes Team", "head_type": "organisation", "tail": "Retail Operations",
         "tail_type": "location", "confidence": 0.95}]},              # voted an Organisation, so not a place
}


def test_gliner_schema_stays_inside_the_ontology():
    from kb import gliner_extract as gx
    schema = gx.schema()
    labels = set(schema["entities"])
    assert {gx.TYPES[t] for t in gx.TYPES} == labels and set(gx.TYPES) <= set(node_types())
    assert set(schema["relations"]) <= set(predicates())
    assert not {"related_to", "mentions"} & set(schema["relations"])  # only specific, typed links
    for relation in schema["relations"].values():
        assert set(relation["head"]) <= labels and set(relation["tail"]) <= labels and relation["description"]


def test_gliner_results_become_typed_links_with_their_own_chunk_as_evidence(db):
    from kb import gliner_extract as gx
    entities, types, links, chosen = gx.to_graph(FILES, GLINER_RESULTS)
    assert types == {"card dispute policy": "Policy", "platinum card": "Product",
                     "disputes team": "Organisation", "retail operations": "Organisation"}
    assert {(l["source"], chosen[i], l["target"]) for i, l in enumerate(links)} == {
        ("card dispute policy", "applies_to", "platinum card"), ("disputes team", "part_of", "retail operations")}
    build = GraphBuild(id="g1", folder_id="f1", started_by="E002", document_ids=["doc-a"])
    db.add(build)
    counts = gb.save_graph(db, build, entities, types, links, chosen, FILES)
    db.commit()
    assert counts["links"] == 2 and counts["predicates"] == {"applies_to": 1, "part_of": 1}
    part_of = db.scalar(select(GraphLink).where(GraphLink.predicate == "part_of"))
    evidence = db.scalars(select(GraphEvidence).where(GraphEvidence.link_id == part_of.id)).all()
    assert [(e.chunk_id, e.quote) for e in evidence] == [("doc-a:r1:000001", "The Disputes Team reports to Retail Operations.")]
    assert {l.status for l in db.scalars(select(GraphLink))} == {"proposed"}  # review still decides


def test_gliner_extract_reads_only_chunks_missing_from_the_cache(tmp_path, monkeypatch):
    from kb import gliner_extract as gx
    seen = []

    def fake_worker(chunks, work, schema_text, timeout, progress):
        seen.append([c["id"] for c in chunks])
        (work / "gliner-results.jsonl").write_text("".join(
            '{"id": "%s", "entities": [], "relations": []}\n' % c["id"] for c in chunks), encoding="utf-8")

    monkeypatch.setattr(gx, "_run_worker", fake_worker)
    chunks = [{"id": "a", "text": "Alpha Bank"}, {"id": "b", "text": "Beta Bank"}]
    assert set(gx.extract(chunks, tmp_path, 60)) == {"a", "b"}
    assert set(gx.extract(chunks + [{"id": "c", "text": "Gamma Bank"}], tmp_path, 60)) == {"a", "b", "c"}
    assert seen == [["a", "b"], ["c"]]  # the second build only read the new chunk
