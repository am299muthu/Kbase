# Native infrastructure setup

> **2026-10-27 local deployment:** Native Garage 2.5.0 now provides loopback S3 for files; SQLite remains the accepted staff/access/control database. Start storage with `.venv/bin/python scripts/start_object_store.py`, then one app with `.venv/bin/python -m kb.cli serve`. Existing private configuration/credentials must not be replaced by examples below. See [current rollout and limitations](governance-and-activation.md) and [object storage](object-storage.md).

The application does **not** require Docker. The current local profile uses native OpenSearch for document search, SQLite for control records/lineage manifests, local object files and no external model calls. Infrastructure mode additionally requires PostgreSQL and S3 and never falls back to local adapters. See [current gaps](current-gaps.md) for the distinction between verified BM25 retrieval and still-unprovisioned model/parser/production services.

## OpenSearch + Dashboards (no Docker)

Checked the official downloads page and Homebrew metadata on 2026-10-26: OpenSearch and Homebrew Dashboards versions are 3.9.0. Official downloadable server bundles primarily target Linux/Windows; macOS has a native Homebrew package.

```sh
bash scripts/install-native-opensearch.sh
```

This installs packages but does not start services or overwrite configuration. Review the existing configuration in `$(brew --prefix)/etc/opensearch/` and the equivalent Dashboards directory. Bind both services to loopback for development; configure credentials, CA-signed TLS and the Dashboards server connection consistently. Start them explicitly:

```sh
brew services start opensearch
brew services start opensearch-dashboards
```

Linux: use a signed DEB or RPM from https://opensearch.org/downloads/ and follow the linked native installation guide. Choose matching server/Dashboards versions and supported Java. Do not disable authentication on publicly reachable interfaces. The app ships a one-shard/zero-replica **development** k-NN mapping, not an HA production cluster. Set replicas >=1 with at least three cluster-manager eligible nodes for production, use role-scoped service accounts, and provision snapshots. Dashboards, Observability, Data Prepper and ML/agent plugins are optional operational products, **not installed or configured by the app**. The current ingestion worker and real MCP tool server supply ingestion/orchestration; there is no fake plugin integration.

## Current ARM64 plugin profile

Security and k-NN 3.9.0.0 are now installed and locally verified. OpenSearch uses **https://127.0.0.1:9200**, a private local CA, and an index-scoped service account. The app's existing `.env` includes credentials and `OPENSEARCH_CA_CERTS`; preserve it. Lucene float32 HNSW passed a synthetic filtered-vector check; native Faiss/NMSLIB and quantized paths are not provisioned. See [installation details, dependency fix and operational boundaries](opensearch-arm64-plugins.md). Dashboards was not reconfigured or started.

## PostgreSQL

Use an existing reviewed local PostgreSQL installation. For a fresh developer instance, create a non-superuser login and dedicated database using `createuser --pwprompt kb` and `createdb --owner=kb kbase` (authenticate as your local administrator). Set `DATABASE_URL=postgresql+psycopg://kb:YOUR_URL_ENCODED_PASSWORD@localhost:5432/kbase`. Do not use the example password. The prototype bootstraps schema via SQLAlchemy; production requires reviewed migrations, backups, connection pooling and separate migration/application roles.

## S3 / LocalStack

Point `S3_ENDPOINT_URL` at an **already running** LocalStack S3 endpoint (normally `http://localhost:4566`). LocalStack itself is not bundled or started; its usual local runtime uses Docker, so do not assume its Python CLI supplies a container-free emulator. A remote approved LocalStack endpoint or another S3-compatible store may be used when Docker is prohibited. For AWS S3, leave the endpoint blank, use appropriate IAM credentials, and provision encryption/KMS, Object Lock, lifecycle and retention policies separately. The app enables bucket versioning/public-access blocking on buckets it creates; it does not claim KMS or WORM configuration is present.

## Infrastructure configuration

In `.env` set:

```dotenv
APP_MODE=infrastructure
DATABASE_URL=postgresql+psycopg://kb:YOUR_URL_ENCODED_PASSWORD@localhost:5432/kbase
SEARCH_BACKEND=opensearch
STORAGE_BACKEND=s3
OPENSEARCH_URL=https://localhost:9200
OPENSEARCH_USERNAME=kb_service
OPENSEARCH_PASSWORD=YOUR_SERVICE_PASSWORD
OPENSEARCH_VERIFY_CERTS=true
OPENSEARCH_CA_CERTS=/path/to/trusted-ca.pem
S3_ENDPOINT_URL=http://localhost:4566
DEMO_PASSWORD=YOUR_UNIQUE_LOCAL_FIXTURE_PASSWORD
EMBEDDING_BACKEND=hash
```

`hash` embeddings are deterministic smoke-test vectors only. For semantic retrieval configure a reviewed embedding provider. Infrastructure output to PII-masked employees fails closed until local spaCy `en_core_web_sm` is provisioned (`pip install '.[pii]'`, then provision the model from an approved source). NER plus rules is still not a compliance guarantee. Establish a validated PII evaluation set and human risk sign-off before production.

```sh
.venv/bin/python -m kb.cli init --demo-documents
.venv/bin/python -m kb.cli serve
```

OpenSearch retrieval is BM25 by default (`OPENSEARCH_VECTOR_SEARCH=false`). The installed native server now has Security/TLS and locally verified Lucene float32 k-NN; enable filtered Lucene HNSW/RRF for KB retrieval only after provisioning a real embedding model and fresh vector index, with an explicitly authorized service-role update for that index. Current SQL grants prefilter retrieval; SQL document/chunk manifests validate candidate integrity and current status/retention before release. All document versions remain traceable. Seven-year expiry is enforced at retrieval even before `archive` housekeeping runs. Rebuild into a **new index** when changing embedding model or dimensions; do not mix incompatible vectors. Local OpenSearch BM25 migration/retrieval has been live-verified (10 documents / 107 chunks); the full PostgreSQL/S3/semantic path has not.

## External model processing

No external calls occur by default. Set `EXTERNAL_MODELS_ENABLED=true` **only after approving external processing**, set `LLM_PROVIDER=openrouter`, and enter a freshly rotated `OPENROUTER_API_KEY`. Free model IDs in the requirements are configuration defaults, **not verified availability guarantees**; inspect OpenRouter's model catalogue and quotas. Configure `EMBEDDING_BACKEND=openrouter` and correct `EMBEDDING_DIMENSIONS` if desired, then use a fresh index and reingest. HTTP errors fail clearly; there is no paid-model fallback.

Egress text is locally PII-masked by default, including embeddings. `ALLOW_UNMASKED_EXTERNAL=true` is a separate opt-in for approved data transfers. Browser PII visibility never implicitly grants third-party transfer permission. Raw/curated storage retains original content. Do not use rule-only demo masking for actual confidential banking records.
