#!/usr/bin/env bash
# Explicit operator action; installs native packages, never Docker.
set -euo pipefail
if [[ "$(uname -s)" != Darwin ]]; then
  printf '%s\n' 'For Linux use the native DEB/RPM packages from https://opensearch.org/downloads/.' 'Install a matching OpenSearch + Dashboards version and verify package signatures.'
  exit 1
fi
command -v brew >/dev/null || { printf '%s\n' 'Install Homebrew using its official documented procedure first.'; exit 1; }
printf '%s\n' 'Installing native OpenSearch and Dashboards. Existing configuration will not be overwritten by this script.'
brew install opensearch opensearch-dashboards
prefix="$(brew --prefix)"
printf '\n%s\n' "Review configuration under $prefix/etc/opensearch and $prefix/etc/opensearch-dashboards."
printf '%s\n' 'Keep network.host/server.host bound to 127.0.0.1 for local development.' 'Provision authentication/TLS before exposing either service.' 'Start explicitly after reviewing configuration:' '  brew services start opensearch' '  brew services start opensearch-dashboards' 'For production: configure multiple nodes, replicas, TLS, security roles, snapshots and capacity monitoring.'
