# OpenSearch plugins on this macOS ARM64 host

## Installed and verified — 2026-10-26

- Host: Darwin ARM64 (Apple Silicon); Homebrew OpenSearch **3.9.0**, OpenJDK 25.
- Installed **opensearch-security 3.9.0.0** and **opensearch-knn 3.9.0.0** using the official plugin installer and version-matched Maven Central ZIPs.
- Verified published SHA-512 checksums before installation. This checks download integrity against the same repository, not an independent signature verification.
- Required extension modules are already present: `rule-framework`, `lang-painless`, `transport-grpc`. Optional `workload-management` is absent; the installer warns that related features may be unavailable.
- The upstream release build manifest lists Linux/Windows, not macOS plugin bundles. This is a locally verified Homebrew configuration, **not a claim of official macOS production support**.

### Actual package sources

- [Security ZIP and checksums](https://repo.maven.apache.org/maven2/org/opensearch/plugin/opensearch-security/3.9.0.0/)
- [k-NN ZIP and checksums](https://repo.maven.apache.org/maven2/org/opensearch/plugin/opensearch-knn/3.9.0.0/)
- [Upstream 3.9.0 release manifest](https://github.com/opensearch-project/opensearch-build/blob/main/manifests/3.9.0/opensearch-3.9.0.yml)

Downloaded ZIPs are in separate directories beneath `/private/tmp/kbase-opensearch-plugins.CuMCIe/`. These temporary downloads may be removed by the OS; installed plugins are under `/opt/homebrew/var/opensearch/plugins/`.

The installer was run against each verified local ZIP:

```sh
/opt/homebrew/opt/opensearch/bin/opensearch-plugin install --batch file:///ABSOLUTE/PATH/TO/opensearch-security-3.9.0.0.zip
/opt/homebrew/opt/opensearch/bin/opensearch-plugin install --batch file:///ABSOLUTE/PATH/TO/opensearch-knn-3.9.0.0.zip
```

`--batch` accepts plugin Java permission requests; do not apply this blindly to unreviewed downloads. Do not reinstall over the current plugins, substitute Linux ARM64 native libraries, or mix server/plugin versions.

## Homebrew Security dependency fix

The first startup failed with `NoClassDefFoundError: org/bouncycastle/jcajce/provider/BouncyCastleFipsProvider`. The published Security ZIP expects Bouncy Castle dependencies supplied by the server; this Homebrew server did not provide them on its runtime classpath.

Added these exact OpenSearch 3.9.0-pinned Maven artifacts to the installed Security plugin directory, after verifying their published SHA-256 checksums:

| Artifact (`org.bouncycastle`) | Version |
|---|---|
| `bc-fips` | 2.1.3 |
| `bcpkix-fips` | 2.1.12 |
| `bcutil-fips` | 2.1.7 |

[Upstream version pins](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/gradle/libs.versions.toml); [Security dependency declaration](https://github.com/opensearch-project/security/blob/3.9.0.0/build.gradle). Downloads remain under `/private/tmp/kbase-opensearch-crypto.11igrlq9/`. Recheck packaging/dependencies on upgrades rather than blindly carrying these JARs forward. Using these libraries does **not** establish FIPS compliance or validated deployment.

## Security configuration

OpenSearch is running as a Homebrew service on **https://127.0.0.1:9200**, bound to loopback only. HTTP and transport TLS use a locally generated CA and certificates, not bundled demo certificates. Hostname/certificate verification remains enabled; no system-wide CA trust changes were made.

| Location | Purpose |
|---|---|
| `/opt/homebrew/etc/opensearch/opensearch.yml` | Loopback single-node and Security TLS settings |
| `/opt/homebrew/etc/opensearch/kbase-tls/` | Public root CA, node certificate and private node key |
| `/opt/homebrew/etc/opensearch/kbase-security/` | Custom authentication, roles, mappings and audit configuration |
| `var/opensearch-admin/` | Private CA key, administrative client certificate/key, service credential copy |
| `.env` | App's restricted `kb_service` credentials and explicit CA path, mode 0600 |
| `var/opensearch-preplugins.VAeDIO/` | Cold pre-install backup of server configuration/data and previous app environment |

Private credential directories use mode 0700 and generated files mode 0600. Never commit, publish, print, or screenshot their contents. Keep a secure backup of CA/admin keys outside the app host for a real deployment. Node/admin certificates expire after 365 days; the local CA after 3650 days. Plan renewal before expiry.

Security was initialized using the separate admin client certificate; no demo admin password/account was created. Basic authentication uses one generated `kb_service` identity with permissions scoped to **abc-kb-curated-v1**, plus cluster health/info and necessary composite bulk/mget actions. A subsequent explicit authorization added the prepared **abc-kb-curated-openrouter-v1** vector index to that same role; it remains empty pending transfer approval ([status](openrouter-activation.md)). Its role intentionally lacks index deletion, security administration and cluster-settings access. Mapping inspection requires `indices:admin/mappings/get`, which was separately authorized and verified.

The app supports `OPENSEARCH_CA_CERTS` and uses:

```dotenv
OPENSEARCH_URL=https://127.0.0.1:9200
OPENSEARCH_USERNAME=kb_service
# OPENSEARCH_PASSWORD is already stored privately; do not overwrite it with an example.
OPENSEARCH_VERIFY_CERTS=true
OPENSEARCH_CA_CERTS=/opt/homebrew/etc/opensearch/kbase-tls/root-ca.pem
OPENSEARCH_INDEX=abc-kb-curated-v1
OPENSEARCH_VECTOR_SEARCH=false
```

This service identity can read all KB chunks because it performs application retrieval. **Do not give it to staff or use it as a Dashboards login**: employee folder ACLs remain application controls, not per-employee OpenSearch DLS/FLS. Local OS users/admins remain a separate trust boundary. Dashboards Security integration and production identity/TLS provisioning remain separate tasks; Dashboards was not started or reconfigured here.

Security audit uses internal OpenSearch storage, with request-body logging disabled and sensitive headers excluded. This is not an immutable external audit sink.

## k-NN scope and verification

The Maven k-NN ZIP is Java-only: no k-NN Faiss/common/SIMD native libraries. On this host, verified **ordinary float32 HNSW with explicit `engine: lucene`**, cosine similarity, and an in-query folder filter. Three synthetic vectors were indexed into `kbase-knn-arm64-smoke-20261026`; the excluded folder was not returned and the nearest allowed vector ranked first. Only that specifically authorized test index was deleted afterward.

Do **not** omit the engine: the plugin default can select Faiss. Faiss/NMSLIB, native model training, half-float and SIMD-dependent quantized paths are not provisioned or verified. Native-library absence is not a universal Java fallback.

Plugin availability does not turn the KB into semantic search. Existing KB content remains **BM25, 10 documents / 107 chunks**, and no embeddings or external API calls were made. To enable semantic retrieval later:

1. Approve/model-provision real embeddings, verify dimensions and provider constraints.
2. Choose a fresh vector index; have an operator explicitly authorize/update the service role for that exact index.
3. Set the new index, embedding configuration and `OPENSEARCH_VECTOR_SEARCH=true`, then reindex approved curated content.
4. Validate quality, filtered access, failure behavior and model identity before switching traffic. Keep the original BM25 index intact until validation is complete.

## Verification and operations

Passed: loaded plugin versions, verified HTTPS, anonymous rejection (401), restricted service denial on another index/security index/cluster-settings reads, preserved 107 KB chunks, existing mapping inspection, synthetic filtered Lucene k-NN, live app `/health`, six-tool authenticated MCP check, and synthetic-policy HTTP chat as E005. A proposed real-PDF content check was permission-blocked and not executed. A proposed cluster-setting write denial test was also blocked; it was omitted in favor of read-only denial checks, with no cluster-setting write executed.

```sh
/opt/homebrew/opt/opensearch/bin/opensearch-plugin list
brew services list
curl --fail http://127.0.0.1:8000/health
.venv/bin/python -m kb.cli mcp-check
```

For operator checks without disclosing passwords, use the admin certificate and explicit CA:

```sh
curl --fail --cacert /opt/homebrew/etc/opensearch/kbase-tls/root-ca.pem \
  --cert var/opensearch-admin/admin.pem --key var/opensearch-admin/admin-key.pem \
  https://127.0.0.1:9200/_cat/plugins
```

Do not use `curl -k`, disable certificate verification, enable unsafe demo certificates, or expose this local prototype publicly. Homebrew upgrades can replace binaries/modules and require matching plugins and a fresh compatibility check. The cold backup is a rollback aid, not a live snapshot/DR guarantee; do not restore it over a running server or erase later index changes without explicit approval.
