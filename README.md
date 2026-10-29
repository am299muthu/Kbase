# ABC Knowledge

**Latest rollout (2026-10-28):** Hybrid search is active: the original ten rebuilt PDFs have 273 verified vector chunks. All new uploads and ingestion requests require BM25 + real embeddings; PDFs also require native text parsing and VL image processing. SQLite stores controls and Garage stores files. Five later PDFs failed hybrid reprocessing (four guardrail failures, one VL-output failure); their old generations were preserved. The **Ingestion monitor** shows steps, progress, request history and captured errors. See [current batch and processing policy](docs/pdf-pilot-rebuild.md).

A local enterprise knowledge-base prototype with governed document onboarding, folder-entitled retrieval, citations, PII filtering, and six real MCP tools. All bundled policy and employee examples are **synthetic**. This is not a production banking system.

## Quick start

For this already-configured Mac, use the [service startup order](#service-startup-order-existing-local-installation) below. The setup commands in this section are for first-time initialization.

Use Python 3.11+ from the project root. Native OpenSearch must be running on port 9200 for the default profile; see [infrastructure setup](docs/infrastructure.md). No external model key is needed for BM25 retrieval with extractive responses.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp -n .env.example .env  # Preserve existing private credentials/configuration.
.venv/bin/python -m kb.cli init
.venv/bin/python -m kb.cli serve --maintenance
```

New ingestion now requires the configured `pdf-hybrid` profile and approved external-model processing; the `local` profile rejects uploads, ingestion and index writes. For an empty hybrid index, run `serve --maintenance` to stage and review requests, process approved requests with the matching worker, then pass `profile-check` before normal serving. The existing installation is already provisioned; use the startup order below.

**Existing ARM64 installation:** OpenSearch now requires HTTPS, its local CA and the private service credentials already in `.env`. Do not replace that file with the blank template. Security and Lucene k-NN are installed and locally verified; see [plugin setup and limitations](docs/opensearch-arm64-plugins.md).

Open <http://127.0.0.1:8000> and sign in as **E003**, the retail maker, using `DEMO_PASSWORD` from your local `.env` (the local demo account chooser also provides it). Do not expose demo mode publicly: it deliberately exposes synthetic accounts and their shared demo password. Health is <http://127.0.0.1:8000/health>.

Initialization seeds 50 synthetic employees and five business roots, then onboards the small synthetic policy fixtures. A generated retail DOCX mirrors the retail Markdown source for a multi-format demonstration; a separate retail escalation supplement adds distinct evidence. This does **not** ingest the full `KM_Data` corpus. Source parsing was initially permission-blocked; a subsequent user-authorised local pilot ingested the first three PDFs into the restricted `pdf-pilot` folder (86 chunks). See the [ingestion guide](docs/ingestion.md) for the completed batch, access roles and repeat-run behaviour. Local answers are explicitly labelled **evidence excerpts**, not LLM-generated responses.

## Service startup order (existing local installation)

Start **OpenSearch → Garage → the app**, then run the health check. Run project commands from `/Users/MutharasanAnbarasan/PROJECTS/Kbase`. Preserve the existing `.env` and Garage configuration; normal restarts do not require initialization or provisioning.

1. **Start OpenSearch** (HTTPS, port 9200). Homebrew runs it in the background:

   ```sh
   brew services start opensearch
   .venv/bin/python scripts/health_check.py --checks opensearch
   ```

   Wait for the OpenSearch check to pass before continuing.

2. **Start Garage** (local S3, port 3900) in a dedicated terminal:

   ```sh
   .venv/bin/python scripts/start_object_store.py
   ```

   Keep this terminal running. If Garage is already running, do not start another instance. An app startup error mentioning `Could not connect to the endpoint URL: "http://127.0.0.1:3900/abc-knowledge"` means the configured storage endpoint is unreachable; check this service first.

3. **Start one app instance** (HTTP, port 8000) in a second terminal:

   ```sh
   .venv/bin/python -m kb.cli serve
   ```

   Keep this terminal running and wait for `Application startup complete`. Starting the app also starts its ingestion worker: existing approved requests matching the current processing scope can run automatically. A normal restart does not require a separate `worker` command.

4. **Verify readiness** from another terminal:

   ```sh
   .venv/bin/python scripts/health_check.py --checks app opensearch
   ```

   Both checks should pass. Open [the app](http://127.0.0.1:8000) or [its health endpoint](http://127.0.0.1:8000/health). The full default health check may report skipped PostgreSQL/model checks; the current installation uses SQLite, and model probes require explicit opt-in.

SQLite needs no separate server. OpenSearch Dashboards is optional and is not required to start the app. Garage and the app must be restarted after their foreground processes stop or the Mac restarts. For an orderly shutdown, stop the app with Ctrl+C before stopping Garage; stop OpenSearch last if needed with `brew services stop opensearch`.

## Modes and prerequisites

| Mode | Persistence and retrieval | Models |
|---|---|---|
| Default local profile (`APP_MODE=demo`) | OpenSearch BM25; SQLite controls/lineage; configured local/S3 objects (this host now uses local Garage S3) | Extractive responses; no external model calls or embeddings |
| `APP_MODE=infrastructure` | SQLite or PostgreSQL controls/manifests, approved S3-compatible files, OpenSearch | Governed opt-in OpenRouter generation/embeddings; hybrid startup requires verified corpus readiness |

Infrastructure mode refuses incompatible backends rather than silently falling back. It requires OpenSearch and S3; staff/access/structured controls may remain SQLite or use PostgreSQL. External processing requires inference-assisted PII/grounding guardrails; local rules/NER run first but do not prove anonymity before an external detector sees text. See [design](design-and-evaluation.md), [current rollout](docs/governance-and-activation.md) and `.env.example`.

Use [native infrastructure setup](docs/infrastructure.md) and `scripts/install-native-opensearch.sh` for the native OpenSearch install helper, following its usage and prerequisite messages. The helper installs OpenSearch **natively, not with Docker**, when the operator runs it. The user has since installed OpenSearch and Dashboards 3.9.0; the server is live-verified on port 9200. Its production observability/ingestion/security stack is not implied by that native installation. A standalone Python LocalStack CLI can be installed, but the LocalStack service normally still needs a Docker-compatible runtime. Installing its CLI alone is not a native S3 service. A real S3 endpoint is an alternative. PostgreSQL and these services are separate prerequisites, not started by the five-command demo setup.

### OpenRouter (opt-in)

`.env` selects a named `local` or pending `hybrid` runtime profile. Individual flags do not override that profile's gates. The requested external provider is **OpenRouter**; model IDs/dimensions and the rotated API key stay in private `.env`. The compatible multimodal index is now provisioned, but real vectors and current checker/transfer approvals must be verified before selecting hybrid. `serve` refuses an unready hybrid corpus. Requested free-tier model IDs are configuration defaults, **not a guarantee of availability, zero cost, quotas, or measured quality**. See [rollout procedure](docs/governance-and-activation.md).

**Security action:** the original requirement material exposed an API credential. Revoke/rotate it with the provider; never copy it into configuration, screenshots, logs, or repository history. This documentation does not reproduce it. Provider requests are masked by default; do not enable `ALLOW_UNMASKED_EXTERNAL` without approved data-processing governance.

### Hybrid PDF extraction and separate image embeddings

The PDF parser is `docling_hybrid`: **pypdf text + pdfplumber searchable grid-table cells + Docling object identification/crops**. OpenRouter VL descriptions are labelled generated supplements. The active profile embeds native text and visual interpretations for hybrid retrieval; separate image embeddings are disabled. See [current rebuild and verification](docs/pdf-pilot-rebuild.md).

**PDF-pilot rebuild:** The new `pdf-hybrid` profile combines BM25 and text-vector retrieval for ten small PDFs, with VL image interpretations included in the embedded text. It preserves other folders in the existing BM25 index. The configured VL fallback is `nvidia/nemotron-3.5-super-vl:free`; output is schema-validated and the actual model is recorded. See [batch scope, cleanup backups, parsing and activation checks](docs/pdf-pilot-rebuild.md). This profile must pass readiness verification before activation; creating or approving requests alone does not make the batch searchable.

## Operator workflows

```sh
.venv/bin/python -m kb.cli --help
.venv/bin/python -m kb.cli inventory
.venv/bin/python -m kb.cli mcp-check
.venv/bin/python -m kb.cli ingest mockdata/policies/retail-banking-policy.md --limit 1 --folder retail-banking --maker E003
.venv/bin/python -m kb.cli worker
.venv/bin/python -m kb.cli archive
.venv/bin/python -m kb.cli recover
```

CLI `--auto-approve` and operator-supplied `--checker` are now rejected. Onboarding stages a maker request; a separately authenticated checker must review the exact source hash/version and processing/transfer scope. The web process runs an approved-request worker. Existing documents must use exact-ID reprocessing requests rather than another corpus-directory import.

In the UI, a maker uploads to an entitled folder; a different checker approves; the worker parses and indexes it. Owners/delegates manage folder grants and subfolders. Access requests in chat record an audit request only; they never grant permissions.

### PDF ingestion and governed enrichment

The existing three-PDF pilot has exact-ID hybrid reprocessing requests with independent E002 reviews recorded. Actual external transfer and enrichment remain pending. Do not repeat a directory import to enrich them; that selects new files instead. See [current requests and approvals](docs/governance-and-activation.md).

For a separately authorized **new** PDF, stage one exact source as maker, then sign in separately as checker and review its captured source/transfer scope. No ingestion or external processing occurs before that approval:

```sh
.venv/bin/python -m kb.cli ingest /APPROVED/NEW/SOURCE.pdf \
  --folder pdf-pilot --maker E003 --limit 1
```

`reindex` rebuilds already-published curated chunks; it does not reparse PDFs. Reprocessing creates new generations and preserves the current generation if it fails. Legacy checker IDs or maker-entered approval flags are not approvals for a new model/destination/scope.

To rebuild existing approved documents from their curated objects without importing more files or changing approvals:

```sh
.venv/bin/python -m kb.cli reindex --limit 1000
# Or restrict the rebuild:
.venv/bin/python -m kb.cli reindex --folder pdf-pilot
```

Reindexing does not reparse PDFs or enable Docling. See [current gaps](docs/current-gaps.md) for OpenRouter, semantic retrieval, Docling/OCR and production controls still to provision.

## Standalone service health checks

```sh
.venv/bin/python scripts/health_check.py
.venv/bin/python scripts/health_check.py --checks app opensearch --json
# Optional synthetic model calls; review pricing and configure a rotated key first:
.venv/bin/python scripts/health_check.py --probe-models --json
```

Checks app health, OpenSearch endpoints, authenticated PostgreSQL `SELECT 1`, and optional OpenRouter embedding/inference requests. Set `POSTGRES_HEALTH_URL` privately when the app still uses SQLite. Model probes require the explicit flag and may incur API charges; no corpus text is sent. Exit codes: **0** all selected checks passed, **1** failed, **2** incomplete/skipped. See [configuration, safe output and limitations](docs/health-checks.md).

## Synthetic identities

| Root ID | Employees | Owner / checker / maker / delegate |
|---|---|---|
| `retail-banking` | E001–E010 | E001 / E002 / E003 / E004 |
| `technology` | E011–E020 | E011 / E012 / E013 / E014 |
| `risk-compliance` | E021–E030 | E021 / E022 / E023 / E024 |
| `finance` | E031–E040 | E031 / E032 / E033 / E034 |
| `people` | E041–E050 | E041 / E042 / E043 / E044 |

Remaining employees are readers. Each employee initially has only their own root; ordinary reader/maker/checker grants are exact-folder, while owner/delegate privileges inherit to descendants. PII visibility is a separate employee entitlement, not permission to read another business root.

## Evaluation and demonstration

- [50 manual evaluation cases and rubric](evaluation/README.md): expected answers/tools, safety checks, and full-evaluation protocol. [Limited local smoke measurements](evaluation/smoke-measurements.json) cover 16 selected cases (p50 578.81 ms; nearest-rank p95 601.51 ms), not full quality/entailment scores; Q041 has a documented no-evidence handling failure.
- [Demo guide](docs/demo-guide.md): four tasks, MCP traces, maker-checker walkthrough, and multi-document/multi-format evidence checks.
- [OpenSearch Dashboards and query-latency setup](docs/opensearch-observability.md),
- [Functional coverage and gaps](requirements.md), [architecture and design](design-and-evaluation.md), [AI tooling](ai-tooling.md), [deployment status](deployed.md).
- [Architecture PNG including access control](docs/architecture-access-control/architecture.png), [editable diagrams.net XML](docs/architecture-access-control/architecture.xml), and [role matrix / trust boundaries / flows](docs/architecture-and-access-control.md). Generate with `.venv/bin/python scripts/render_architecture.py --output-dir docs/architecture-access-control` using Pillow; this is an asset generator, not a test runner. Historical `docs/architecture.*` and root-level submission copies are preserved.

## Boundaries

The prototype has not demonstrated terabyte-scale ingestion, high availability, production authentication, immutable audit storage, complete PII recall, universal file-format support, weekly metadata connectors, or deployment. Regex intent routing is not an autonomous LLM planner. Citation-ID validation does not prove that a claim is semantically supported. Keep the original corpus and all secrets out of published demos unless independently cleared.
