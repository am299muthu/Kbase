# Governed multimodal rollout — current status

## Implemented and verified on 2026-10-27

- **SQLite remains the control store** for staff, access, structured metadata, sessions, approval requests, generation pointers and audit events. PostgreSQL is optional, not a requirement for this design.
- **File layers now use native local S3:** Garage 2.5.0 ARM64, installed from the explicitly authorized Homebrew bottle. S3/RPC/admin listen only on 127.0.0.1 (3900/3901/3903). Private configuration and credentials are under `var/garage`; `.env` remains mode 0600. The `kb-objects` runtime key has only read/write on `abc-knowledge`, not bucket-owner permissions.
- Copied and SHA-256 verified **61 objects / 5,025,128 bytes**, covering existing staging/raw/raw-parsed/curated/assets layers. Original files were not deleted. The completed manifest/receipt are `var/garage/migration-20261027.json` and `var/garage/migration-20261027.receipt.jsonl`. SQLite was backed up before additive governance tables were initialized.
- Created **abc-kb-curated-multimodal-v2**, with BM25 fields, separate 2048-dimensional text/image Lucene HNSW cosine fields, model identities and generation filtering. The explicitly approved existing service role now covers this exact index. Native BM25 and both filtered k-NN query shapes passed against the **empty** target without model calls or document writes.
- Evidence grounding and citation validation are enforced. PII detection/redaction was removed from the active pipeline on 2026-11-06.
- Scanned/sparse/raster-dominant PDF pages use the configured **VL model**, not a Tesseract prerequisite. Local rendering creates four bounded overlapping regions; their combined coverage can include the whole page and is explicitly listed in checker transfer scope. Model-read content remains generated/unverified, with page/bbox/hash lineage. Without VL enabled, ingestion refuses unreadable placeholders. Offline actual scanned-PDF parsing and mocked VL enrichment passed; electronic text/grid parsing still passed.

## Maker–checker enforcement

`ProcessingRequest` binds the source hash, version, metadata and exact processing destination/model/parser/transfer scope. Active entitled makers submit; a different active entitled checker reviews through authenticated HTTP. Owners cannot approve their own submissions. The checker echoes the displayed request ID and scope digest. Source or runtime changes invalidate processing approval. Approval/entitlements are checked before every outbound ingestion/reindex model request, including retries.

Maker metadata cannot assert legal/risk/privacy/licence approval. External-domain sources require explicit checker evidence references. These are prototype attestations, not proof of independent legal/risk organization approvals.

CLI `--auto-approve`/`--checker` shortcuts now fail before source ingestion. `init --demo-documents` stages synthetic fixtures for review rather than impersonating checkers. Interrupted work fails for fresh review; recovery does not resurrect an approval.

Reprocessing is exact-ID and idempotent. New generations use immutable object/chunk identities; current SQL manifests/pointers change only after indexing succeeds. Failed replacements preserve the published generation. Staged/obsolete indexed generations are excluded from hybrid rank fusion and SQL rechecks remain authoritative. Cross-index cutovers still require a controlled maintenance window: do not serve the old index while publishing SQL manifests for a different target index.

### Pilot requests

Exactly the three existing `pdf-pilot` PDFs have hybrid-target requests, submitted for maker **E003** with idempotency key `pilot-multimodal-v2-20261027`. **All three were subsequently approved by independently authenticated E002; the last verified state is approved, not processed.**

| Document ID | Request ID |
|---|---|
| `0f9e27ece89d8a36cbf2401042108a1a` | `d090e905c9721294ea53d1d8b103e487` |
| `602897f0f4529fa74f6e0aaf6dcd0c09` | `d4ef81ccde0f0eb33c344f2f88427e88` |
| `cf79d28e0f1290c5ea51ba28e5203034` | `d1ccab3bfa6b20caf8ee117b4d80c790` |

E002 completed the captured-source/scope reviews through **Approvals**; do not request repeat approvals for these unchanged requests. The working local worker ignores requests for a different profile. These application approvals do not clear the separate direct external-transfer permission boundary. Existing grants are unchanged. See [architecture and access-control boundaries](architecture-and-access-control.md).

## Durable restart profiles

Private `.env` selects `KB_RUNTIME_PROFILE=local`. Named profiles contain only routing/gates, never credentials. They override corresponding individual environment flags; select the intended profile explicitly. Standalone diagnostics honor the same profile.

Start native storage in one terminal if it is not already running:

```sh
.venv/bin/python scripts/start_object_store.py
```

Start one app instance in another terminal:

```sh
.venv/bin/python -m kb.cli serve
```

No ad-hoc safe overrides are needed. The verified running profile is **SQLite controls + local S3 files + BM25/extractive**, external models disabled. Plain restart, app/OpenSearch health, six MCP tools and cited synthetic E005 policy chat passed. The current database has **12 document records / 107 published chunks** (not a claim that all 12 records are indexed).

`profiles/hybrid.env` is a **pending** profile. `serve` refuses hybrid startup until `profile-check` verifies storage, compatible mappings, all current indexed documents' scope-bound approvals, hashes and real text/image vector fields. A false readiness check exits 2. Empty indexes or configuration flags alone cannot claim readiness.

## What still blocks real enrichment and hybrid activation

1. Real-corpus OpenRouter transfer was denied in Auto permission mode. **No alternate transfer path was attempted.** Switch out of Auto so the transfer can receive direct permission review. Local S3 copying is not approval to send text/pixels to OpenRouter.
2. The three pilot checker approvals are recorded. Processing must run under their exact approved hybrid scope, not the serving local profile; changed source/scope or revoked entitlements still invalidate execution.
3. The remaining indexed corpus also needs approved embeddings and verified population in the target. Do not silently drop it by activating an index containing only three PDFs. Legacy approvals do not authorize new external processing. `reindex` uses current curated generations and cannot discover new PDF objects; the three PDFs need actual reprocessing.
4. Perform cross-index reprocessing/reindexing during a controlled maintenance window with stopped writers and a consistent SQLite/object backup. Validate all current manifests, ACL denials, citations, generated/source distinctions and vector/model fields before selecting the hybrid profile durably and starting one app instance. Retain old objects/index and backups; rollback is not deletion.
5. Earlier full synthetic VL ingestion hit HTTP 429; live availability/quality is still unproven. No paid fallback was introduced. Rotate the chat-exposed OpenRouter key privately.

No real PDF enrichment/reprocessing or live hybrid corpus is claimed by this update.

## Remaining security/operational limits

External model transfers require approved scope. Grounding verdicts are fallible and need evaluation.

Garage accepted conditional PUTs without enforcing `If-None-Match` in the actual probe. This host therefore explicitly uses **serialized-local** writes: a shared project-DATA_DIR lock and existence check coordinate application/migration writers, with streamed hash verification. This is not server-atomic, a distributed lock, protection against another credential holder, or a production immutability guarantee. Keep other writers stopped for migration. The service is loopback HTTP for approved local development, not a remotely deployable TLS/HA/WORM service. See [storage details](object-storage.md).

Demo authentication, enterprise SSO/MFA, immutable audit, parser OS isolation/malware scanning, robust HA/DR and full semantic/security evaluation remain separate deployment work.
