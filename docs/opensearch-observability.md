# OpenSearch Dashboards and query-latency monitoring

Updated 2026-10-28. Local, native (no Docker) macOS setup for viewing OpenSearch data and the latency of the queries the knowledge app sends. This is a development aid, not a production monitoring design.

## What is installed

| Component | Version / source | Purpose |
|---|---|---|
| `opensearch` (Homebrew) | 3.9.0, with Security and k-NN plugins | Search backend (HTTPS on 127.0.0.1:9200) |
| `opensearch-dashboards` (Homebrew) | 3.9.0 | UI on http://localhost:5601 |
| `node` (Homebrew, reinstalled) | 26.12.1 | Dashboards runtime. The previous Node pointed at a missing `libsimdjson.33`. |
| `query-insights` OpenSearch plugin | 3.9.0.0 from Maven Central (`org.opensearch.plugin:query-insights`), SHA-256 verified | Records top-N queries by latency |
| `queryInsightsDashboards` Dashboards plugin | 3.9.0, extracted from the official `opensearch-dashboards-3.9.0-linux-arm64` bundle | "Query Insights" UI page |

The `query-insights` name is not in the built-in `opensearch-plugin install` list for the Homebrew build; it is installed from a verified file. The Homebrew Dashboards build ships **without** the Security Dashboards plugin (no login page). The official bundle contains `securityDashboards` if a login is later required.

## Settings changed

OpenSearch security (created with the admin client certificate in `var/opensearch-admin/`):

| Item | Value |
|---|---|
| Internal user `dashboards_admin` | Mapped to role `all_access`. Used by Dashboards for every request. |
| Internal user `dashboards_server` | Mapped to `kibana_server`. Created but not used by the current config. |
| Passwords | Randomly generated, stored only in `var/opensearch-admin/dashboards.env` (mode 600) and the Dashboards config |

Dashboards (`$(brew --prefix)/etc/opensearch-dashboards/opensearch_dashboards.yml`, mode 600; original kept as `.bak-<date>`):

```yaml
server.host: "127.0.0.1"
server.port: 5601
opensearch.hosts: ["https://127.0.0.1:9200"]
opensearch.ssl.verificationMode: full
opensearch.ssl.certificateAuthorities: ["/opt/homebrew/etc/opensearch/kbase-tls/root-ca.pem"]
opensearch.username: "dashboards_admin"
opensearch.password: "<from var/opensearch-admin/dashboards.env>"
```

Prefer the script over copying this block, so the password is never typed or committed.

Filesystem: `mkdir $(brew --prefix)/var/run` (PID file); the plugin copied to `$(brew --prefix)/Cellar/opensearch-dashboards/3.9.0/plugins/`, which Homebrew exposes under `$(brew --prefix)/var/opensearch-dashboards/plugins/`; and a symlink `var/opensearch-dashboards/node_modules` to the Cellar `node_modules`, so the plugin can load `@osd/config-schema`.

Cluster settings (persistent): `search.insights.top_queries.latency.enabled=true`, `window_size=5m`, `top_n_size=50`, `exporter.type=local_index`, `exporter.delete_after_days=7`.

Index settings on `abc-kb-curated-multimodal-v2` and `abc-kb-curated-v1`: search slow log at `0ms`, level `trace` (raw query capture; see below).

## Reproduce from scratch

Prerequisites: Homebrew OpenSearch and Dashboards 3.9.0 installed, OpenSearch running with Security and the local CA (`kbase-tls/root-ca.pem`), and the admin client certificate pair in `var/opensearch-admin/` (see [infrastructure](infrastructure.md)).

```sh
bash scripts/setup_opensearch_observability.sh
```

The script reinstalls Node if needed, installs and checksum-verifies the plugin, restarts OpenSearch, creates the Dashboards users and config if absent, copies the Dashboards plugin (about 425 MB temporary download), applies the settings and restarts Dashboards. It prints no secrets. Re-running is safe: completed steps are skipped.

## Using it

- **Latency per query:** open http://localhost:5601/app/query-insights-dashboards. Top queries by latency appear for the current five-minute window and are kept in a local `top_queries-*` index for seven days. Query Insights lists queries slower than others in the window; it is not a full request log.
- **API:** `GET _insights/top_queries?type=latency` (Dev Tools console).
- **Raw query structure:** `python3 scripts/show_search_queries.py -n 3` reads the slow log at `$(brew --prefix)/var/log/opensearch/opensearch_homebrew_index_search_slowlog.log` and prints readable JSON with embedding arrays shortened.
- **Browse documents:** Dashboards Management, Index patterns, then Discover.

## Security and operating notes

- Dashboards has no login. It acts as `dashboards_admin` for anyone who can reach port 5601; it is bound to 127.0.0.1 only. Do not bind it to a network interface or forward the port without adding authentication.
- The slow log and `top_queries-*` index contain employees' question text. Treat them as sensitive. Turn the slow log off when not needed:
  `PUT <index>/_settings {"index.search.slowlog.threshold.query.trace":"-1","index.search.slowlog.threshold.fetch.trace":"-1"}`
- Query Insights adds small overhead and storage. Disable with `search.insights.top_queries.latency.enabled=false`.
- `brew upgrade` or reinstall of Dashboards removes the copied plugin from the Cellar, and a version change breaks it. Re-run the script after upgrading, and match the plugin version to the Dashboards version.
- Index settings are lost if an index is deleted and rebuilt; re-run the script to apply them again.
- Neither service nor these tools are part of the application's access-control model. The application's own OpenSearch account (`kb_service`) is unchanged and has no Dashboards permissions.

## Startup correction — 2026-10-29

The current installation includes `securityDashboards` and uses `dashboards_server`
as its backend account; the earlier no-login description above is historical.
OpenSearch Security's `config.dynamic.kibana.server_username` must also be
`dashboards_server`. A mismatch (previously `kibanaserver`) prevented saved-object
migration with `indices:admin/get` errors and returned HTTP 503.
The live configuration was backed up privately in
`var/opensearch-admin/securityconfig-before-startup-fix.json`, then only the
server identity was changed using securityadmin with `-t config`.
The runtime requires `OPENSEARCH_HOME=/opt/homebrew/opt/opensearch/libexec` and
`OPENSEARCH_JAVA_HOME=/opt/homebrew/opt/openjdk@25/libexec/openjdk.jdk/Contents/Home`.
An additional `kb_dashboards_metadata` role permits `indices:admin/get` and is
mapped only to `dashboards_server`.
Start project storage with `.venv/bin/python scripts/start_object_store.py`, then
start the app with `.venv/bin/python -m kb.cli serve` (preserving `.env`).
