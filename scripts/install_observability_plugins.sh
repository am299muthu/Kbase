#!/usr/bin/env bash
# Native macOS/Homebrew 3.9.0 plugin setup. Restarts OpenSearch and Dashboards.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX="$(brew --prefix)"
WORK="$ROOT/var/observability-install"
mkdir -p "$WORK"
for artifact in opensearch-job-scheduler opensearch-sql-plugin opensearch-observability; do
  folder="$artifact"
  [ "$artifact" != opensearch-sql-plugin ] || folder=opensearch-sql
  if [ ! -d "$PREFIX/var/opensearch/plugins/$folder" ]; then
    url="https://repo1.maven.org/maven2/org/opensearch/plugin/$artifact/3.9.0.0/$artifact-3.9.0.0.zip"
    [ -f "$WORK/$artifact.zip" ] || curl -fsSL --retry 2 --max-time 180 "$url" -o "$WORK/$artifact.zip"
    curl -fsSL --max-time 30 "$url.sha256" -o "$WORK/$artifact.sha256"
    printf '%s  %s\n' "$(cut -c1-64 "$WORK/$artifact.sha256")" "$WORK/$artifact.zip" | shasum -a 256 -c -
    "$PREFIX/opt/opensearch/bin/opensearch-plugin" install --batch "file://$WORK/$artifact.zip"
  fi
done
if [ ! -d "$PREFIX/var/opensearch-dashboards/plugins/observabilityDashboards" ]; then
  archive="${OSD_ARCHIVE:-$WORK/osd.tgz}"
  if [ ! -f "$archive" ]; then
    curl -fsSL --retry 2 --max-time 300 https://artifacts.opensearch.org/releases/bundle/opensearch-dashboards/3.9.0/opensearch-dashboards-3.9.0-linux-arm64.tar.gz -o "$archive"
  fi
  tar -xzf "$archive" -C "$WORK" opensearch-dashboards-3.9.0/plugins/observabilityDashboards opensearch-dashboards-3.9.0/plugins/securityDashboards
  cp -R "$WORK/opensearch-dashboards-3.9.0/plugins/securityDashboards" "$PREFIX/Cellar/opensearch-dashboards/3.9.0/plugins/"
  cp -R "$WORK/opensearch-dashboards-3.9.0/plugins/observabilityDashboards" "$PREFIX/Cellar/opensearch-dashboards/3.9.0/plugins/"
fi
[ -e "$PREFIX/var/opensearch-dashboards/src" ] || ln -s "$PREFIX/Cellar/opensearch-dashboards/3.9.0/src" "$PREFIX/var/opensearch-dashboards/src"
[ -e "$PREFIX/var/opensearch-dashboards/node_modules" ] || ln -s "$PREFIX/Cellar/opensearch-dashboards/3.9.0/node_modules" "$PREFIX/var/opensearch-dashboards/node_modules"
brew services restart opensearch
brew services restart opensearch-dashboards
