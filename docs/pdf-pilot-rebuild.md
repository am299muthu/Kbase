> Scope update, 2026-11-06: PII detection/redaction and PII visibility gates have been removed from the active pipeline. References to those checks below describe historical runs. Folder authorization and evidence grounding remain active.

# PDF-pilot rebuild (2026-10-27)

## Current status (2026-10-28)

All ten original PDFs have completed hybrid generations: **273 verified chunks**.
`KB_RUNTIME_PROFILE=pdf-hybrid` is active and readiness passes. Subsequent retries
completed work beyond the earlier `ingestion-results.json`; the final ten-document
receipt is `completion-20261028.json` in the private rebuild directory.

**Hybrid ingestion is mandatory for all new UI uploads, CLI ingestion, retries
and index writes, in every entitled folder.** BM25-only configuration is rejected
before a new upload is stored. PDF processing additionally requires VL. A failed
embedding or guardrail does not publish a BM25-only replacement. Existing legacy
generations remain readable until reprocessed; five later financial-highlight
PDFs have exact hybrid requests in `additional-hybrid-requests-20261028.json`.

Those five additional requests were subsequently approved by E002 and attempted.
Four failed during embedding guardrails (one invalid JSON response, three invalid
safe-redaction verdicts); the fifth failed VL output validation after 16 of 48 visual
regions. No hybrid replacement was published for those five. Their earlier published
generations remain intact. The monitor records the exact current step and errors;
these five must not be counted among the ten verified hybrid documents.

The historical `OPENSEARCH_HYBRID_FOLDERS` value is retained solely for existing
approval fingerprints; it no longer restricts which entitled folders can receive
hybrid ingestion. Retrieval and section resolution use each published generation's
actual configured index, allowing approved migrations without losing old documents.

Open **Ingestion monitor** (`/#ingestion`) for five-second refreshes, live step,
visual/chunk counts, active model, errors and request history. Earlier requests
without step telemetry say so rather than inventing progress. A cancelled replacement
does not make the already-published document appear unindexed. Detailed diagnostics
remain restricted to the document's maker/checker/owner roles.

Use `serve --maintenance` to keep approvals and monitoring online while chat and
the embedded worker are paused. A separately run worker can report progress during
maintenance. Normal `serve` resumes chat and one embedded worker.

The indexing guardrail now disables reasoning output and explicitly requests the
required JSON structure. A long synthetic policy passed the actual adapter. Invalid
guardrail responses capture finish reason and output lengths without document text;
the bounded diagnostic buffer retains the most recent failure.

Scope: replace only `pdf-pilot` with ten small PDFs. Preserve original files in
`KM_Data/Pdf`, synthetic demo documents, employee accounts, grants, and audit history.
The selected batch has 47 pages, with at most nine pages per file; the 512-page
source is excluded. The exact source paths and SHA-256 hashes are recorded in
private `var/pdf-pilot-rebuild-20261027/selection.json`.

## Cleanup and recovery evidence

The scoped reset removed 13 old pilot document records, 202 OpenSearch chunks,
and 66 Garage document objects. No index or bucket was dropped. The preserved
SQLite snapshot, index records, object backups, and receipts are under
`var/pdf-pilot-rebuild-20261027/backup-bounded/`. Original PDFs are unchanged.
An earlier backup attempt stopped before deletion because the service account
does not have scroll permissions; the successful backup used bounded reads and
exact chunk-ID deletes without expanding service permissions.

`scripts/reset_pdf_pilot.py` requires a plan followed by `--apply`, checks that
the app is stopped and no request is processing, verifies source hashes and
the unchanged inventory, and backs up before deletion. It is limited to the
known local Garage endpoint, known project indexes, and exact pilot IDs.
Do not rerun it on the new batch unless a new reset is intended.

## Parsing and retrieval

- Native PDF text uses pypdf; searchable grid tables use pdfplumber.
- Docling identifies document objects and renders bounded visual regions.
- The VL model interprets those regions. Generated descriptions and model-read
  text retain page/object provenance, uncertainty, and generated-content labels.
- Chunks preserve page, section, source hash, and visual-object metadata. Native
  text and generated visual interpretations receive text embeddings for retrieval.
- The `pdf-hybrid` profile combines BM25 and vector rankings for `pdf-pilot` in
  `abc-kb-curated-multimodal-v2`. Other folders retain BM25 content in
  `abc-kb-curated-v1`. Access and current-generation checks apply to both indexes.
- Separate image embeddings are disabled in this profile; images contribute
  through their VL-generated text. The earlier `hybrid` profile remains the
  separate full multimodal configuration.

All ten selected PDFs passed local extraction, asset-bound checks and chunking.
Preflight produced 268 chunks before visual enrichment; this is not the final
indexed chunk count. Results are in private `preflight.json` beside the selection.

## Vision fallback and JSON validation

Primary: `google/gemma-4-31b-it:free`.
Explicit fallback: `nvidia/nemotron-3.5-super-vl:free`, configured with
`OPENROUTER_VISION_FALLBACK_MODEL` in private `.env`.

Only HTTP 429/502/503/504 trigger the configured fallback after bounded retries.
Authentication failures, invalid approval, malformed output, and truncation do
not trigger another model. Every request retains the zero-price ceilings and
disables provider fallbacks; availability is not guaranteed. The actual model
used is recorded on each generated visual block.

The prompt requires exactly `description` (nonempty string), `visible_text`
(array of strings), and `uncertainties` (array of strings), with empty arrays
rather than nulls. It prohibits markdown fences, reasoning, and extra prose.
Reasoning output is disabled and the completion budget is 2,400 tokens.
The NVIDIA adapter passed a synthetic invoice extraction with schema validation
and the expected visible value. This is adapter evidence, not a substitute for
checking the real documents.

## Approval and activation

Each fresh request captures the source, destination, parser settings, primary
and fallback model IDs, and transfer kinds. E003 submits; independently signed-in
E002 reviews. `requests.json` records the exact ten request IDs. A local-profile
worker cannot run a PDF-hybrid request. Approval does not mean indexing succeeded.

Process only those exact requests under `KB_RUNTIME_PROFILE=pdf-hybrid`, with
the serving app stopped for the cutover. Confirm all ten requests are completed,
all ten PDFs indexed, and current chunk/vector hashes validated before activation:

```sh
KB_RUNTIME_PROFILE=pdf-hybrid .venv/bin/python -m kb.cli profile-check
```

After readiness passes, select `KB_RUNTIME_PROFILE=pdf-hybrid` in private `.env`
and start the app using the normal README service order. The startup gate checks
pilot vectors/approvals and preserved demo chunks. For non-pilot folders, this
profile supports existing BM25 retrieval; new ingestion requires an appropriate
folder processing profile. Do not switch back to `local` after publication and
assume pilot chunks are in the old index.

Runtime result receipts are saved in private `ingestion-results.json`; failures
must be diagnosed and reviewed before a new request. No automatic checker
approval or fabricated vector/visual fallback is used.

## Installed agent skills

The official [Docling skill](https://github.com/docling-project/docling/tree/main/docling/.agents/skills/docling)
and community [Unstructured PDF Parser skill](https://github.com/memtomem/agent-skills-public/tree/main/skills/pdf-parser)
are installed in the user's Codex skills directory. These are agent guidance;
the second skill is not the Unstructured.io cloud service or a new runtime
dependency. No corpus was sent to an Unstructured service.

## Model fallback chains and 4096-dimension embeddings (2026-11-04)

Model settings now accept ordered, comma-separated chains (`kb/config.py: model_chain`, `kb/providers.py: run_model_chain`):

| Task | Setting | Order tried |
|---|---|---|
| Answers | `OPENROUTER_MODEL` | free models in listed order, then paid |
| Guardrails | `OPENROUTER_GUARDRAIL_MODEL` (else `OPENROUTER_MODEL`) | same |
| VL interpretation | `OPENROUTER_VISION_MODEL` + `OPENROUTER_VISION_FALLBACK_MODEL` | free primary, free fallback, then paid |
| Text embeddings | `OPENROUTER_EMBEDDING_MODEL` (single model) | `qwen/qwen3-embedding-8b`, 4096 dimensions (paid; requested with `dimensions=4096`) |
| Image embeddings | `OPENROUTER_IMAGE_EMBEDDING_MODEL` | chain; disabled in `pdf-hybrid`. Qwen text embeddings cannot embed pixels. |

- The next model is tried after an availability, quota, credit, missing-endpoint or invalid-output failure. HTTP 401/403 and approval errors stop immediately. Each attempt is recorded in the failure diagnostics (`trying_next_model`, `paid_model_used`).
- A non-`:free` model is reachable only when `OPENROUTER_PAID_MAX_PRICE_PROMPT` and `_COMPLETION` (USD per million tokens) are above zero; they are sent as the provider `max_price`, and free models keep the zero ceiling. They are part of the approval scope, so changing them needs fresh checker approval.
- The `OPENROUTER_For_VL_MODELS` / `OPENROUTER_EMBEDDING_For_VL_MODELS` names in `.env` are not read by the code; the settings above are authoritative.
- Mixing embedding models in one field would make query and document vectors incomparable, so the embedding setting must be a single model. A 4096-dimension model needs a new index: `abc-kb-curated-multimodal-v3` (created from `index_definition()`; the service role gained that exact index). `profiles/pdf-hybrid.env` now selects v3. The v2 index and all objects are untouched for rollback.

Cutover state: v3 is empty, so `profile-check` fails and the app must not be restarted on `pdf-hybrid` yet. 18 reprocess requests (new scope) are pending E002 review; after approval run `KB_RUNTIME_PROFILE=pdf-hybrid .venv/bin/python -m kb.cli worker`, then `profile-check`. Rollback: restore `profiles/pdf-hybrid.env` (v2) and `.env` from `var/`.

