> Scope update, 2026-11-06: PII detection/redaction and PII visibility gates have been removed from the active pipeline. References to those checks below describe historical runs. Folder authorization and evidence grounding remain active.

# OpenRouter activation — prepared, not live

> **2026-10-27 update:** A fresh compatible `abc-kb-curated-multimodal-v2` and its explicitly approved service scope are now provisioned. Files use native local S3, staff/access controls remain SQLite, and the three pilot target-profile requests await independent checker review. Durable profiles replace ad-hoc restart flags. Real OpenRouter transfer and hybrid corpus population remain blocked/incomplete. Follow [the current governed rollout](governance-and-activation.md); the older commands below are historical, not the current activation procedure.

> Subsequent PDF/multimodal work adds separate image vectors and model-identity mapping checks. The earlier text-only `abc-kb-curated-openrouter-v1` remains empty and unchanged, but its old mapping does not satisfy the new fingerprint checks. Do not use the earlier sequence below as a ready multimodal rollout: provision a fresh compatible index with explicitly approved service-role scope, and reprocess originals to obtain new visual/grid chunks. See [current pipeline configuration and verification](pdf-multimodal-pipeline.md).

## Verified on 2026-10-26

The user authorized OpenRouter inference/embeddings and a new vector index. Public model catalogues returned the exact configured IDs with zero prompt/completion token prices. Synthetic API probes and the application's actual adapters succeeded:

| Adapter | Model | Result |
|---|---|---|
| Inference | `nvidia/nemotron-3-ultra-550b-a55b:free` | Assistant text and structured synthetic claims/citation references returned |
| Embeddings | `nvidia/nemotron-3.5-embed-1b:free` | Valid **2048-dimensional** vectors returned |

The standalone diagnostic also passed both model checks with exit code 0. These are synthetic functional checks, not evidence of corpus quality, semantic entailment or ongoing availability/quota guarantees.

The private `.env` now contains these explicit model IDs and `EMBEDDING_DIMENSIONS=2048`; credentials were not printed. The application gateway now requests zero prompt/completion price ceilings with provider fallbacks disabled, consistent with the standalone diagnostic. This does not independently guarantee provider pricing or free-tier capacity. No paid fallback is configured.

## Prepared infrastructure

- Created **abc-kb-curated-openrouter-v1** with Lucene float32 HNSW, cosine similarity and dimension 2048.
- User explicitly authorized the existing `kb_service` role's KB read/write/mapping permissions on this exact index, alongside **abc-kb-curated-v1**. No employee folder grants changed.
- Original BM25 index remains intact with **107 chunks**.

## Blocking boundary

Auto permission mode denied the approved-content reindex because it transfers real source content to an external service. **The denied command did not execute and was not retried through another path.** Switch out of Auto permission mode so the transfer can receive a direct permission review before proceeding.

The running app remains BM25/extractive with external processing disabled. The private `.env` currently selects the OpenRouter adapters but keeps the global egress gate and vector search disabled:

```dotenv
EXTERNAL_MODELS_ENABLED=false
LLM_PROVIDER=openrouter
EMBEDDING_BACKEND=openrouter
OPENSEARCH_VECTOR_SEARCH=false
OPENSEARCH_INDEX=abc-kb-curated-v1
ALLOW_UNMASKED_EXTERNAL=false
```

Those provider selections were preserved; they do not override the global gate. Restarting with this incomplete profile would not activate working OpenRouter answers. The earlier vector index remains unpopulated and incompatible with the newer model-identity checks noted above. Do not switch the app to it or silently enable external inference as another route for blocked document transfer.

After local hybrid-PDF provisioning, the app was restarted with explicit process overrides to keep the working no-egress profile, without rewriting those private `.env` selections:

```sh
LLM_PROVIDER=extractive EXTERNAL_MODELS_ENABLED=false \
PDF_VISION_ENABLED=false ALLOW_EXTERNAL_VISION_IMAGES=false \
IMAGE_EMBEDDINGS_ENABLED=false OPENSEARCH_VECTOR_SEARCH=false \
.venv/bin/python -m kb.cli serve
```

Stop the existing app instance before starting a replacement. App/OpenSearch diagnostics and synthetic E005 policy chat passed after this restart; real PDFs were not reprocessed.

## Completion sequence after permission review

Keep the live profile unchanged while building the new index in a separate operator process:

```sh
EXTERNAL_MODELS_ENABLED=true \
LLM_PROVIDER=openrouter \
EMBEDDING_BACKEND=openrouter \
EMBEDDING_DIMENSIONS=2048 \
OPENSEARCH_VECTOR_SEARCH=true \
OPENSEARCH_INDEX=abc-kb-curated-openrouter-v1 \
.venv/bin/python -m kb.cli reindex --limit 10
```

The intended scope is the currently approved **10 documents / 107 chunks**, not additional source imports. Reconfirm the inventory if it has changed. Outbound embedding text is locally masked, but current rules-only masking can miss sensitive information. The original brief's exposed key and the subsequently chat-shared key should be rotated; set replacement credentials privately, never in chat or command arguments.

Require zero reindex errors, expected chunk counts, correct vector/model metadata, and authorized/unauthorized retrieval validation before updating `.env` to the flags above. Keep `ALLOW_UNMASKED_EXTERNAL=false`. If validation fails, leave the original BM25 profile active and report the failure; do not delete either index or bypass the approval boundary.

Only after validation, stop the existing app process, update the private environment and restart one app instance. Verify live `/health` reports hybrid retrieval/embeddings and OpenRouter inference, then exercise cited synthetic-policy HTTP/MCP chat. Preserve the original index for rollback; rollback consists of restoring the original provider/index flags and restarting the app, not deleting data.
