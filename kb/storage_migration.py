"""Explicit, bounded, byte-preserving local-object to S3 migration.

No directory scan, database changes, source deletions, URI rewrites, remote bucket
creation, or automatic backend switch. Planning and dry-run are offline.
"""

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from kb.storage import IntegrityError, ObjectConflictError, ObjectStore, file_digest, validate_key

LAYERS = frozenset({'staging', 'raw', 'raw-parsed', 'curated', 'assets'})
MAX_OBJECTS = 1000
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_COPY_BYTES = 5 * 1024**3


def _source_path(root: Path, key: str) -> Path:
    validate_key(key)
    if key.split('/')[0] not in LAYERS or '/' not in key:
        raise ValueError('Migration accepts only staging/raw/raw-parsed/curated/assets object keys')
    if root.is_symlink():
        raise ValueError('Source object root must not be a symlink')
    path = root
    for part in key.split('/'):
        path = path / part
        if path.is_symlink():
            raise ValueError('Migration source must not contain symlinks')
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError('Migration source must be an existing file inside the object root')
    return path


def _validate_entries(entries):
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_OBJECTS:
        raise ValueError('Select between 1 and 1000 explicit object keys per manifest')
    seen = set()
    for item in entries:
        if not isinstance(item, dict) or set(item) != {'key', 'size', 'sha256'}:
            raise ValueError('Invalid migration manifest entry')
        validate_key(item['key'])
        if item['key'] in seen:
            raise ValueError('Duplicate object key in migration manifest')
        seen.add(item['key'])
        if (type(item['size']) is not int or not 0 <= item['size'] <= MAX_COPY_BYTES
                or not isinstance(item['sha256'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', item['sha256'])):
            raise ValueError('Invalid manifest size or SHA-256; copy limit is 5 GiB per object')


def _exclusive_json(path: Path, value):
    body = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')
    if len(body) > MAX_MANIFEST_BYTES:
        raise ValueError('Manifest is too large')
    # Never overwrite an existing manifest, source file or receipt.
    fd = os.open(Path(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(body)
        output.flush()
        os.fsync(output.fileno())
    return hashlib.sha256(body).hexdigest()


def plan_migration(source_root: Path, keys: list[str], manifest_path: Path):
    """Hash only explicitly selected local object keys; never contact S3.

    The caller must obtain approval to read these selected file-layer objects.
    """
    source_root = Path(source_root)
    if source_root.is_symlink() or not source_root.is_dir():
        raise ValueError('Source object root must be an existing non-symlink directory')
    source_root = source_root.resolve()
    if not isinstance(keys, list) or not 1 <= len(keys) <= MAX_OBJECTS or len(keys) != len(set(keys)):
        raise ValueError('Select 1–1000 unique explicit object keys')
    entries = []
    for key in keys:
        source = _source_path(source_root, key)
        before = source.stat()
        if before.st_size > MAX_COPY_BYTES:
            raise ValueError('Migration objects larger than 5 GiB require a separate reviewed multipart procedure')
        sha256 = file_digest(source)
        after = source.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise IntegrityError('Source changed while planning; stop writers and create a new manifest')
        entries.append({'key': key, 'size': after.st_size, 'sha256': sha256})
    entries.sort(key=lambda item: item['key'])
    manifest = {'version': 1, 'source_root': str(source_root), 'objects': entries}
    _validate_entries(entries)
    digest = _exclusive_json(manifest_path, manifest)
    return {'manifest_sha256': digest, 'objects': len(entries),
            'bytes': sum(item['size'] for item in entries), 'remote_contacted': False}


def verify_manifest(manifest_path: Path, *, expected_manifest_sha256: str, source_root: Path):
    """Offline dry-run: pin manifest bytes/root and rehash every selected source."""
    if not re.fullmatch(r'[0-9a-f]{64}', expected_manifest_sha256):
        raise ValueError('A reviewed manifest SHA-256 is required')
    with Path(manifest_path).open('rb') as stream:
        body = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(body) > MAX_MANIFEST_BYTES or hashlib.sha256(body).hexdigest() != expected_manifest_sha256:
        raise IntegrityError('Manifest SHA-256 does not match the reviewed manifest')
    manifest = json.loads(body)
    if (not isinstance(manifest, dict) or set(manifest) != {'version', 'source_root', 'objects'}
            or type(manifest['version']) is not int or manifest['version'] != 1):
        raise ValueError('Unsupported migration manifest format')
    source_root = Path(source_root)
    if source_root.is_symlink() or not source_root.is_dir():
        raise ValueError('Source root must be an existing non-symlink directory')
    source_root = source_root.resolve()
    if manifest['source_root'] != str(source_root):
        raise ValueError('Manifest source root does not match the explicitly selected source root')
    _validate_entries(manifest['objects'])
    for item in manifest['objects']:
        source = _source_path(source_root, item['key'])
        if source.stat().st_size != item['size'] or file_digest(source) != item['sha256']:
            raise IntegrityError('Selected local object no longer matches its migration manifest')
    return manifest


def _record(receipt, record):
    receipt.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')
    receipt.flush()
    os.fsync(receipt.fileno())


def _verify_existing(store, item):
    verified = store.verify_object(item['key'], expected_sha256=item['sha256'])
    if verified['size'] != item['size'] or verified['metadata_sha256'] != item['sha256']:
        raise ObjectConflictError('Existing destination lacks matching size or managed SHA-256 metadata; no overwrite performed')
    return verified


def copy_migration(manifest_path: Path, *, expected_manifest_sha256: str, source_root: Path,
                   apply: bool = False, store: ObjectStore | None = None,
                   confirm_bucket: str | None = None, confirm_endpoint: str | None = None,
                   receipt_path: Path | None = None):
    """Copy only a reviewed manifest; default is offline verification, not copying.

    Applying requires explicit bucket AND endpoint confirmation (use 'aws' for the
    SDK's regional AWS endpoint), and a new durable JSONL receipt. Rerunning with a
    new receipt verifies matching existing objects; it never overwrites a conflict.
    """
    manifest = verify_manifest(manifest_path, expected_manifest_sha256=expected_manifest_sha256,
                               source_root=source_root)
    summary = {'objects': len(manifest['objects']), 'bytes': sum(item['size'] for item in manifest['objects']),
               'manifest_sha256': expected_manifest_sha256, 'copied': 0, 'verified_existing': 0,
               'remote_contacted': False, 'applied': False}
    if not apply:
        return summary
    if store is None or store.backend != 's3':
        raise ValueError('Applying requires an explicitly configured S3 ObjectStore')
    endpoint = store.cfg.s3_endpoint_url.strip().rstrip('/') or 'aws'
    if confirm_bucket != store.cfg.s3_bucket or confirm_endpoint != endpoint:
        raise ValueError('Applying requires exact bucket and endpoint confirmation')
    if receipt_path is None:
        raise ValueError('Applying requires a new receipt path')
    # Exclusive receipt creation precedes all remote calls. A partial receipt is a
    # checkpoint, not a success marker. Only a final completed record means success.
    fd = os.open(Path(receipt_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as receipt:
        _record(receipt, {'event': 'started', 'at': datetime.now(timezone.utc).isoformat(),
                          'manifest_sha256': expected_manifest_sha256, 'bucket': confirm_bucket,
                          'endpoint': endpoint, 'write_mode': store.write_mode, 'source_root': manifest['source_root']})
        try:
            store.ensure_bucket()  # Read-only; never create or reconfigure a bucket.
            summary['remote_contacted'] = True
            for item in manifest['objects']:
                source = _source_path(Path(manifest['source_root']), item['key'])
                if source.stat().st_size != item['size'] or file_digest(source) != item['sha256']:
                    raise IntegrityError('Source changed after preflight; migration stopped')
                try:
                    verified = _verify_existing(store, item)
                    status = 'verified_existing'
                except Exception as exc:
                    code = str(getattr(exc, 'response', {}).get('Error', {}).get('Code', ''))
                    if code not in {'NoSuchKey', '404', 'NotFound'}:
                        raise
                    try:
                        store.put_file(item['key'], source, expected_sha256=item['sha256'], if_absent=True)
                        status = 'copied'
                    except ObjectConflictError:
                        # A concurrent writer won; accept only identical verified content.
                        status = 'verified_existing'
                    verified = _verify_existing(store, item)
                if file_digest(source) != item['sha256']:
                    raise IntegrityError('Source changed during migration; review remote object and receipt')
                summary[status] += 1
                _record(receipt, {'event': status, **item, 'version_id': verified['version_id']})
            summary['applied'] = True
            _record(receipt, {'event': 'completed', **summary})
        except Exception as exc:
            # Never log credentials, source text or provider error payloads.
            _record(receipt, {'event': 'failed', 'error_type': type(exc).__name__,
                              'copied': summary['copied'], 'verified_existing': summary['verified_existing']})
            raise
    return summary
