"""The docling_local bridge: worker output becomes ordered, cited blocks and layout objects."""
from pathlib import Path

import pytest

from kb import parsers
from kb.parsers import MissingDependency, chunk_document, parse_document
from kb.reading_order import order_pdf_blocks

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
       b"\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")


def fake_result(out: Path) -> dict:
    (out / "pictures").mkdir(parents=True, exist_ok=True)
    (out / "pictures" / "0.png").write_bytes(PNG)
    box = lambda t: [{"page": 1, "l": 72.0, "t": t, "r": 540.0, "b": t + 20}]  # noqa: E731
    return {
        "version": 1, "device": "cpu", "ocr": True, "notes": [],
        "timings": {"docling_seconds": 1.0, "granite_seconds": 1.0},
        "granite": {"charts": 1, "read": 1, "device": "cpu", "seconds": 1.0},
        "pages": {"1": {"width": 612.0, "height": 792.0}},
        "items": [
            {"label": "section_header", "ref": "#/texts/0", "section": "1. Revenue", "text": "1. Revenue", "boxes": box(300)},
            {"label": "picture", "ref": "#/pictures/0", "section": "1. Revenue", "text": "", "boxes": box(400),
             "picture": {"file": "pictures/0.png", "class": "bar_chart", "confidence": 0.99, "caption": "Figure 1: Revenue.",
                         "chart": {"rows": [["Quarter", "Revenue"], ["Q1", "8.2"], ["Q2", "9.1"]], "seconds": 2.0}}},
            {"label": "text", "ref": "#/texts/1", "section": "1. Revenue", "text": "Revenue grew every quarter.", "boxes": box(100)},
            {"label": "table", "ref": "#/tables/0", "section": "1. Revenue", "text": "| a | b |\n|---|---|\n| 1 | 2 |",
             "boxes": box(500) + [{"page": 1, "l": 72.0, "t": 530, "r": 540.0, "b": 560}]},
        ],
    }


@pytest.fixture
def docling_local(monkeypatch):
    monkeypatch.setenv("PDF_PARSER", "docling_local")
    monkeypatch.setenv("IMAGE_PARSER", "docling_local")
    monkeypatch.setattr(parsers, "_run_docling_worker", lambda path, out: fake_result(out))


def test_worker_output_becomes_blocks_assets_and_layout(tmp_path, docling_local):
    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF-1.4 placeholder")
    parsed = order_pdf_blocks(parse_document(source, tmp_path / "work" / "assets"))
    assert parsed.parser == "docling-local"
    kinds = [b.kind for b in parsed.blocks]
    # page-y-x order: the paragraph at y=100 comes before the heading at y=300
    assert kinds == ["text", "heading", "image", "vision_description", "table"]
    chart = parsed.blocks[3]
    assert chart.metadata["generated"] is True and chart.metadata["model"] == "granite-vision-4.1-4b"
    assert "| Q2 | 9.1 |" in chart.text and chart.asset_path == parsed.blocks[2].asset_path
    assert parsed.assets[0]["object_kind"] == "bar_chart" and parsed.assets[0]["bbox"]["origin"] == "top-left"
    # two boxes for the table on one page: one block, two layout objects
    assert sum(o["object_kind"] == "table" for o in parsed.layout_objects) == 2
    assert parsed.extraction_evidence["docling"]["granite"]["read"] == 1
    assert not list((tmp_path / "work").glob("docling-*")), "the worker folder is removed"
    assert chunk_document(parsed)


def test_images_route_to_docling_local(tmp_path, docling_local):
    source = tmp_path / "chart.png"
    source.write_bytes(PNG)
    assert parse_document(source, tmp_path / "assets").parser == "docling-local"


def test_missing_docling_python_is_a_clear_dependency_error(tmp_path, monkeypatch):
    monkeypatch.setenv("PDF_PARSER", "docling_local")
    monkeypatch.setenv("DOCLING_PYTHON", str(tmp_path / "missing" / "python.exe"))
    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF-1.4 placeholder")
    with pytest.raises(MissingDependency, match="DOCLING_PYTHON"):
        parse_document(source, tmp_path / "assets")


def test_extracted_tables_and_charts_come_back_as_rows_for_the_inspector(tmp_path, docling_local):
    from dataclasses import asdict
    from kb.pdf_debug import extracted_data

    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF-1.4 placeholder")
    parsed = asdict(parse_document(source, tmp_path / "work" / "assets"))
    items = {item["kind"]: item for item in extracted_data(parsed)}
    assert set(items) == {"chart", "table"}
    chart = items["chart"]
    assert chart["object_id"] == "docling:#/pictures/0:1" and chart["page"] == 1 and chart["generated"] is True
    assert chart["source"] == "granite-vision-4.1-4b" and chart["seconds"] == 2.0
    assert chart["rows"] == [["Quarter", "Revenue"], ["Q1", "8.2"], ["Q2", "9.1"]] and chart["text"] == ""
    assert chart["caption"] == "Figure 1: Revenue."
    table = items["table"]
    assert table["object_id"] == "docling:#/tables/0:1" and table["generated"] is False and table["source"] == "docling"
    assert table["rows"] == [["a", "b"], ["1", "2"]]


def test_extracted_data_keeps_table_captions_and_non_table_descriptions():
    from kb.pdf_debug import extracted_data

    parsed = {"blocks": [
        {"text": "plain paragraph", "page": 1, "kind": "text", "metadata": {"object_id": "docling:#/texts/0:1"}},
        {"text": "Table 1: Depot performance.\n\n| Depot | On-time % |\n|-------|----------:|\n| North | 96 |", "page": 1,
         "kind": "table", "metadata": {"object_id": "docling:#/tables/0:1", "extractor": "docling"}},
        {"text": "no rows here", "page": 1, "kind": "table", "metadata": {"object_id": "docling:#/tables/1:1"}},
        {"text": "[Generated visual interpretation]\nA logo.", "page": 2, "kind": "vision_description",
         "metadata": {"object_id": "x:2", "generated": True, "model": "vl"}}]}
    table, description = extracted_data(parsed)
    assert table["rows"] == [["Depot", "On-time %"], ["North", "96"]] and table["caption"] == "Table 1: Depot performance."
    assert description["kind"] == "chart" and description["rows"] == []
    assert description["text"] == "A logo."  # the marker line is dropped; the inspector labels the reading itself


def test_chart_check_and_summary_reach_the_index_and_the_inspector(tmp_path, monkeypatch):
    from dataclasses import asdict
    from kb.pdf_debug import extracted_data

    def result(path, out):
        (out / "pictures").mkdir(parents=True, exist_ok=True)
        for name in ("0.png", "0-part1.png", "1.png"):
            (out / "pictures" / name).write_bytes(PNG)
        box = lambda t: [{"page": 1, "l": 72.0, "t": t, "r": 540.0, "b": t + 60}]  # noqa: E731
        return {"pages": {"1": {"width": 612.0, "height": 792.0}}, "items": [
            {"label": "picture", "ref": "#/pictures/0", "section": "S", "text": "", "boxes": box(100),
             "picture": {"file": "pictures/0.png", "class": "table", "split_into": 2, "chart": None, "caption": ""}},
            {"label": "picture", "ref": "#/pictures/0/part1", "section": "S", "text": "", "boxes": box(100),
             "picture": {"file": "pictures/0-part1.png", "class": "bar_chart", "part_of": "#/pictures/0", "caption": "",
                         "chart": {"rows": [["Year", "Value"], ["2024", "4.7"]], "seconds": 3.0, "mode": "table",
                                   "check": {"status": "not-found", "matched": 0, "total": 1}}}},
            {"label": "picture", "ref": "#/pictures/1", "section": "S", "text": "", "boxes": box(300),
             "picture": {"file": "pictures/1.png", "class": "line_chart", "caption": "",
                         "chart": {"rows": None, "summary": "Rose from 11 to 19.", "seconds": 95.0, "mode": "summary",
                                   "note": "Summary instead of a table: the table was not finished in 90 s"}}},
        ]}

    monkeypatch.setenv("PDF_PARSER", "docling_local")
    monkeypatch.setattr(parsers, "_run_docling_worker", result)
    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF-1.4 placeholder")
    parsed = parse_document(source, tmp_path / "work" / "assets")
    texts = [b.text for b in parsed.blocks]
    assert texts[0] == "[Picture: table, holding 2 charts read separately]"
    assert texts[2].startswith("[Chart data read by Granite Vision; its numbers are not in the PDF text")
    assert texts[4] == "[Chart summary written by Granite Vision; a model reading, check it against the chart]\nRose from 11 to 19."
    data = extracted_data(asdict(parsed))
    assert [d["mode"] for d in data] == ["table", "summary"]
    assert data[0]["check"] == {"status": "not-found", "matched": 0, "total": 1} and data[0]["caption"] == ""
    assert data[1]["text"] == "Rose from 11 to 19." and data[1]["note"].startswith("Summary instead of a table")


def test_chart_markdown_escapes_pipes():
    assert parsers._chart_markdown([["a|b", "c"], ["1", "2\n3"]]).splitlines() == ["| a/b | c |", "|---|---|", "| 1 | 2 3 |"]
