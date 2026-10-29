# Governed file ingestion and enrichment

Run from the project root with `.venv`. SQLite holds staff/access/structured controls; the current file backend is native local S3 and search is OpenSearch. See [latest rollout, restart and permission boundaries](governance-and-activation.md).

## Pilot roles and existing documents

| Folder | Employee | Role |
|---|---|---|
| `pdf-pilot` | E001 | Owner |
| `pdf-pilot` | E002 | Checker |
| `pdf-pilot` | E003 | Maker |

No additional pilot grants were created. The pilot folder was separately provisioned; fresh initialization creates the five standard roots, not this pilot.

The original import remains published as **3 PDFs / 86 chunks**:

| Filename | Document ID | Original chunks |
|---|---|---:|
| `08036c5a50a93da84c5c45ba468c58159d75281e.pdf` | `cf79d28e0f1290c5ea51ba28e5203034` | 30 |
| `0a29925ccc5e6299e132a73325956a3abef6dd26.pdf` | `602897f0f4529fa74f6e0aaf6dcd0c09` | 23 |
| `0e21835a42a6df2405496f62647058ff855743c1.pdf` | `0f9e27ece89d8a36cbf2401042108a1a` | 33 |

These still reflect original pypdf extraction, not new VL/grid enrichment. Exactly these IDs have target-profile reprocessing requests with checker approvals recorded; external execution remains pending. Do not reimport the directory or repeat unchanged approvals to enrich them.

## Stage a separately authorized new source

```sh
.venv/bin/python -m kb.cli ingest /APPROVED/NEW/SOURCE.pdf \
  --folder pdf-pilot --maker E003 --limit 1
```

Or sign in as maker and upload through the UI. Business metadata is inherited; the maker cannot supply compliance-approval flags. CLI `--auto-approve` and operator-supplied `--checker` are now rejected before file ingestion. Old auto-approval examples are historical, not supported governance.

New files remain pending with a request binding their hash, version, metadata and exact processing/transfer scope. Sign in separately as E002, open **Approvals**, inspect that source/scope, confirm it and submit a review. Owners also cannot approve their own work. External-domain sources need explicit checker control-evidence references, not maker booleans.

The worker processes only approved requests matching its runtime profile. A local-profile worker ignores pending/approved hybrid-profile requests rather than processing them under different settings. Source/config/entitlement changes invalidate processing authority; approval is rechecked before outbound ingestion model requests, including retries.

## Reprocess existing PDFs

Use exact existing document IDs, not a directory scan:

```sh
.venv/bin/python -m kb.cli reprocess --folder pdf-pilot --maker E003 \
  --document-id DOCUMENT_ID --idempotency-key UNIQUE_BATCH_KEY
```

This only submits a review request under the selected runtime profile. It does not approve, parse, index or send data. Repeating the same key/source/scope is idempotent; changing its binding fails. The actual pilot requests have already been staged for the pending hybrid profile; view them rather than creating another open request.

After independent approval and direct external-transfer permission review, processing must run under the exact approved target profile. Reprocessing stages immutable generation objects/chunk IDs and publishes SQL manifests only after index success. Failure preserves the old published generation. Cross-index publication needs a stopped-writer maintenance window and consistent control/object backup; do not serve the old index while replacing its SQL manifests for a different target.

Scanned/sparse/raster-dominant PDF pages use configured VL reading of overlapping rendered regions, not a Tesseract prerequisite. Their combined coverage can contain the whole page and is included in transfer scope. Model-read text is generated/unverified. Disabled/unavailable VL fails closed instead of indexing unreadable scan placeholders.

## Reindex versus reprocess

```sh
.venv/bin/python -m kb.cli reindex --folder pdf-pilot
```

Reindex reads the **current published curated generation**, not original PDFs. It validates IDs/hashes and concurrent generation state. It cannot discover new objects, grids or visual descriptions. External embeddings require a current source/scope-bound approval; legacy checker IDs alone are insufficient. Any error produces a nonzero exit. Neither operation silently changes employee grants.

## Selection and recovery

- Directory `ingest` walks supported files in sorted order; `--limit` counts new files. Repeating an unchanged directory import selects subsequent new files, not previously indexed PDFs.
- Changed bytes under a filename create a version; future `--latest-date` values are rejected. Retention defaults to seven years.
- Retry/metadata changes require fresh review. Metadata changes invalidate approval and pause retrieval until the replacement is reviewed/processed.
- Stop all workers before `recover --folder FOLDER --document-id ID`. Recovery is exact-ID and fails interrupted work for fresh review; it never requeues approval automatically.
- Errors omit source text and raw third-party bodies. Use request status, library and audit log, not just process exit, to confirm success.

Real OpenRouter transfer remains blocked pending direct review outside Auto permission mode. Local S3 persistence is live, but real pilot enrichment and hybrid corpus activation have not occurred. See [current gaps](current-gaps.md).

## Diagnosing failures after approval (2026-10-27)

The additional ten-PDF batch reached **4 indexed / 6 failed** after checker approval. Diagnosis used scoped request metadata and local parsing only; no real-document model transfer occurred.

| Failure | Finding / remedy |
|---|---|
| One `ParseError` | Off-page PDF drawing coordinates produced an invalid pdfplumber table bounding box. Detection now clips to the visible page before extracting cells. The affected `201ab28823c4a10ca391e800642e9cf381737ed2.pdf` passed actual local extraction/visual-gate/chunking with 45 chunks after the fix; it has not been republished. |
| Four `ModelUnavailable` errors | Each contains a large raster cover region. Nonuniform masked images cannot be assumed to be text-free decoration solely because native cover text exists. The approved local scope has VL disabled; these files need reviewed visual-processing scope and direct external-transfer permission, not repeated unchanged local retries. |
| One `ParserLimitError` | The 512-page file has 206 scan/raster candidate pages, implying 824 overlapping scan tiles before other objects, above the reviewed 100-object budget. Review a bounded split or different resource/transfer scope. The check now fails before expensive Docling conversion/rendering. No limits were silently increased and no partial document was indexed. |

Low character count no longer automatically makes a provably blank/simple native-text page require VL. Only explicitly accounted-for text/state operators are exempt; unknown drawing, image and annotation content remains subject to visual review. Genuine image-only scans and scans with electronic headers still fail closed when VL is disabled.

New failures record a fixed public error code and operation stage in the error/audit data, without raw third-party exception text or source content. A new initial-processing request clears the document's stale error display while retaining the old failed request. Existing failure records were **not** rewritten by this repair.

### Retry the corrected table PDF

1. Sign in as **E003**, find `201ab28823c4a10ca391e800642e9cf381737ed2.pdf`, and select **Retry**. This submits a fresh pending request; it does not reuse the failed approval.
2. Sign in separately as **E002** and review that new request in **Approvals**. On approval, the local worker can attempt indexing with the fixed parser.
3. Confirm `indexed`/`completed`, not merely `approved`. Until then the file remains unindexed. Do not submit unchanged local retries for the other five blocked files.

Verification: actual failed-source local extraction produced 41 blocks, 28 assets and 45 chunks; focused inline synthetic checks covered blank separators/short titles, image-only scans, scans with native headers, unknown drawings, early object-budget refusal and sanitized stage diagnostics. The focused scan checks stubbed Docling rendering; they are not a VL-quality evaluation. Retry-state/maker–checker checks used an isolated in-memory database. The app was restarted once with fixes and returned HTTP 200 with healthy database/storage/search/MCP/PII. No persistent test scripts, automatic checker approvals, model transfers or corpus publications were added by the repair.
