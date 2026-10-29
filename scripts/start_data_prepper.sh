#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME="$ROOT/var/data-prepper-runtime/opensearch-data-prepper-2.16.0-linux-arm64"
JAVA="${JAVA_HOME:-/opt/homebrew/opt/openjdk@25}/bin/java"
[ -f "$ROOT/var/telemetry/pipelines.yaml" ] || { echo 'Run .venv/bin/python scripts/setup_data_prepper.py first.' >&2; exit 1; }
[ -d "$RUNTIME/lib" ] || { echo 'Install Data Prepper 2.16.0; see docs/data-prepper.md.' >&2; exit 1; }
cd "$ROOT/var/telemetry"
umask 077
exec "$JAVA" -Xms256m -Xmx768m -Dfile.encoding=UTF-8 \
  "-Ddata-prepper.dir=$RUNTIME" "-Dlog4j.configurationFile=$RUNTIME/config/log4j2-rolling.properties" \
  -cp "$RUNTIME/lib/*" org.opensearch.dataprepper.DataPrepperExecute \
  "$ROOT/var/telemetry/pipelines.yaml" "$ROOT/var/telemetry/data-prepper-config.yaml"
