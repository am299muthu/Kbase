# Document parsers

> **2026-10-27 update:** Scanned/sparse/raster-dominant PDF pages use the configured VL model over overlapping locally rendered regions; Tesseract is not a prerequisite for this path. Model-read text is generated/unverified, with page/bbox/hash lineage. Missing/disabled VL refuses scan placeholders. Whole-page content may be covered collectively and requires explicit checker/image-transfer review. Electronic text remains pypdf, ruled grid cells pdfplumber, and object detection Docling. See [current governed rollout](governance-and-activation.md).

Python 3.11+; local/offline extraction only. No model downloads, shell commands,
recursive archive unpacking, or external email/image fetches are performed.
Configuration is read from environment variables; the application's `.env` loader
must run before parsing.

```python
from pathlib import Path
from kb.parsers import parse_document, chunk_document

parsed = parse_document(Path("policy.docx"), Path("var/assets"))
chunks = chunk_document(parsed, max_chars=1800, overlap=200)
```

`ParsedBlock(text, section='Content', page=None, kind='text', asset_path=None, metadata={})`
and `ParsedDocument(blocks, assets, parser)` are dataclasses.
`SUPPORTED_EXTENSIONS` is an exported set of lowercase extensions with leading
dots, suitable for an upload allowlist (optional dependencies still apply).
Each chunk has
`text`, `section`, `page` (first cited page or null), `pages` (all intersecting
source pages), and `assets` (asset dictionaries). Asset dictionaries include
`path`, `sha256`, `mime_type`, `page`, `section`, and optional extraction metadata.
Paths are absolute local paths, not public URLs; the ingestion layer must copy
assets into governed object storage and enforce the parent document's ACL.

## Formats and dependencies

| Input | Dependency | Behavior / limitations |
| --- | --- | --- |
| PDF (default) | `pypdf`; `Pillow` for extracted-image conversion | Text with 1-based page citations; embedded images saved where supported. PDF reading order, tables, vector charts, and complex typography are not reconstructed reliably. Image extraction failures on readable pages issue warnings. Drawing-only pages without extractable text/images fail explicitly. |
| PDF (opt-in) | `docling`, preprovisioned model artifacts | Set `PDF_PARSER=docling` and `DOCLING_ARTIFACTS_PATH` to a local model directory. Layout/table parsing and picture export; no silent fallback to pypdf. Missing/incompatible artifacts fail, not auto-download. Remote services and picture-description enrichment are disabled. |
| PDF (hybrid; current local configuration) | `docling`, `pdfplumber`, `pypdf`, preprovisioned local artifacts | `PDF_PARSER=docling_hybrid`: pypdf preserves text, pdfplumber extracts searchable ruled-grid cells, Docling detects/crops visual objects. Bounding boxes/page/object metadata are retained. Optional separately gated OpenRouter VL and image embeddings run downstream, not inside the local parser. See [multimodal pipeline](pdf-multimodal-pipeline.md). |
| PNG/JPEG/TIFF/BMP/WebP; scanned PDF images | `Pillow`, `pytesseract`, system Tesseract and local language packs | Set `OCR_ENABLED=true`; `OCR_LANGUAGE=eng` and `OCR_TIMEOUT_SECONDS=60` by default. OCR is text recognition, **not chart interpretation**. Scanned pages with no recognized text fail. pypdf OCRs extracted images rather than rendering/compositing a whole page, so rotated/fragmented scans may need provisioned Docling or explicit offline conversion. Multi-frame images are read per frame. |
| DOCX | `python-docx` | Body paragraphs/headings, tables including nested tables, headers/footers, and embedded image references. No reliable page numbers (`page=null`); no Word pagination engine. Merged cells can repeat content. Tracked changes, text boxes, footnotes, SmartArt, linked images and arbitrary OLE objects are not fully extracted. Macros are never executed. |
| DOC | Above plus local LibreOffice (`libreoffice`/`soffice`) or macOS `textutil` | Converts in a temporary directory to DOCX; argument-list subprocess, no shell, no source-name options, timeout and output checks. LibreOffice uses a private profile. Conversion fidelity depends on installed converter. Treat native converters as untrusted-document workers and isolate them at OS/container level; this module is not a sandbox. |
| XML | `defusedxml` | Streaming SAX extraction of text, attributes and element-path sections. DTDs/entities/external references forbidden. No XML stylesheet or schema fetch. |
| EML | Python standard library | Headers and plain/HTML bodies (script/style excluded); inline images and attachments retained as referenced assets. Alternative bodies may repeat text. Attachments are **not recursively indexed**. Remote resources are never fetched. |
| MSG | optional `extract-msg` | Headers, body, byte-valued attachments. Embedded MSG/OLE attachment objects require explicit conversion to EML/offline extraction and fail clearly. |
| XLS | `xlrd` | All sheets, row-labelled cell values; formulas use the stored result supplied by xlrd. |
| XLSX/XLSM | `openpyxl` | Read-only row streaming, all sheets, formula text retained; no formula evaluation, macros, chart interpretation or spreadsheet-image extraction. Workbook/sheet names provide citations; not printed page numbers. |
| CSV | Python standard library | Streaming rows; delimiter sniffing with comma fallback; standard-library CSV field-size limit applies. |
| TXT/LOG/MD/Markdown/TSV | Python standard library | Bounded reads; UTF-8/BOM, BOM-marked UTF-16, or strict Windows-1252 fallback. TSV remains readable plain text. No Markdown execution. |
| Parquet | optional `pyarrow` | Footer/schema and row-group/row-count metadata only. **Never reads/indexes row values.** |
| `.bz2`, `.gz`, `.xz`, `.zip`, `.7z`, `.tar` | unsupported by design | Raises `UnsupportedFormat` with instructions for a separately approved offline streaming decompression/splitting job. Large Wikipedia dumps are never decompressed automatically. |

The local parser marks visual descriptions pending. The ingestion pipeline now
has an opt-in OpenRouter VL stage for hybrid PDF object crops; descriptions are
explicitly labelled generated and retain model/source provenance. Real crop
transfer remains disabled pending its separate permission boundary; a live
synthetic VL probe passed, but a later full live VL ingestion check was rate-limited
(HTTP 429). No chart-understanding accuracy or completed corpus reprocessing is
claimed. See [configuration and verification](pdf-multimodal-pipeline.md).

## Local Docling and Granite (`docling_local`, Windows demo default)

`scripts/windows/env.ps1` sets `PDF_PARSER=docling_local` and `IMAGE_PARSER=docling_local` when
the local Docling install exists at `App/docling/.venv`. KBase's own Python has no Docling, torch
or transformers, so `kb.parsers._parse_docling_local` runs `scripts/docling/worker.py` with that
install's Python (`DOCLING_PYTHON`) as a separate process for each PDF or image.

- **Docling:** reading-order layout, headings and sections, TableFormer tables as Markdown, picture
  crops, and RapidOCR for scanned pages, bitmap regions and uploaded images (no Tesseract needed).
- **Picture classifier:** Docling's DocumentFigureClassifier labels each picture. An uploaded image
  that is itself a chart is classified as a whole.
- **Merged chart regions:** the layout model often draws one picture around several charts (a row
  of small panels, two stacked charts). The worker cuts such a region at full-width divider lines,
  or else at white gutters, classifies each piece, folds nearby label pieces (legends, axis years)
  into the chart beside them, and keeps the cut only when at least two pieces are charts.
- **Missed vector charts:** charts drawn as a few vector paths (a donut) can be missed by the layout
  model. The worker clusters the PDF's own path objects, skips anything already inside a picture or
  table, and classifies the rest; charts among them become pictures.
- **Pictures classified as a table** are also read by Granite (a panel of small charts, or a table
  drawn as an image).
- **Granite Vision 4.1 4B:** reads bar, line, pie and stacked bar charts into a data table, one
  chart at a time with a time limit each. It runs on the GPU in 4-bit (about 2.6 GB of graphics
  memory, 3 to 9 s per chart on the RTX 4060), otherwise on the CPU. Each table is indexed as a
  `vision_description` block next to its picture, marked `generated` with the model name and the
  text "a model reading, check it against the chart". Scatter plots and other pictures stay as
  pictures with their caption. When Granite cannot finish a table in time (a line of daily prices)
  it writes a short summary instead (`<chart2summary>`, `GRANITE_SUMMARY_SECONDS`, default 60).
- **Number check:** every Granite table is compared with the numbers printed in the PDF text inside
  the chart's region: matches (80% or more found), partial, not found, or not checked (no text
  layer). The result is in the indexed text and the object inspector. It catches invented numbers,
  not a right number placed in the wrong row or column.
- Every block keeps its page and top-left bounding box, and every layout object goes to the PDF
  object inspector.
- Models load only from the local Hugging Face cache (`HF_HUB_OFFLINE=1`); nothing is downloaded
  and nothing leaves the machine.
- Progress shows in the Ingestion monitor during `parsing` (Granite's "chart n of m").
- A failure names the step and exception type; the worker's log tail goes to the server log only.

The time limits (Granite chart table, Granite chart summary, Docling whole document, Tesseract OCR,
Word .doc conversion) can be changed in the app on the **Processing settings** page by employees
listed in `KB_DEVELOPERS`. A saved value wins over the environment variable, which wins over the
default. Changes apply from the next document that starts processing, are written to the activity
log, and do not void approvals; each run records the limits it used in its extraction evidence.

| Setting | Default | Meaning |
|---|---|---|
| `DOCLING_PYTHON` | set by `env.ps1` | python.exe of the local Docling install |
| `DOCLING_OCR` | `true` | OCR bitmap regions and scanned pages |
| `GRANITE_CHARTS` | `true` | read charts into tables |
| `GRANITE_MAX_CHARTS` | `25` | charts read per document; the rest stay pictures, noted in the evidence |
| `GRANITE_SECONDS_PER_CHART` | `90` | time limit per chart |
| `DOCLING_TIMEOUT_SECONDS` | `3600` | whole-document limit |

Measured on the synthetic 4-page `sample_report.pdf` through the live maker-checker flow: 93 s
from approval to indexed, 6 of 7 charts read (the scatter plot is kept as a picture), 22 chunks.
Each document loads the models again (about 30 s); a long-lived worker would remove that.

## Chunking and provenance

Consecutive blocks with the same section and visual-object identity are grouped. Headings start new
sections, not page boundaries; paragraphs/sentences/whitespace are preferred
window boundaries. Giant paragraphs and unbroken strings use bounded hard
windows, so no block text is dropped. Every character of every nonempty source
block appears in at least one chunk. Separators are inserted between blocks;
chunk overlap never crosses a section. `max_chars` must be positive and
`0 <= overlap < max_chars`.

Every intersecting source block contributes page/asset provenance, including
when a chunk crosses a PDF page. Docling items spanning several pages are
represented once per cited page because the block contract has one page field;
this can duplicate their text but does not discard later-page citations. Word,
email, text, XML, spreadsheets and Parquet have null page citations; their
sections/rows supply the meaningful source locator. Headings remain in text.

## Resource limits and failure behavior

Defaults are deliberately bounded for an ingestion worker, not terabyte files:

| Environment variable | Default |
| --- | ---: |
| `PARSER_MAX_FILE_BYTES` | 134217728 (128 MiB) |
| `PARSER_MAX_TEXT_CHARS` | 20000000 |
| `PARSER_MAX_BLOCKS` | 200000 |
| `PARSER_MAX_CHUNKS` | 100000 |
| `PARSER_MAX_PAGES` | 10000 |
| `PARSER_MAX_PDF_OBJECTS` | 100 (hybrid PDF objects/VL call bound) |
| `PARSER_MAX_CELLS` | 1000000 |
| `PARSER_MAX_ASSETS` | 10000 |
| `PARSER_MAX_ASSET_BYTES` | 20971520 (per asset) |
| `PARSER_MAX_TOTAL_ASSET_BYTES` | 268435456 (per document, references counted) |
| `PARSER_MAX_IMAGE_PIXELS` | 40000000 |
| `PARSER_MAX_ARCHIVE_BYTES` | 268435456 (expanded Office container) |
| `PARSER_MAX_ARCHIVE_ENTRIES` | 20000 |
| `PARSER_MAX_XML_DEPTH` | 128 |
| `DOC_CONVERSION_TIMEOUT_SECONDS` | 120 |

Limits raise `ParserLimitError`; unsupported formats raise `UnsupportedFormat`;
missing optional dependencies raise `MissingDependency`. All inherit
`ParseError`. Empty/encrypted/unreadable/failed extraction never returns an
empty successful document. Format-library errors are wrapped without copying
source content into the public error message. No partial document is returned.

Office ZIPs are inspected before decompression; XML/text/CSV/spreadsheets stream
where practical. Some libraries (PDF/Word/email/XLS/Docling) hold significant
state in memory despite file/text limits. Deploy workers with memory/CPU/time
quotas, restricted filesystem/network access and patched libraries. Parser
warnings must be captured in ingestion audit logs. OCR/conversion errors should
be quarantined for review rather than silently skipped.

Assets are SHA-256 content-addressed files in the created `asset_dir`; source
filenames are never used as destination paths. Failed jobs can leave orphaned
assets: the ingestion owner must garbage-collect unreferenced assets and keep
raw originals for repeatable extraction. Share assets only within a governed
security boundary; content addressing is not an entitlement mechanism. Treat
both parsed text and attachments as untrusted data, never instructions/code.
