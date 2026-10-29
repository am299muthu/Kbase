# Object storage without moving SQLite controls

Employees, folder grants, approvals, structured document/chunk metadata, audit events and sessions remain in SQLite. S3 replaces only the file/object layer: `staging/`, `raw/`, `raw-parsed/`, `curated/` and `assets/`. It does not require PostgreSQL, an external model, or an external search service.

On 2026-10-27 the user explicitly authorized native local S3 storage. Garage 2.5.0 ARM64 was installed from Homebrew's verified bottle, configured under private `var/garage`, and bound only to loopback. Bucket `abc-knowledge` and key `kb-objects` were explicitly authorized and provisioned; the runtime key has read/write, not owner, permissions. **61 file-layer objects / 5,025,128 bytes** were copied and hash-verified, originals retained, and the running app now uses S3 while controls remain SQLite. See [rollout status and receipts](governance-and-activation.md). No remote/cloud file destination was used.

## Configuration

Use the Settings/environment fields below. Values here are examples, not credentials or an available endpoint.

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `STORAGE_BACKEND` | `local` | Set `s3` only for an intentional cutover. |
| `S3_ENDPOINT_URL` | empty | Empty selects regional AWS S3. An approved compatible service uses its HTTPS origin, with no path/query/userinfo. |
| `S3_BUCKET` | `abc-knowledge` | Existing private DNS-compatible bucket name; not a URL or ARN. |
| `AWS_REGION` | `ap-southeast-1` | AWS region or the signing region required by the compatible provider. |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | empty | Set both, or leave both empty to use the SDK credential provider chain. |
| `AWS_SESSION_TOKEN` | empty | Optional token with an explicitly configured temporary credential pair. |
| `S3_ADDRESSING_STYLE` | `auto` | `auto`, `path` or `virtual`; native compatible services often require `path`. |
| `S3_CA_BUNDLE` | empty | Optional private CA PEM bundle; TLS certificate/hostname verification stays enabled. |
| `S3_ALLOW_INSECURE_HTTP` | `false` | Explicit development-only HTTP opt-in, restricted to literal loopback IPs or `localhost`. This host's local Garage use was explicitly approved; remote deployment still requires verified TLS. |
| `S3_WRITE_MODE` | `conditional` | Server-enforced conditional PUT, or explicit `serialized-local` for a loopback development provider; see limitations below. |
| `S3_CONNECT_TIMEOUT_SECONDS` | `5` | Connect timeout, greater than zero and at most 120 seconds. |
| `S3_READ_TIMEOUT_SECONDS` | `60` | Socket read timeout, greater than zero and at most 600 seconds. Not an overall transfer deadline. |
| `S3_MAX_ATTEMPTS` | `3` | Total SDK request attempts, 1–10, including the first request. |

AWS example, after an operator has created and secured the bucket:

```dotenv
STORAGE_BACKEND=s3
S3_ENDPOINT_URL=
S3_BUCKET=your-approved-private-bucket
AWS_REGION=ap-southeast-1
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_SESSION_TOKEN=
```

Prefer workload roles/short-lived credentials or an approved SDK profile. Do not commit credentials, paste them into commands, or embed them in endpoint URLs. On non-AWS hosts using a profile or environment credentials, consider `AWS_EC2_METADATA_DISABLED=true` to disable unintended EC2 metadata credential discovery. S3 request timeouts do not configure every SDK credential provider's discovery timeout. SDK endpoint override environment/profile settings are ignored; `S3_ENDPOINT_URL` is the explicit application endpoint selector.

For a compatible service, configure its operator-supplied HTTPS origin, signing region and addressing mode. A custom CA is supported; disabling certificate verification is not. Remote plaintext HTTP, endpoint credentials/query strings and common placeholder credentials are rejected. A bad endpoint, missing SDK, credential failure, permission error or unavailable bucket is an error: **there is no silent local fallback**. S3 selection does not create a local object tree.

## Provisioning is separate from startup

`ObjectStore.ensure_bucket()` and `.health()` only issue `HeadBucket`. Startup/init and ordinary writes never create buckets, enable versioning or change public access policy. A missing bucket fails until an authorized operator provides it.

The explicit administrative API is:

```python
store.provision_bucket(
    confirm_bucket="your-approved-private-bucket",
    enable_versioning=True,
    block_public_access=True,
)
```

This is an opt-in remote mutation, **not a command to run as part of these instructions**. It creates only after a missing-bucket response and exact bucket-name confirmation. It never modifies an existing bucket. Permission failures are not treated as missing buckets. A failure after creation can leave a partially configured bucket; review it manually, rather than deleting it or retrying with security controls silently removed.

Some compatible providers lack AWS public-access-block/versioning APIs or use different region conventions. The operator must review the provider, explicitly disable unsupported administrative calls if appropriate, and enforce equivalent private access/version retention through its own controls. An existing bucket's privacy, encryption, versioning, lifecycle, backup and region are **not proven** by `HeadBucket` or a successful health check.

Use separate identities for provisioning and runtime. The runtime identity generally needs bucket `ListBucket` for `HeadBucket`, plus object `GetObject`/`PutObject` within the approved layer prefixes. It does not need bucket creation, public-policy editing or deletion. Where required, add narrowly scoped KMS permissions. Do not grant public access or generate public/presigned links. Use provider-side default encryption and enforce approved encryption/network policies. Object storage is not a replacement for the application's SQLite authorization checks.

## Integrity and failure behavior

- New S3 objects carry `x-amz-meta-sha256`. Every write is streamed back and checked against the source SHA-256, metadata and response length. Ordinary file uploads can use multipart transfers; an ETag is never mistaken for a file digest.
- Reads verify SHA-256 metadata and length before atomically replacing the destination. Callers can also supply an independently trusted `expected_sha256`, such as the original document hash in SQLite. Missing/malformed metadata fails closed unless an independent expected hash is supplied; malformed or conflicting metadata still fails.
- Metadata is not a signature. A storage administrator able to alter both bytes and metadata is outside that integrity guarantee. Preserve independent manifest/SQLite hashes and backups.
- A failed fetch leaves an existing destination intact. Local writes/copies use temporary files and atomic replacement after hash verification. Local storage has no persisted checksum sidecar; use `expected_sha256` to detect pre-existing local corruption, rather than merely validating the copy.
- Errors are propagated; a failed write/read is not success. A failed S3 write can nevertheless have created an object/version before read-back or permission failure. It is retained for operator review, never automatically deleted.
- Read-back verification needs `GetObject` and incurs extra requests, download bandwidth and latency. Downloads and local temporary/parser/cache files are still needed when processing remote objects; S3 mode does not mean that no document bytes ever touch the host.
- Object keys must be canonical relative POSIX paths. Traversal, backslashes, empty/dot path segments, control characters and symlink paths inside the local object namespace are rejected.

## Explicit migration: copy, verify, then decide on cutover

Migration is intentionally **not** corpus ingestion. It copies already-existing object-layer files from the explicitly supplied `data_dir/objects` root. It does not traverse `KM_Data`, choose documents, grant access, approve documents, invoke parsers/models, reindex search, initialize databases, switch the backend, alter SQLite rows, rewrite embedded URIs, or delete anything.

The migration API lives in `kb/storage_migration.py`:

1. Stop ingestion, worker, app writes and maintenance processes for a stable snapshot. Back up SQLite and the local object root together using the existing backup procedure. Obtain approval for the exact objects and destination.
2. Select a JSON list of **explicit object keys**, with no globs or automatic recursive scan. Include every dependent original, parsed object, curated payload and asset required by the reviewed documents. Each manifest contains 1–1000 unique keys in the five permitted layers. Conditional create-only migration supports objects up to 5 GiB each; larger files require a separately reviewed multipart migration.
3. Call `plan_migration(source_root, keys, manifest_path)`. This opens and hashes only those selected local files, writes a new mode-0600 manifest, and returns its SHA-256. It does not construct an S3 client or contact a remote service. Review and retain the manifest hash out of band. The manifest records each exact key, size, SHA-256 and absolute source root.
4. Call `verify_manifest(manifest_path, expected_manifest_sha256=reviewed_hash, source_root=object_root)`, or `copy_migration(..., apply=False)`, for an offline dry-run. Both rehash selected files. Changed manifests, changed files, mismatched roots, unsupported layers, symlinks and duplicate keys stop the operation. Dry-run does not check remote existence/permissions or readiness, and should not instantiate `ObjectStore`.
5. Only after authorization, call the applying API with an explicitly configured S3 store:

   ```python
   copy_migration(
       manifest_path,
       expected_manifest_sha256=reviewed_hash,
       source_root=object_root,
       apply=True,
       store=approved_s3_store,
       confirm_bucket=approved_bucket,
       confirm_endpoint=approved_endpoint,  # exact origin without trailing /, or "aws"
       receipt_path=new_receipt_path,
   )
   ```

   This checks the existing bucket; it never provisions one. The endpoint and bucket confirmations must match the configured target. In default `conditional` mode, race protection relies on the provider correctly enforcing `If-None-Match: *`; provider capability must be verified, not inferred from acceptance of the header. Garage ignored this condition in the actual probe, so this host explicitly selects `serialized-local`: a shared project-DATA_DIR file lock serializes all application writes, and migration checks destination existence under that lock before writing. Existing objects are refused by create-only writes and accepted by migration only after full byte/hash/size verification and matching managed metadata. This is **not server-atomic or distributed**, and cannot protect against writers bypassing the project's lock; keep other writers stopped. Serialized mode is rejected for non-loopback endpoints. Different or metadata-less destination objects remain conflicts and are never patched or overwritten automatically.
6. Each object is rechecked against the original manifest. Verified copies and existing identical objects are recorded in an exclusive mode-0600 JSONL receipt, flushed/fsynced after every record. Only a final `completed` record means the batch completed. A partial receipt preserves the checkpoint; a lost connection may leave additional unrecorded remote objects. Retry the same reviewed manifest with a **new** receipt; identical remote objects are verified and skipped. No source or destination object is deleted on failure.
7. Review completed receipts and hashes before any separate configuration cutover. Keep originals/backups and a rollback plan. Configuration is global, so copying a bounded batch is **not** permission to switch the entire deployment before all its required objects are present. There is no runtime remote-to-local fallback. Rollback to local storage requires reviewing any objects written after cutover; those new writes are not automatically mirrored locally.

### URI/reference boundary

Byte-preserving migration keeps JSON bytes and hashes unchanged. Existing parsed/curated payloads can still contain `local://...` strings. SQLite document hashes, object keys and grants are not rewritten. A URI-sensitive client or search index may therefore still show an old local reference after copying. Do not substitute strings inside JSON during a hash-preserving copy. Review reference resolution, apply any separately authorized metadata transformation/reindex, and validate retrieval/assets/authorization on a synthetic fixture before cutover. Receipt completion alone does not establish application-level cutover readiness.

### CLI integration contract

The main CLI exposes these APIs as separate operator commands; planning/migration/provisioning must dispatch **before** automatic `init_db()`, staff seeding, search initialization or other unrelated mutations:

- `object-plan --keys-file PATH --manifest PATH`: parse a JSON list and call `plan_migration(cfg.data_dir / 'objects', keys, manifest)`; print the manifest digest/count/bytes, not file contents.
- `object-migrate --manifest PATH --manifest-sha256 HEX`: offline verification by default. Only `--apply --confirm-bucket NAME --confirm-endpoint ORIGIN_OR_aws --receipt NEW_PATH` constructs an S3 store and calls the applying API. Do not infer approvals, endpoints or credentials.
- `object-provision --confirm-bucket NAME`: explicit administrative operation only; optional `--no-versioning`/`--no-public-access-block` correspond to consciously reviewed provider limitations. Never call provisioning from startup/init.

CLI help is authoritative for the available flags. This document does not authorize executing any migration or remote operation.
