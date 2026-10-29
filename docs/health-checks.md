# Standalone service health checks

Run from the project root using the existing virtual environment:

```sh
.venv/bin/python scripts/health_check.py
.venv/bin/python scripts/health_check.py --json
```

The script is independent of app startup: it does not import the app, create tables/indexes, ingest documents, change settings, restart services, or read document content. It reads project `.env` (or `--env-file PATH`) with process environment taking precedence. Dotenv `${...}` interpolation is intentionally disabled. Use literal values or process environment variables. No new Python dependencies are required beyond `requirements.txt`.

## Checks

| Check | What it verifies |
|---|---|
| `app` | HTTP `/health`, overall status and reported dependency statuses |
| `opensearch` | Server root/version metadata, green cluster health, and HEAD existence/access for the exact configured index; no search/document reads |
| `postgres` | Authenticated connection and `SELECT 1`; no application table reads/writes |
| `openrouter-embedding` | Optional real API request with one fixed synthetic string; validates a finite, nonzero embedding and configured dimension |
| `openrouter-inference` | Optional real API request with a fixed synthetic prompt; validates nonempty assistant text, not answer quality |
| `openrouter-image-embedding` | Optional synthetic shape image sent to `OPENROUTER_IMAGE_EMBEDDING_MODEL`; validates its separate `IMAGE_EMBEDDING_DIMENSIONS` |
| `openrouter-vision` | Optional synthetic shape image sent to `OPENROUTER_VISION_MODEL`; checks returned assistant text |

Select only the desired checks:

```sh
.venv/bin/python scripts/health_check.py --checks app opensearch
.venv/bin/python scripts/health_check.py --checks postgres
.venv/bin/python scripts/health_check.py --checks app --app-url http://127.0.0.1:8000 --timeout 10
```

Results include a status, safe diagnostic detail, and elapsed milliseconds. JSON additionally includes a UTC timestamp and exit code. Raw API bodies, model text/vectors, database URLs, passwords and keys are not printed. Driver errors are summarized without their sensitive exception messages. Standard HTTP proxy environment variables are not used; endpoints are contacted directly, and redirects are not followed.

### Exit codes

- **0:** All selected checks passed.
- **1:** At least one selected check failed.
- **2:** No failures, but at least one check was skipped or unconfigured: **incomplete**, not fully healthy.

A yellow/red OpenSearch cluster is reported as failure/degraded. Timeouts are per HTTP request or database connection/statement, not a total runtime deadline. App `/health` performs its own dependency checks and can exceed a short caller timeout.

## PostgreSQL configuration

Uses `POSTGRES_HEALTH_URL` if nonempty, otherwise the app's `DATABASE_URL` when it is PostgreSQL. If the app uses SQLite and no separate PostgreSQL DSN is configured, PostgreSQL is explicitly **skipped**, not treated as healthy. This checks the configured database/user, not just an open port or a running Homebrew service.

Set privately in `.env`, using your existing authorized database/user and URL-encoded password when needed:

```dotenv
POSTGRES_HEALTH_URL=postgresql+psycopg://EXISTING_USER:URL_ENCODED_PASSWORD@127.0.0.1:5432/EXISTING_DATABASE
```

Do not paste credentials into chat or put DSNs in command-line arguments. Configuring this variable does not migrate the app or change its `DATABASE_URL`. Remote database TLS parameters, such as `sslmode=verify-full` and `sslrootcert`, should be supplied in the DSN according to your deployment policy. The diagnostic itself does not provision users, grants or certificates.

## OpenRouter model probes

Default execution does **not** invoke models. Configure a rotated API key and exact model IDs privately first:

```dotenv
OPENROUTER_API_KEY=YOUR_ROTATED_PRIVATE_KEY
OPENROUTER_MODEL=YOUR_VERIFIED_INFERENCE_MODEL_ID
OPENROUTER_EMBEDDING_MODEL=YOUR_VERIFIED_EMBEDDING_MODEL_ID
EMBEDDING_DIMENSIONS=YOUR_VERIFIED_DIMENSION
```

Then explicitly authorize synthetic API requests:

```sh
.venv/bin/python scripts/health_check.py --probe-models
# Or only the two model checks:
.venv/bin/python scripts/health_check.py --checks openrouter-embedding openrouter-inference --probe-models --json
```

`--probe-models` authorizes these diagnostic requests only. It does **not** change `EXTERNAL_MODELS_ENABLED`, activate inference/embeddings in the app, reindex documents, or authorize transferring corpus/query text. No actual document/user text is sent. The probes contact only the official `https://openrouter.ai` origin and documented API paths, with HTTPS verification enabled.

Requests specify provider prompt/completion price ceilings of zero and disable provider fallbacks. There are no retries or paid-model fallbacks. These are request constraints, **not an independently verified guarantee of zero charges**, model availability or provider policy. Treat the flag as authorization for potentially billable API calls; review provider pricing first. A rejected zero-price route, missing model, auth failure, exhausted quota or unsupported parameters is reported as failure, without retrying a more expensive route. Missing key/model or an omitted probe flag is reported as skipped.

Inference uses a 32-token output cap. A reasoning model can exhaust that cap without producing assistant text; the check then fails, without implying that all requests to that model would fail. A successful probe demonstrates a usable response at that time, not sustained capacity, quality, semantic groundedness, or production readiness.

Official API references: [embeddings](https://openrouter.ai/docs/api/api-reference/embeddings), [provider routing and price limits](https://openrouter.ai/docs/guides/routing/provider-selection).

The diagnostic now also supports separate image-embedding and vision-language models, selected only by `OPENROUTER_IMAGE_EMBEDDING_MODEL` / `IMAGE_EMBEDDING_DIMENSIONS` and `OPENROUTER_VISION_MODEL`. Default checks still make no model calls; `--probe-models` can now invoke four model checks, so select a subset to conserve quotas. A separate image-model synthetic probe and a small VL probe passed, but a subsequent full VL ingestion attempt returned **HTTP 429**; do not treat initial success as ongoing free-tier availability. See [multimodal verification and limits](pdf-multimodal-pipeline.md).

```sh
.venv/bin/python scripts/health_check.py --checks openrouter-image-embedding openrouter-vision --probe-models --timeout 90 --json
```


## Current local verification

On 2026-10-26, actual default diagnostics passed **app** and **OpenSearch**. PostgreSQL was skipped because the application uses SQLite and no separate health DSN was configured. Both OpenRouter checks were skipped because the configured key is blank. Overall default exit code: **2 (incomplete)**. No model request was made and no database credentials were guessed.

Synthetic/mocked response checks additionally verify failure reporting and model-response validation; they are not live provider/PostgreSQL integration evidence.

### Subsequent OpenRouter verification

After the user supplied a key and authorized model use, live synthetic probes passed both configured free-tier models. The embedding model returned **2048 dimensions**, which is now explicitly set alongside both model IDs in private `.env`. The standalone script's two model checks returned exit code **0** with `--probe-models --timeout 90`. Actual application embedding and structured-inference adapters also passed synthetic checks. No corpus content was used for those probes. Corpus embedding/reindexing was separately blocked by Auto permission mode and remains incomplete; see [activation status](openrouter-activation.md). The earlier missing-key result above is historical, not the current model-probe status.
