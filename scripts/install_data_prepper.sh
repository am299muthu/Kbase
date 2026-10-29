#!/usr/bin/env bash
# Pinned no-JDK distribution; run with native Java on this development Mac.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/var/data-prepper-runtime"
mkdir -p "$DEST"
ARCHIVE="$DEST/data-prepper-2.16.0.tar.gz"
curl -fL --retry 2 --max-time 300 \
  https://artifacts.opensearch.org/data-prepper/2.16.0/opensearch-data-prepper-2.16.0-linux-arm64.tar.gz -o "$ARCHIVE"
printf '%s  %s\n' ee4aa0c37bdd1f3d89b3004684e4a8e61795e6c564768c831c28e7e17ae52460 "$ARCHIVE" | shasum -a 256 -c -
tar -xzf "$ARCHIVE" -C "$DEST"
echo 'Installed Data Prepper 2.16.0. Next: .venv/bin/python scripts/setup_data_prepper.py'
