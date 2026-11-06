> Scope update, 2026-11-06: PII detection/redaction and PII visibility gates have been removed from the active pipeline. References to those checks below describe historical runs. Folder authorization and evidence grounding remain active.

# Hybrid PDF and multimodal retrieval pipeline

> **2026-10-27:** Source/scope-bound maker–checker governance, inference PII/grounding guards, VL reading of scanned-page regions, native local S3, durable profiles and the compatible target index are now implemented. Three pilot target-profile requests await checker/direct transfer review; no real PDF enrichment or live hybrid corpus is claimed. See [current rollout and executed checks](governance-and-activation.md). The earlier verification below remains historical.

## Extraction responsibilities

`PDF_PARSER=docling_hybrid` selects a deliberate split, not a parser fallback:

1. **pypdf** extracts electronic full-page text with page citations. All extracted text is retained; text inside captions/tables is not discarded by geometric guesses.
2. **pdfplumber** finds ruled/grid tables on searchable pages using `vertical_strategy=lines` and `horizontal_strategy=lines`. Exact cell rows are serialized as JSON rows, preserving null/empty/multiline values. This is not OCR or model-generated transcription. Borderless tables and scanned grids are not this extractor's target.
3. **Docling** identifies table/picture objects and supplies normalized top-left bounding boxes, page provenance and cropped images. Its structured table extraction handles non-grid tables. A Docling table substantially overlapping an already-extracted pdfplumber grid is not duplicated as a second visual table. Native page text remains retained for coverage.
4. **Optional OpenRouter VL enrichment** interprets individual object crops. Generated descriptions, model-read text and uncertainties are explicitly labelled, with model ID and source-image hash. They supplement, never replace, extracted grid values and source text.

Docling renders page images **locally** to obtain object crops. The vision adapter submits only crop bytes, never the original PDF or direct whole-page renders. Before either VL or image-embedding upload, bounding boxes must be valid and cover no more than `VISION_MAX_PAGE_FRACTION=0.8` of the page; whole-page/oversized detections fail closed. Crop-only and page-coverage limits still cannot guarantee absence of sensitive pixels. OCR is separate and remains disabled until local Tesseract/language packs are provisioned. Where pypdf finds no text, locally available Docling text can supplement it, identified by its extractor.

`parse_document` remains local-only. External enrichment is a separate `kb/vision.py` stage invoked by `kb/pipeline.py` after PDF parsing and before raw-parsed/curated persistence. Requested enrichment fails closed on unavailable models or malformed responses; it does not fabricate or silently substitute descriptions.

## Separate models and vector spaces

All model IDs are read through settings from `.env`/environment, not hard-coded in adapters:

| Purpose | Model variable | Dimension variable | OpenSearch field |
|---|---|---|---|
| Text embeddings | `OPENROUTER_EMBEDDING_MODEL` | `EMBEDDING_DIMENSIONS` | `vector` |
| Image embeddings | `OPENROUTER_IMAGE_EMBEDDING_MODEL` | `IMAGE_EMBEDDING_DIMENSIONS` | `image_vector` |
| Text-answer inference | `OPENROUTER_MODEL` | — | — |
| Visual-object inference | `OPENROUTER_VISION_MODEL` | — | — |

Image retrieval embeds the **query text with the image embedding model**, then searches only `image_vector`. It never compares a text-model query vector against image-model vectors, even when their dimensions happen to match. The image model must support both image input and text-query input in one cross-modal space. No image-to-text-model substitution exists.

When enabled, retrieval runs three independent ACL/retention-filtered streams: BM25, text-model k-NN and image-model k-NN. Application-side reciprocal-rank fusion combines ranks, then SQL manifests recheck current grants/status/retention/content integrity. Each visual object has its own chunk grouping and crop reference. Raw and curated metadata retain page/bbox/object/extractor information; generated visual content is distinguishable from extraction.

The mapping includes text/image model identifiers in `_meta`. Mapping type, Lucene engine, dimensions and model identity are checked before vector indexing/search; mismatches fail with an instruction to create a fresh index. Old prepared vector indexes without this metadata are not silently accepted. No automatic SQLite/BM25 fallback is introduced.

## Installed local prerequisites

Installed with explicit source authorization from PyPI:

- Docling **2.142.1** / docling-core **2.104.0** / docling-ibm-models **4.0.4**.
- pdfplumber **0.11.10**.
- PyTorch **2.14.1** and resolved dependencies; `pip check` passed.

Provisioned only layout/TableFormer model families from official `docling-project` Hugging Face repositories using:

```sh
.venv/bin/docling-tools models download layout tableformer --output-dir /NEW/EMPTY/LOCAL/MODEL/DIRECTORY
```

This host's directory is `var/docling-models.yHcHEi`. Repositories resolved during download:

- `docling-project/docling-layout-heron`: `8f39ad3c0b4c58e9c2d2c84a38465abf757272d8`.
- `docling-project/docling-layout-heron-onnx`: `40bde044036bb181c130ddf6c51792187268748f`.
- `docling-project/docling-models` tag `v2.3.0`: `fc0f2d45e2218ea24bce5045f58a389aed16dc23`.

Docling receives the explicit local artifact path, with remote services, picture classification/description and code/formula enrichment disabled. Offline Hub/Transformers defaults are set when configuring Docling. A synthetic parse also passed with socket connections blocked. This is not an OS-level parser sandbox or a universal guarantee for arbitrary future Docling configurations; deploy with network/filesystem/resource isolation for untrusted sources.

## Configuration and transfer gates

Local `.env` now selects:

```dotenv
PDF_PARSER=docling_hybrid
DOCLING_ARTIFACTS_PATH=/Users/MutharasanAnbarasan/PROJECTS/Kbase/var/docling-models.yHcHEi
PARSER_MAX_PDF_OBJECTS=100
```

The separately approved image candidate was verified with synthetic images and text queries, then saved as explicit configuration:

```dotenv
OPENROUTER_IMAGE_EMBEDDING_MODEL=nvidia/llama-nemotron-embed-vl-1b-v2:free
IMAGE_EMBEDDING_DIMENSIONS=2048
```

Both the configured text embedding model and this separate image model returned 2048-dimensional vectors; **equal size does not mean compatible spaces**. `OPENROUTER_VISION_MODEL` is explicitly configured in `.env`; the existing text inference and embedding selections were preserved.

The following controls remain disabled for real content:

```dotenv
EXTERNAL_MODELS_ENABLED=false
PDF_VISION_ENABLED=false
ALLOW_EXTERNAL_VISION_IMAGES=false
IMAGE_EMBEDDINGS_ENABLED=false
OPENSEARCH_VECTOR_SEARCH=false
```

Image pixels cannot be protected by text-only PII masking. Real crop transfer needs explicit image-transfer approval, in addition to the global external-model gate. Image encoding is normalized to PNG, bounded by `VISION_MAX_IMAGE_BYTES=5242880` and `VISION_MAX_IMAGE_PIXELS=4000000`. These controls are separate from `ALLOW_UNMASKED_EXTERNAL`, which remains false for text. No raw image payload is stored in OpenSearch or SQL; references and hashes are stored instead.

The previous real-content reindex remains blocked by Auto permission mode. This task did not retry it through image crops, inference, another process or another service. A fresh multimodal index and specifically authorized service-role scope are needed before enabling image vectors. The earlier empty text-only prepared index cannot be treated as a populated compatible multimodal index. After transfer approval, activate the flags only alongside validated mappings, model metadata and actual corpus population.

**Existing indexed PDFs have not been reparsed.** `reindex` uses old curated chunks; it cannot discover Docling objects or grid tables. Existing sources need a separately scoped, approved reprocessing/versioning operation before the new extraction can affect their retrieval.

## Executed verification and limits

- Generated a one-page synthetic electronic PDF with text, a ruled table and an embedded shape image. Local parsing produced **3 blocks, 2 object crops and 3 chunks** with correct table values, Docling picture detection, page/bbox/object metadata, and no network calls.
- Live synthetic OpenRouter image/text-query embedding probes passed for the separate image model at **2048 dimensions**. A small synthetic VL crop also produced a valid structured description.
- A subsequent isolated ingestion run with actual VL calls **failed with HTTP 429**. This is recorded as a free-tier availability/quota failure, not a successful full live-model pipeline. The configured model was not replaced and no paid fallback was used.
- With explicit user scope authorization, ran the actual ingestion pipeline against `kbase-pdf-multimodal-smoke-20261026`, an isolated control DB/object store, mocked VL output and deliberately different deterministic **8D text / 12D image** test vectors. Verified live OpenSearch indexing, all three query streams/RRF, access denials, SQL metadata-only manifests, durable crop references, and rejection of wrong model metadata. Only that exact synthetic index was deleted afterward. No real corpus, staff grant or KB index was changed.
- Additional regressions passed for chart-labelled objects, preventing a tiny grid from suppressing a larger Docling table, and rejecting whole-page regions before either image-transfer path. The updated local parser still produced the expected 3 blocks / 2 crops / 3 chunks without network access.
- The synthetic deterministic-vector test proves mechanics/isolation, **not semantic accuracy** or table/chart interpretation quality. No full-corpus reprocessing, production PII benchmark or 50-case semantic-quality evaluation is claimed.
- Final Python compilation and `pip check` passed. Frontend syntax checking could not run: the installed Node executable aborts because `libsimdjson.33.dylib` is missing. No JavaScript was changed in this PDF task, and no system/Homebrew repair was attempted.
- Restarted one app instance to load the local parser changes, with process overrides preserving `LLM_PROVIDER=extractive`, BM25 and all external/image-processing gates disabled. Private `.env` provider selections were not overwritten. The standalone app/OpenSearch checks passed (exit 0); live `/health` confirmed six MCP tools, and E005 synthetic-policy HTTP chat returned three citations. Existing sources were not reparsed. A plain restart without these overrides still encounters the incomplete `.env` OpenRouter profile described in [activation status](openrouter-activation.md).

## Diagnostics

The standalone script now has separate image-embedding and VL checks. They send only generated synthetic shapes and require explicit model-probe opt-in:

```sh
.venv/bin/python scripts/health_check.py --checks openrouter-image-embedding openrouter-vision --probe-models --timeout 90 --json
```

Do not repeatedly retry the VL probe while the provider is rate-limiting. Default diagnostics skip model calls. Existing [health-check exit-code and credential-safety rules](health-checks.md) still apply.
