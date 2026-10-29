# Application log and trace analytics

Implemented 2026-10-29. Application metadata flows through **OpenTelemetry SDK → Data Prepper → OpenSearch → authenticated OpenSearch Dashboards**. The SDK sends OTLP/gRPC directly to Data Prepper; a separate Collector is not needed for this local setup. Corpus ingestion and hybrid retrieval continue to use their existing indexes.

## Components and data

- Data Prepper **2.16.0**, no-JDK archive, runs with the installed native Java 25 runtime on this Mac. The upstream archive is labelled Linux ARM64; this local Java execution was smoke-tested, not an upstream macOS support claim.
- TLS and basic authentication protect OTLP traces on **21890**, logs on **21892**, and core APIs on **4900**. Peer forwarding uses TLS mutual authentication and local-node discovery. Receivers may listen on all interfaces; authenticated TLS is required. Do not expose these ports to untrusted networks.
- `kb-logs-*`: daily structured operational logs. Discover uses `time`. `traceId` and `spanId` correlate logs with traces.
- `otel-v1-apm-span-*`: spans processed by `otel_traces`, including trace-group fields for Trace Analytics.
- `otel-v1-apm-service-map*`: service relationships processed by `service_map` (30-second aggregation windows).
- OpenSearch SQL, Observability and Job Scheduler plugins **3.9.0.0**, plus matching Observability and Security Dashboards plugins, provide the analytics screens and login.

The app records HTTP route templates/status, durations, chat question hashes, retrieval result counts/modes, model identifiers/status/retries, ingestion request IDs/stages/progress and error types. Traces cover chat, retrieval, section fetch, indexing, parsing, vision and model requests. MCP subprocesses inherit W3C trace context and use service name `kbase-mcp`; the app uses `kbase`. HTTP responses include `X-Trace-ID` when telemetry is enabled.

Telemetry deliberately excludes raw questions, document text, model response bodies, cookies, authorization headers, full URLs/query strings and exception messages/stack traces. Existing SQL diagnostics, Query Insights and slow logs are separate: their prior content-capture behavior is unchanged. The browser's existing tool trace is also separate from OpenTelemetry traces.

## Start existing services

From the project root, start **OpenSearch → Garage → Data Prepper → app**, then Dashboards:

```sh
brew services start opensearch
.venv/bin/python scripts/start_object_store.py
# Separate terminal; keep running:
bash scripts/start_data_prepper.sh
# Separate terminal; keep running:
.venv/bin/python -m kb.cli serve
brew services start opensearch-dashboards
```

`KB_TELEMETRY_ENABLED=true` is set in the private `.env`. Set it to `false` and restart the app/worker to disable export. Credentials and TLS material live in `var/telemetry/` with private permissions. `KB_TELEMETRY_CONFIG` can override the client JSON path. Do not run multiple app workers or Data Prepper instances on the same ports.

Stop the app first, then Data Prepper with Ctrl+C, then Garage/OpenSearch as needed. SDK queues flush on normal process exit; hard termination can lose telemetry.

## View logs and traces

Open [Dashboards](http://127.0.0.1:5601). Sign in as `dashboards_admin` using `DASHBOARDS_PASSWORD` from the existing private `var/opensearch-admin/dashboards.env`. This is an operator account, separate from E002/E003 employee identities. The server uses `dashboards_server`; the browser now requires login. Dashboards remains bound to loopback.

- **Discover:** select `kb-logs-*` for operations/errors or `otel-v1-apm-span-*` for spans. Start with the last 15 minutes and refresh after new app activity.
- **Observability → Trace analytics:** inspect traces, latency, errors and services. The built-in Data Prepper index names are detected automatically. Correlated logs use `kb-logs-*` and `traceId`/`spanId`/`time` fields.
- **Observability → Logs / Event analytics:** use PPL queries such as `source = \`kb-logs-*\` | stats count() by severityText`.
- Match a browser response's `X-Trace-ID` against `traceId` to follow an operation. Ingestion progress logs also carry the processing request ID.

Synthetic validation events have operation names beginning `telemetry.smoke` or `telemetry.synthetic_failure`; their deliberate error spans are not application failures. Service maps need cross-service traffic and an aggregation window, so they may lag trace records.

## Reproduce setup

Prerequisites: existing secured native OpenSearch 3.9.0, local admin certificate in `var/opensearch-admin/`, Garage, matching Dashboards, native Java, and Python requirements installed. Preserve the existing `.env`.

```sh
.venv/bin/python -m pip install -r requirements.txt
bash scripts/install_data_prepper.sh
.venv/bin/python scripts/setup_data_prepper.py
# Installs plugins and restarts OpenSearch/Dashboards; use an idle window:
bash scripts/install_observability_plugins.sh
.venv/bin/python scripts/secure_local_dashboards.py
brew services restart opensearch-dashboards
# After Dashboards is ready:
.venv/bin/python scripts/setup_telemetry_dashboards.py
```

The installer pins the Data Prepper archive's SHA-256. Server plugins are checked against their Maven Central SHA-256 files. Homebrew upgrades can replace plugins/runtime links; rerun setup only with matching versions. The setup scripts do not broaden the app's `kb_service` role. A separate `kb_telemetry_writer` can manage/read/write only telemetry indexes; initialization additionally needs cluster monitoring/template operations and global **alias metadata** lookup, not corpus document access. The existing admin certificate is used only for provisioning.

Private generated `pipelines.yaml` and `data-prepper-config.yaml` are JSON syntax, which is valid YAML. `scripts/setup_data_prepper.py` is the reproducible pipeline definition; generated files contain credentials and must not be committed. TLS certificates expire after 365 days; regenerate the certificate/key and rerun provisioning, then restart exporters and Data Prepper when rotating.

## Validation and troubleshooting

```sh
KB_TELEMETRY_ENABLED=false .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/check_telemetry.py
```

The first command checks content exclusion, route templating and propagation locally. The second sends synthetic successful/error spans and logs (including a second process), then reads only their trace ID from OpenSearch. It makes no model calls and does not ingest source documents. A successful run prints trace ID and counts.

Check Data Prepper terminal output and `var/telemetry/*-dlq.json` for sink failures. In this session stdout is captured in `var/telemetry/runtime.log`; the app output is in `var/telemetry/app-runtime.log`. The launcher normally prints to its terminal. Invalid credentials/TLS fail export; OpenSearch authorization failures appear in the sink logs. A missing Data Prepper process does not intentionally fail chat or ingestion: SDK exports run in background with bounded queues and short timeouts.

This is development observability, not a durable audit system. SDK and Data Prepper input buffers are in memory; outages, saturation or hard restarts can lose telemetry. Sinks retry five times and preserve exhausted writes in local DLQ files; those files need operator review/replay. Automatic telemetry retention/deletion and DLQ replay are **not configured**; monitor disk usage and define a retention policy before sustained deployment. Logs may duplicate after ambiguous retries. Full exception diagnostics remain in the app's existing restricted ingestion monitor. No historical logs or traces were backfilled.

Design references: [Data Prepper](https://docs.opensearch.org/latest/data-prepper/), [trace analytics pipelines](https://docs.opensearch.org/latest/data-prepper/common-use-cases/trace-analytics/), and [OpenSearch sink configuration](https://docs.opensearch.org/latest/data-prepper/pipelines/configuration/sinks/opensearch/).
