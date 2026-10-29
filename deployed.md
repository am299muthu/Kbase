# Deployment status

**Not deployed.** No public URL, published recording, repository-sharing action, or free-tier hosting verification is claimed.

| Item | Status |
|---|---|
| Application URL | Not available; local development URL is `http://127.0.0.1:8000` |
| Health URL | Local only: `http://127.0.0.1:8000/health` |
| Recorded demo | Not recorded/published; use `docs/demo-guide.md` |
| Hosting and cold-start measurements | Not measured |
| Full historical corpus | Not ingested; demonstration uses synthetic fixtures only |

## Deployment plan, not completed deployment

A persistent single host can run FastAPI, the ingestion worker and private stdio MCP subprocesses. Keep PostgreSQL/OpenSearch/S3 outside an ephemeral web filesystem, or explicitly label a SQLite/files deployment as a disposable synthetic demo. Native OpenSearch has JVM/memory requirements that may exceed free-tier limits; free-tier suitability has not been verified. The LocalStack Python CLI alone does not provide a native Docker-free S3 service.

Before exposing any service: use production identity and HTTPS, disable demo account/password disclosure, set secure cookies, restrict database/search/object endpoints, provision persistent storage/backups, verify NER and PII behavior, approve external model data egress, rotate the exposed source credential, and review the production gaps in `requirements.md`. Infrastructure mode validates backend selections but is not itself a production-hardening switch.

`/health` reports application dependencies, MCP discovery, masking detector and model configuration; it does not prove provider availability, authenticated end-to-end retrieval, readiness under load or semantic answer quality. After a real deployment, replace this status with verified URLs, exact revision/configuration (without secrets), hosting/storage details, and separate cold-/warm-start measurements. Do not present a localhost link as a deployed application.
