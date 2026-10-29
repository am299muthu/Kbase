# Current implementation gaps

Latest status: 2026-10-27. See [governed rollout and exact verification](governance-and-activation.md).

## Implemented and live-verified

- Native OpenSearch 3.9.0 with verified local-CA HTTPS, Security/k-NN 3.9.0.0 and narrowly scoped service permissions.
- Current published corpus: **107 chunks / 10 indexed documents**; the database also contains other document records (12 total at the latest check). SQLite holds staff/access/structured controls and content-integrity manifests, not a searchable text duplicate in OpenSearch mode.
- Files now persist in native loopback **Garage S3**. The explicitly authorized migration copied and hash-verified **61 objects / 5,025,128 bytes** without deleting originals. No PostgreSQL migration is required for staff/access controls.
- Local hybrid PDF extraction: pypdf electronic text, pdfplumber ruled grids, Docling object crops. Scanned/sparse/raster-dominant pages have a configured VL-reading path, tested offline with mocked generated readings; Tesseract is not required for that path.
- Inference-assisted PII redaction, separate grounding verdicts, citation validation and final entitlement rechecks are implemented and synthetic/mocked-verified. They are not live corpus accuracy/privacy proof.
- Maker/checker requests bind source hash/version/metadata and processing/transfer scope. Separate authenticated checker review is required; CLI auto-approval is disabled. Exact-ID, idempotent reprocessing stages new generations and preserves published content on failure. Positive/negative HTTP and synthetic pipeline checks passed.
- Created compatible **abc-kb-curated-multimodal-v2**: separate text/image vector fields, model identities and generation filters. Its exact service-role scope was explicitly approved. Actual filtered BM25 and both native k-NN query shapes passed on the **empty** index, without external model calls.
- Durable `local`/pending `hybrid` profiles and fail-closed hybrid startup/readiness checks. The working app was restarted without ad-hoc overrides and verified healthy with SQLite controls, S3 files, six MCP tools and cited synthetic policy chat.

## Remaining gaps

| Priority | Gap | Current behaviour / next work |
|---|---|---|
| High | Real external processing | OpenRouter real-corpus transfer was denied in Auto permission mode and was not retried. Direct permission review is required outside Auto. Rotate the chat-exposed key privately. Local S3 copying does not authorize external text/pixel transfer. |
| High | Pilot enrichment | Exactly three existing pilot PDFs have pending E003 maker requests for the hybrid target. E002 must review the source/scope independently. Existing PDFs remain unchanged until approved processing actually succeeds. |
| High | Live hybrid corpus | Running retrieval is still BM25/extractive. The target multimodal index is compatible but empty. Populate the three enriched PDFs and all other current indexed documents with approved real vectors, validate every manifest/model/vector and ACL/citation behaviour, then activate during a controlled maintenance window. Legacy approvals do not authorize new external embeddings. |
| High | Model availability and accuracy | Earlier small live synthetic probes passed, but full synthetic VL ingestion hit HTTP 429. New PII/grounding guards and scan reading have not been validated on the real corpus. Model verdicts can miss PII or unsupported claims; no zero-cost/capacity/quality guarantee is claimed. |
| High | Pixel confidentiality | Text masking does not redact image pixels. Scan tiles can collectively contain whole-page content, explicitly included in checker scope. Image transfer requires separate approval/gates; no image anonymization guarantee exists. |
| Critical before deployment | Enterprise identity and confidentiality | Demo accounts/shared password are not SSO/MFA. Service/admin access bypasses employee application ACLs; provision enterprise identity, private networking, lifecycle controls and appropriate direct-index restrictions. |
| High | Storage production controls | Local Garage is single-node loopback HTTP with application-serialized writes, not verified server-atomic conditional PUT, distributed locking, HA, WORM or immutable version retention. Provision approved TLS/encryption, backup/restore and retention controls before deployment. Originals are retained, but post-cutover S3 writes are not automatically mirrored to local rollback files. |
| High | Governance depth and parsing isolation | Maker/checker enforcement is implemented. External-control evidence remains a single-checker attestation, not independent legal/risk/privacy identities. Malware scanning and OS-level parser isolation remain unprovisioned. |
| Medium | Agent workflows/connectors | MCP tools are real but routing is code-planned. Access requests are audit events, not a completed approval/grant inbox. Weekly metadata/dbt/ERD/lineage and licensed market-data connectors are absent. |
| Medium | Reliability and scale | No durable broker/DLQ, verified HA/DR, TB-scale load evidence or scalable generation-publication read model. Generation terms filters suit the prototype, not unbounded collections. SQLite controls are an accepted design choice; concurrency/backups still need validation for intended load. |
| Medium | Evaluation and deployment | Full 50-case semantic/security evaluation and real scan/table/chart interpretation evaluation are outstanding. No production deployment, full-quality score or semantic entailment guarantee is claimed. |

## Immediate rollout order

1. Complete direct external-transfer permission review and independent checker review of the three pilot target-profile requests.
2. In a controlled maintenance window, reprocess only those exact PDFs; separately obtain current transfer approvals and populate the remaining published corpus without importing additional sources.
3. Verify real model output/vector coverage, access denials, citations and failure handling. `profile-check` must pass for the hybrid target before serving it.
4. Select the hybrid profile durably only after success. Retain old index/objects and consistent control backups for reviewed rollback.

Python/dependency checks passed. JavaScript syntax was verified with native JavaScriptCore; the unrelated Homebrew Node/simdjson runtime problem was not repaired.
