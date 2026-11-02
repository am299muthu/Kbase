# Knowledge solution data flow

Implementation snapshot: 30 October 2026. Read the ingestion and question-answering
lanes left to right. Debugging panels are parallel outputs, not a sequential pipeline.

- [PNG diagram](data-flow.png)
- [Editable SVG](data-flow.svg)
- Regenerate with `.venv/bin/python scripts/render_data_flow.py`.

Source: `kb/pipeline.py`, `kb/pdf_hybrid.py`, `kb/providers.py`, `kb/guardrails.py`,
`kb/search.py`, `kb/agent.py`, `kb/app.py`, and `profiles/pdf-hybrid.env`.
This describes code and configuration, not a fresh deployment-health attestation.

## Important branches

- Independent checker approval precedes the worker's initial ingestion. The worker
  validates source hash, metadata binding, scope and current entitlements again.
- Geometry snapshots are persisted after parsing, before visual model calls.
  The inspector combines this snapshot with the original retained in Garage.
  Older runs fall back to visual-crop geometry in their stored parsed JSON.
- Vision availability errors can switch to the configured fallback. Invalid
  returned structure still fails. Text PII guardrails have no fallback today.
- Before embedding, locally masked text passes the inference PII check. The
  approved embedding model receives validated text. Required check failures
  stop publication and leave any prior published generation intact.
- New index writes require real text embeddings. Search can still read legacy
  BM25 generations; image-vector search is disabled in the pdf-hybrid profile.
- Answer generation uses currently entitled evidence through MCP retrieval tools;
  citations, grounding, PII handling and final access checks precede release.
- Retry without review is conditional on the original approval remaining valid.
  Changed bindings require a fresh maker request and checker review.
- PDF inspection reads unredacted original pages locally, so it requires both a
  permitted folder role and unmasked-content entitlement.
- Query Insights and slowlogs measure OpenSearch work, not full LLM/chat latency.
