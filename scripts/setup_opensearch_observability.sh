#!/usr/bin/env bash
# Reproduce the OpenSearch Dashboards + query-latency monitoring setup on macOS (Homebrew, no Docker).
# Idempotent where practical. Run from the project root:  bash scripts/setup_opensearch_observability.sh
# Prerequisites: Homebrew `opensearch` and `opensearch-dashboards` 3.9.0 installed, OpenSearch running with the
# Security plugin and the admin client certificate in var/opensearch-admin/ (see docs/opensearch-observability.md).
# Restarts OpenSearch and Dashboards. Secrets are generated locally and never printed.
set -euo pipefail

VERSION=3.9.0
QI_VERSION=3.9.0.0
PREFIX="$(brew --prefix)"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ADMIN_DIR="$ROOT/var/opensearch-admin"
CA="$PREFIX/etc/opensearch/kbase-tls/root-ca.pem"
OSD_CFG="$PREFIX/etc/opensearch-dashboards/opensearch_dashboards.yml"
OSD_CELLAR="$(readlink -f "$PREFIX/opt/opensearch-dashboards")"
API=https://127.0.0.1:9200
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

c() { curl -s --cacert "$CA" --cert "$ADMIN_DIR/admin.pem" --key "$ADMIN_DIR/admin-key.pem" -H 'Content-Type: application/json' "$@"; }
wait_green() { for _ in $(seq 1 40); do c "$API/_cluster/health" 2>/dev/null | grep -qE '"status":"(green|yellow)"' && return 0; sleep 3; done; echo "OpenSearch did not become healthy" >&2; return 1; }

echo "1/7 Node runtime (Dashboards needs a working Node; fixes libsimdjson mismatch)"
node -v >/dev/null 2>&1 || brew reinstall node

echo "2/7 query-insights server plugin from Maven Central (SHA-256 verified)"
if ! "$PREFIX/opt/opensearch/bin/opensearch-plugin" list 2>/dev/null | grep -qx query-insights; then
  base=https://repo1.maven.org/maven2/org/opensearch/plugin/query-insights/$QI_VERSION
  curl -sf -o "$WORK/qi.zip" "$base/query-insights-$QI_VERSION.zip"
  echo "$(curl -sf "$base/query-insights-$QI_VERSION.zip.sha256" | cut -c1-64)  $WORK/qi.zip" | shasum -a 256 -c -
  "$PREFIX/opt/opensearch/bin/opensearch-plugin" install --batch "file://$WORK/qi.zip"
  brew services restart opensearch; sleep 8
fi
wait_green

echo "3/7 Dashboards users (internal users + role mappings; passwords saved to $ADMIN_DIR/dashboards.env, mode 600)"
umask 077
if [ ! -f "$ADMIN_DIR/dashboards.env" ]; then
  ADM="$(python3 -c "import secrets;print(secrets.token_urlsafe(18)+'#9Aa')")"
  SRV="$(python3 -c "import secrets;print(secrets.token_urlsafe(24))")"
  printf 'DASHBOARDS_URL=http://localhost:5601\nDASHBOARDS_USER=dashboards_admin\nDASHBOARDS_PASSWORD=%s\nDASHBOARDS_SERVER_PASSWORD=%s\n' "$ADM" "$SRV" > "$ADMIN_DIR/dashboards.env"
  c -X PUT "$API/_plugins/_security/api/internalusers/dashboards_admin"  -d "{\"password\":\"$ADM\"}" >/dev/null
  c -X PUT "$API/_plugins/_security/api/internalusers/dashboards_server" -d "{\"password\":\"$SRV\"}" >/dev/null
  c -X PUT "$API/_plugins/_security/api/rolesmapping/all_access"  -d '{"users":["dashboards_admin"]}' >/dev/null
  c -X PUT "$API/_plugins/_security/api/rolesmapping/kibana_server" -d '{"users":["dashboards_server"]}' >/dev/null
fi
chmod 600 "$ADMIN_DIR/dashboards.env"
ADM="$(grep '^DASHBOARDS_PASSWORD=' "$ADMIN_DIR/dashboards.env" | cut -d= -f2-)"

echo "4/7 Dashboards connection settings (localhost-only; no login page in this Homebrew build)"
if ! grep -q '^opensearch.hosts' "$OSD_CFG"; then
  cp "$OSD_CFG" "$OSD_CFG.bak-$(date +%Y%m%d)"
  cat >> "$OSD_CFG" <<EOF
server.host: "127.0.0.1"
server.port: 5601
opensearch.hosts: ["https://127.0.0.1:9200"]
opensearch.ssl.verificationMode: full
opensearch.ssl.certificateAuthorities: ["$CA"]
opensearch.username: "dashboards_admin"
opensearch.password: "$ADM"
EOF
  chmod 600 "$OSD_CFG"
fi
mkdir -p "$PREFIX/var/run"   # Dashboards writes its PID file here

echo "5/7 queryInsightsDashboards plugin (extracted from the official $VERSION bundle, ~425 MB download)"
PLUGIN_HOME="$PREFIX/var/opensearch-dashboards"
if [ ! -d "$PLUGIN_HOME/plugins/queryInsightsDashboards" ]; then
  curl -sf -o "$WORK/osd.tgz" "https://artifacts.opensearch.org/releases/bundle/opensearch-dashboards/$VERSION/opensearch-dashboards-$VERSION-linux-arm64.tar.gz"
  tar -xzf "$WORK/osd.tgz" -C "$WORK" "opensearch-dashboards-$VERSION/plugins/queryInsightsDashboards"
  mkdir -p "$OSD_CELLAR/plugins"
  cp -R "$WORK/opensearch-dashboards-$VERSION/plugins/queryInsightsDashboards" "$OSD_CELLAR/plugins/"
fi
# User plugins resolve from var/; the plugin needs Dashboards' @osd/* modules.
[ -e "$PLUGIN_HOME/node_modules" ] || ln -s "$OSD_CELLAR/node_modules" "$PLUGIN_HOME/node_modules"

echo "6/7 Cluster + index settings"
c -X PUT "$API/_cluster/settings" -d '{"persistent":{
  "search.insights.top_queries.latency.enabled":true,
  "search.insights.top_queries.latency.window_size":"5m",
  "search.insights.top_queries.latency.top_n_size":50,
  "search.insights.top_queries.exporter.type":"local_index",
  "search.insights.top_queries.exporter.delete_after_days":7}}' >/dev/null
# Optional raw query capture (see docs). Query text is logged: disable when not needed.
for idx in abc-kb-curated-multimodal-v2 abc-kb-curated-v1; do
  c -X PUT "$API/$idx/_settings" -d '{"index.search.slowlog.threshold.query.trace":"0ms","index.search.slowlog.threshold.fetch.trace":"0ms","index.search.slowlog.level":"trace"}' >/dev/null || true
done

echo "7/7 Start Dashboards"
brew services restart opensearch-dashboards
for _ in $(seq 1 40); do curl -s -o /dev/null -m 3 http://127.0.0.1:5601/api/status && break; sleep 4; done
curl -s -m 5 http://127.0.0.1:5601/api/status | grep -q queryInsightsDashboards && echo "OK: http://localhost:5601/app/query-insights-dashboards" || echo "Dashboards is up but the plugin is not listed; check $PREFIX/var/log/opensearch-dashboards.log"
