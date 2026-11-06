> Scope update, 2026-11-06: PII detection/redaction and PII visibility gates have been removed from the active pipeline. References to those checks below describe historical runs. Folder authorization and evidence grounding remain active.

# Build verification — 2026-10-26

> Latest 2026-10-27 results: [governed multimodal rollout](governance-and-activation.md). Local S3 migration and the compatible index are now provisioned; real OpenRouter enrichment/hybrid corpus activation remain incomplete. Older sections below are dated history, not current provisioning gaps.

> The initial-build sections below describe the earlier SQLite/extractive snapshot. The subsequent PDF pilot and OpenSearch migration supersede its search-backend and corpus-ingestion status. See [current gaps](current-gaps.md) and the migration verification at the end of this report.

## Executed locally

- Created `.venv` with Python 3.13.9 and successfully installed `requirements.txt`.
- Compiled all `kb` Python modules; `node --check kb/static/app.js` passed.
- Initialised SQLite, local object layers, 50 synthetic employee/access records and seven synthetic documents (six Markdown fixtures and one generated DOCX). An additional synthetic TXT upload exercised maker–checker and was subsequently archived by a backdated metadata update.
- Started the real HTTP application on `http://127.0.0.1:8000` and exercised it through HTTP clients, not only imported functions.
- `/health`: app/database/local storage/local search healthy; actual MCP stdio discovery reported six tools; external model processing disabled.
- Authenticated MCP lookup completed through `initialize` → `list_tools` → `call_tool`; temporary smoke session was removed.
- Login/logout and unauthenticated rejection passed. A cross-origin state-changing request was rejected.
- Cited policy chat returned actual MCP search/section/PII traces. A multi-document answer included the distinct retail escalation supplement plus main-policy Markdown and DOCX citations.
- Synthetic name/email/phone were masked in answer, evidence and traces. Policy quantities and stable citation IDs were preserved. This is **not** a complete PII recall evaluation.
- A maker could not approve their own upload; an unentitled checker could not approve it. An independent entitled checker approved it and the background worker indexed it.
- Live reader grant revocation immediately removed document visibility and returned `no_access`; the original grant was restored afterward. Last direct owner removal was rejected.
- Seven-year retrieval filtering excluded a backdated synthetic source before/while housekeeping completed. A separate expired synthetic DOCX image fixture verified durable object-URI references in parsed assets/blocks and was archived; parsed lineage uses a valid `#/blocks` JSON pointer rather than a nonexistent chunk anchor.
- Verified the local audit event predecessor/hash chain. This demonstrates internal consistency, not immutable or malicious-administrator-resistant storage.
- Independent browser verification ran headless Chrome against an isolated synthetic runtime: desktop and 390px mobile layouts, login/navigation, library metadata/upload, self-review blocking, independent approval, folder/grant display, audit, and chat with working citation links/tool traces. No browser runtime/CSP errors or mobile body overflow were observed; the chat screenshot was also reviewed visually. The isolated browser/server were stopped afterward.

Parser-specific synthetic checks are documented in [parsers.md](parsers.md); the parser implementation was separately exercised with generated PDF page citations, DOCX/CSV/XML/EML/spreadsheet examples, schema-only Parquet, giant-text coverage and invalid-input rejection.

## Limited measured evaluation

Artifact: [`evaluation/smoke-measurements.json`](../evaluation/smoke-measurements.json).

Sixteen selected cases from the 50-question manual set were executed over real HTTP. Settings: local SQLite lexical retrieval, extractive answers, warm web application, fresh private MCP process per chat, no external API/model calls. HTTP chat latency excludes login and includes MCP startup. The selected set includes one fast pre-tool regex refusal, so compare distributions only with the same workload.

- p50: **578.81 ms**.
- p95 (nearest-rank): **601.51 ms**.
- Expected tool-name coverage: **16/16**; this is not autonomous tool-selection accuracy.
- Citation ID integrity: **16/16** (vacuous for responses with no citations); this is not semantic citation accuracy or groundedness.
- Human answer quality, semantic groundedness and semantic citation accuracy: **not measured**.

**Observed quality miss:** Q041 requests a disaster-recovery street address that no fixture supplies. Local lexical mode returns related incident excerpts with no `insufficient_evidence` escalation. It does not invent an address, but it does not reliably recognize insufficiency. Record this as a failed abstention target rather than a correct answer. Do not use extractive demo behavior as a production question-answering quality guarantee.

Four additional calls compared retrieval `k=3` and `k=8` on Q021/Q025. Both k values included the distinct retail supplement; Q025 at k=3 omitted DOCX evidence, while k=5 and k=8 included DOCX+Markdown. This supports retaining k=5 for the small fixture demonstration only, not a general retrieval optimum.

## Not performed / not verified

- The supplied `KM_Data` corpus was only inventoried by path/type/size. Its content parsing was denied by the environment permission check and remained stopped. No real source records were ingested or sent to a model.
- OpenSearch/PostgreSQL/LocalStack integrations were implemented but were **not** verified against running infrastructure services. Native OpenSearch packages were not installed; the operator helper is provided separately.
- No external inference or embedding API call, model availability/free-tier validation, or real confidential-data transfer.
- No full 50-case human-scored quality evaluation, production PII benchmark, production load/HA/failover exercise, weekly metadata connector, deployment, video recording, GitHub publication, or repository-sharing action.
- CI workflow code was added, but no remote CI run occurred because this directory is not a Git repository.

## Subsequent PDF pilot and OpenSearch migration

After the user authorised the three-PDF import and confirmed the pilot roles, the PDFs were indexed and verified locally: 30 + 23 + 33 = 86 chunks. The source text was not printed during migration verification.

The user then installed native OpenSearch/Dashboards 3.9.0 and requested OpenSearch rather than SQLite indexing. Executed and verified:

- OpenSearch on port 9200 returned HTTP 200; plugin inventory was empty.
- Set the actual local `.env` and application default to `SEARCH_BACKEND=opensearch`; explicitly selected BM25 (`OPENSEARCH_VECTOR_SEARCH=false`) because k-NN is absent.
- Added `kb.cli reindex` to migrate already-approved curated objects without changing grants/approvals or reparsing originals.
- Reindexed **10 documents / 107 chunks**, with zero reported errors. Compared stored OpenSearch text hashes before replacing legacy SQL text caches with integrity/lineage manifests. Original curated objects remain preserved.
- Verified all 107 SQL chunk rows have empty `text` and no `text` field in their payload; their IDs, metadata and content hashes remain for authorization/integrity checks.
- Verified OpenSearch BM25 search and source-section reads for all three PDFs, with source page metadata intact; ungranted E005 did not receive pilot evidence.
- Restarted the web app and checked actual HTTP `/health`: OpenSearch cluster green, retrieval `bm25`, `embeddings_used=false`; database SQLite controls, local objects, six MCP tools and extractive inference were reported separately.
- Verified real HTTP/MCP policy chat, PDF retrieval and cross-folder denial after restart. Responses were checked in memory without printing source text.
- Simulated a search-client failure and confirmed that it propagates, with no fallback to SQLite search.
- Python compilation, `pip check` and reindex command help passed. An optional new-ingestion/cleanup smoke was not executed because the proposed broad archive cleanup was permission-blocked; no deletion was retried.

OpenRouter calls, semantic embeddings, k-NN, Docling reprocessing, PostgreSQL/S3 migration and production security remain unverified/unprovisioned. The initial SQLite latency artifact is historical, not a performance measurement of the new OpenSearch profile.

## Subsequent ARM64 plugin installation

- Installed version-matched Security and k-NN **3.9.0.0** from Maven Central after SHA-512 validation. Added the three exact upstream-pinned Bouncy Castle libraries missing from the Homebrew runtime after SHA-256 validation; the initial startup failed without them.
- Preserved stopped-server data/configuration and the previous app environment in `var/opensearch-preplugins.VAeDIO/`. Existing **107 KB chunks** remain intact.
- With explicit user authorization, created private local CA/node/admin keys and service credentials, enabled loopback-only HTTPS/transport TLS, initialized custom Security configuration, and scoped `kb_service` to the existing KB index. The required mapping-read action was separately authorized.
- Verified loaded plugins through the live node API, CA/hostname validation, anonymous rejection (401), service denial on other indexes/security-index/cluster-settings reads, and existing index mapping access.
- With explicit user authorization, created `kbase-knn-arm64-smoke-20261026`, indexed three synthetic float32 vectors, and passed a filtered Lucene HNSW query. Deleted only that exact synthetic index and verified its absence. No native Faiss/NMSLIB/quantized path was tested or provisioned.
- Added `OPENSEARCH_CA_CERTS` support; private `.env` now uses HTTPS and the restricted credentials. Restarted the real application; `/health` is healthy/green, authenticated six-tool MCP check passed, and synthetic-only E005 HTTP policy chat returned citations.
- A proposed real-PDF content check was permission-blocked and not executed. A proposed cluster-setting write denial test was blocked and omitted; subsequent denial checks were read-only. No cluster-setting write or real-source output occurred.
- KB retrieval remains **BM25**, with external processing disabled and no embeddings computed. Dashboards was not started/reconfigured. Local plugin verification does not establish official macOS support or production/FIPS compliance. See [ARM64 installation and limitations](opensearch-arm64-plugins.md).
