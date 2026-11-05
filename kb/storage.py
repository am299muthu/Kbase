"""Local files or verified S3 objects; never fall back between backends."""

import base64
from contextlib import contextmanager
import hashlib
import ipaddress
import json
import mimetypes
import os
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from kb.config import settings


class IntegrityError(RuntimeError):
    """Object bytes do not match their recorded SHA-256."""


class ObjectConflictError(RuntimeError):
    """A create-only write encountered an existing object."""


def validate_key(key: str) -> str:
    # Reject aliases as well as traversal: one canonical key for both backends.
    if (not isinstance(key, str) or not key or len(key.encode('utf-8')) > 1024
            or '\\' in key or any(ord(char) < 32 or ord(char) == 127 for char in key)
            or PurePosixPath(key).is_absolute()
            or any(part in {'', '.', '..'} for part in key.split('/'))):
        raise ValueError('Invalid object key')
    return key


def _digest(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise ValueError('Expected a lowercase SHA-256 digest')
    return value


def _error_code(exc) -> str:
    return str(getattr(exc, 'response', {}).get('Error', {}).get('Code', ''))


def _endpoint_options(cfg):
    endpoint = cfg.s3_endpoint_url.strip()
    if endpoint:
        try:
            url = urlsplit(endpoint)
            valid = (url.scheme in {'https', 'http'} and bool(url.hostname)
                     and url.username is None and url.password is None
                     and not url.query and not url.fragment and url.path in {'', '/'})
            _ = url.port  # Validate the port without connecting or resolving DNS.
        except ValueError:
            valid = False
        if not valid:
            raise ValueError('S3 endpoint must be an HTTP(S) origin without credentials, path or query')
        if url.scheme == 'http':
            try:
                loopback = ipaddress.ip_address(url.hostname).is_loopback
            except ValueError:
                loopback = url.hostname == 'localhost'
            if not (getattr(cfg, 's3_allow_insecure_http', False) and loopback):
                raise ValueError('S3 requires HTTPS; opt-in HTTP is restricted to loopback development endpoints')
    ca_bundle = getattr(cfg, 's3_ca_bundle', '')
    if ca_bundle and not Path(ca_bundle).is_file():
        raise ValueError('S3_CA_BUNDLE must name a readable CA certificate bundle')
    return endpoint or None, str(ca_bundle) if ca_bundle else True


class ObjectStore:
    def __init__(self, cfg=None, *, client=None):
        self.cfg = cfg if cfg is not None else settings()
        self.root = Path(self.cfg.data_dir) / 'objects'
        self.backend = self.cfg.storage_backend
        if self.backend not in {'local', 's3'}:
            raise ValueError('Unsupported storage backend; no local fallback is permitted')
        self.client = None
        if self.backend == 'local':
            if client is not None:
                raise ValueError('An S3 client cannot be used with local storage')
            self.root.mkdir(parents=True, exist_ok=True)
            return

        endpoint, verify = _endpoint_options(self.cfg)
        self.write_mode = getattr(self.cfg, 's3_write_mode', 'conditional')
        if self.write_mode not in {'conditional', 'serialized-local'}:
            raise ValueError('Invalid S3 write mode')
        if self.write_mode == 'serialized-local':
            host = urlsplit(endpoint or '').hostname
            try:
                loopback = bool(host) and ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = host == 'localhost'
            if not loopback:
                raise ValueError('Serialized writes are restricted to loopback development storage')
        bucket = self.cfg.s3_bucket
        if (not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', bucket)
                or '..' in bucket or '.-' in bucket or '-.' in bucket
                or re.fullmatch(r'\d+\.\d+\.\d+\.\d+', bucket)):
            raise ValueError('S3_BUCKET must be a DNS-compatible bucket name, not an ARN or URL')
        access = self.cfg.aws_access_key_id
        secret = self.cfg.aws_secret_access_key
        token = getattr(self.cfg, 'aws_session_token', '')
        if bool(access) != bool(secret) or (token and not access):
            raise ValueError('Explicit S3 credentials require both access key and secret (and optional session token)')
        placeholders = {'test', 'dummy', 'changeme', 'change-me', 'your-access-key', 'your-secret-key'}
        if access.lower() in placeholders or secret.lower() in placeholders:
            raise ValueError('Placeholder S3 credentials are not allowed; use real credentials or the AWS credential chain')
        connect = float(getattr(self.cfg, 's3_connect_timeout_seconds', 5))
        read = float(getattr(self.cfg, 's3_read_timeout_seconds', 60))
        attempts = getattr(self.cfg, 's3_max_attempts', 3)
        addressing = getattr(self.cfg, 's3_addressing_style', 'auto')
        if not (0 < connect <= 120 and 0 < read <= 600 and isinstance(attempts, int) and 1 <= attempts <= 10):
            raise ValueError('Invalid S3 timeout or retry limits')
        if addressing not in {'auto', 'path', 'virtual'}:
            raise ValueError('Invalid S3 addressing style')
        if client is not None:
            self.client = client
            return
        import boto3
        from botocore.config import Config

        credentials = {}
        if access:
            credentials = {'aws_access_key_id': access, 'aws_secret_access_key': secret}
            if token:
                credentials['aws_session_token'] = token
        # Empty explicit credentials deliberately use the SDK provider chain (roles,
        # profiles, environment). Credential errors propagate; they never select local.
        self.client = boto3.client(
            's3', endpoint_url=endpoint, verify=verify, region_name=self.cfg.aws_region,
            config=Config(signature_version='s3v4', connect_timeout=connect, read_timeout=read,
                          ignore_configured_endpoint_urls=True,
                          retries={'mode': 'standard', 'total_max_attempts': attempts},
                          s3={'addressing_style': addressing},
                          request_checksum_calculation='when_required',
                          response_checksum_validation='when_required'),
            **credentials,
        )

    def local_path(self, key: str) -> Path:
        validate_key(key)
        path = self.root / key
        # Do not follow symlinks within the object namespace, even into root.
        cursor = self.root
        if cursor.is_symlink():
            raise ValueError('Object paths must not contain symlinks')
        for part in key.split('/'):
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError('Object paths must not contain symlinks')
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root.resolve()):
            raise ValueError('Invalid object key')
        return resolved

    def delete_prefix(self, prefix: str):
        """Delete a document namespace, with a slash boundary and retry-safe deletes."""
        validate_key(prefix)
        if self.backend == 'local':
            root = self.local_path(prefix)
            if root.exists():
                for path in root.rglob('*'):
                    self.local_path(path.relative_to(self.root.resolve()).as_posix())
                shutil.rmtree(root)
            return
        prefix += '/'
        # Purge versions too on providers that support versioned buckets.
        try:
            versioned = self.client.get_bucket_versioning(Bucket=self.cfg.s3_bucket).get('Status') in {'Enabled', 'Suspended'}
        except Exception as exc:
            if _error_code(exc) not in {'NotImplemented', '501'}:
                raise
            versioned = False
        operation = 'list_object_versions' if versioned else 'list_objects_v2'
        for page in self.client.get_paginator(operation).paginate(Bucket=self.cfg.s3_bucket, Prefix=prefix):
            entries = page.get('Versions', []) + page.get('DeleteMarkers', []) if versioned else page.get('Contents', [])
            objects = [{'Key': row['Key'], **({'VersionId': row['VersionId']} if versioned else {})} for row in entries]
            for start in range(0, len(objects), 1000):
                result = self.client.delete_objects(Bucket=self.cfg.s3_bucket, Delete={'Objects': objects[start:start + 1000], 'Quiet': True})
                if result.get('Errors'):
                    raise RuntimeError('Object cleanup incomplete')

    def ensure_bucket(self):
        """Check availability only. Application startup must never provision S3."""
        if self.backend == 's3':
            self.client.head_bucket(Bucket=self.cfg.s3_bucket)

    def provision_bucket(self, *, confirm_bucket: str, enable_versioning: bool = True,
                         block_public_access: bool = True):
        """Explicit operator-only creation; no changes to an already existing bucket.

        Compatible providers may not implement AWS security controls. The operator
        must explicitly disable unsupported calls and configure equivalent policies.
        A provisioning error can leave a newly created bucket; never auto-delete it.
        """
        if self.backend != 's3' or confirm_bucket != self.cfg.s3_bucket:
            raise ValueError('Provisioning requires S3 and exact bucket confirmation')
        from botocore.exceptions import ClientError
        try:
            self.ensure_bucket()
            return {'status': 'exists', 'bucket': self.cfg.s3_bucket}
        except ClientError as exc:
            if _error_code(exc) not in {'404', 'NoSuchBucket', 'NotFound'}:
                raise
        options = {} if self.cfg.aws_region == 'us-east-1' else {
            'CreateBucketConfiguration': {'LocationConstraint': self.cfg.aws_region}}
        self.client.create_bucket(Bucket=self.cfg.s3_bucket, **options)
        if block_public_access:
            self.client.put_public_access_block(Bucket=self.cfg.s3_bucket, PublicAccessBlockConfiguration={
                'BlockPublicAcls': True, 'IgnorePublicAcls': True,
                'BlockPublicPolicy': True, 'RestrictPublicBuckets': True})
        if enable_versioning:
            self.client.put_bucket_versioning(Bucket=self.cfg.s3_bucket,
                                              VersioningConfiguration={'Status': 'Enabled'})
        return {'status': 'created', 'bucket': self.cfg.s3_bucket}

    @contextmanager
    def _write_guard(self):
        if self.backend != 's3' or self.write_mode != 'serialized-local':
            yield
            return
        # This coordinates only clients using this project's shared DATA_DIR.
        # It is NOT a distributed lock or a server-side conditional-write claim.
        import fcntl
        lock = Path(self.cfg.data_dir) / '.s3-write.lock'
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def put_file(self, key: str, source: Path, *, expected_sha256: str | None = None,
                 if_absent: bool = False):
        with self._write_guard():
            return self._put_file(key, source, expected_sha256=expected_sha256, if_absent=if_absent)

    def _put_file(self, key: str, source: Path, *, expected_sha256: str | None = None,
                  if_absent: bool = False):
        validate_key(key)
        source = Path(source)
        digest = file_digest(source)
        if expected_sha256 is not None and digest != _digest(expected_sha256):
            raise IntegrityError('Source SHA-256 does not match the expected digest')
        if self.backend == 's3':
            content_type = mimetypes.guess_type(source.name)[0] or 'application/octet-stream'
            extra = {'ContentType': content_type, 'Metadata': {'sha256': digest}}
            if if_absent and self.write_mode == 'serialized-local':
                try:
                    self.client.head_object(Bucket=self.cfg.s3_bucket, Key=key)
                except Exception as exc:
                    if _error_code(exc) not in {'404', 'NoSuchKey', 'NotFound'}:
                        raise
                else:
                    raise ObjectConflictError('Existing local S3 object was not overwritten')
                self.client.upload_file(str(source), self.cfg.s3_bucket, key, ExtraArgs=extra)
            elif if_absent:
                # A conditional PUT cannot silently overwrite another writer. Do not
                # emulate it with HEAD followed by an unconditional upload.
                if source.stat().st_size > 5 * 1024**3:
                    raise ValueError('Create-only migration objects are limited to 5 GiB per object')
                md5 = hashlib.md5(usedforsecurity=False)
                with source.open('rb') as stream:
                    for part in iter(lambda: stream.read(1024 * 1024), b''):
                        md5.update(part)
                    stream.seek(0)
                    try:
                        self.client.put_object(Bucket=self.cfg.s3_bucket, Key=key, Body=stream,
                                               ContentLength=source.stat().st_size,
                                               ContentMD5=base64.b64encode(md5.digest()).decode('ascii'),
                                               IfNoneMatch='*', **extra)
                    except Exception as exc:
                        if _error_code(exc) in {'412', 'PreconditionFailed', 'ConditionalRequestConflict', '409'}:
                            raise ObjectConflictError('Create-only S3 object already exists or changed concurrently') from exc
                        raise
            else:
                self.client.upload_file(str(source), self.cfg.s3_bucket, key, ExtraArgs=extra)
            # ETags are NOT content hashes (multipart uploads and encryption differ).
            # Verify every write by streaming the object back, including its metadata.
            self.verify_object(key, expected_sha256=digest)
            if file_digest(source) != digest:
                raise IntegrityError('Source changed during upload; remote object requires review')
        else:
            target = self.local_path(key)
            if source.resolve() == target:
                if if_absent:
                    raise ObjectConflictError('Create-only local object already exists')
                return self.uri(key)
            self._atomic_local_copy(source, target, digest, if_absent=if_absent)
        return self.uri(key)

    def put_json(self, key: str, data):
        with self._write_guard():
            return self._put_json(key, data)

    def _put_json(self, key: str, data):
        validate_key(key)
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        digest = hashlib.sha256(body).hexdigest()
        if self.backend == 's3':
            self.client.put_object(Bucket=self.cfg.s3_bucket, Key=key, Body=body,
                                   ContentType='application/json', Metadata={'sha256': digest},
                                   ContentMD5=base64.b64encode(hashlib.md5(body, usedforsecurity=False).digest()).decode('ascii'))
            self.verify_object(key, expected_sha256=digest)
        else:
            target = self.local_path(key)
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix='.kb-write-', dir=target.parent)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, target)
            finally:
                Path(name).unlink(missing_ok=True)
        return self.uri(key)

    @staticmethod
    def _atomic_local_copy(source, target, digest, *, if_absent=False):
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.kb-copy-', dir=target.parent)
        try:
            with os.fdopen(fd, 'wb') as out, source.open('rb') as incoming:
                shutil.copyfileobj(incoming, out, length=1024 * 1024)
                out.flush()
                os.fsync(out.fileno())
            if file_digest(Path(name)) != digest or file_digest(source) != digest:
                raise IntegrityError('Source changed or local copy failed SHA-256 verification')
            if if_absent:
                try:
                    os.link(name, target)
                except FileExistsError as exc:
                    raise ObjectConflictError('Create-only local object already exists') from exc
            else:
                os.replace(name, target)
        finally:
            Path(name).unlink(missing_ok=True)

    def _read_s3(self, key, *, expected_sha256=None, output=None):
        if expected_sha256 is not None:
            _digest(expected_sha256)
        response = self.client.get_object(Bucket=self.cfg.s3_bucket, Key=key)
        body = response['Body']
        try:
            recorded = response.get('Metadata', {}).get('sha256')
            if recorded is not None:
                try:
                    _digest(recorded)
                except ValueError as exc:
                    raise IntegrityError('Invalid S3 SHA-256 metadata') from exc
            if recorded is None and expected_sha256 is None:
                raise IntegrityError('S3 object has no SHA-256 metadata; supply an independently trusted digest')
            digest = hashlib.sha256()
            size = 0
            while part := body.read(1024 * 1024):
                digest.update(part)
                size += len(part)
                if output is not None:
                    output.write(part)
            actual = digest.hexdigest()
            if (size != response['ContentLength'] or (recorded is not None and actual != recorded)
                    or (expected_sha256 is not None and actual != expected_sha256)):
                raise IntegrityError('S3 object failed SHA-256 or length verification')
            return {'sha256': actual, 'size': size, 'metadata_sha256': recorded,
                    'version_id': response.get('VersionId')}
        finally:
            body.close()

    def verify_object(self, key: str, *, expected_sha256: str | None = None):
        validate_key(key)
        if self.backend == 's3':
            return self._read_s3(key, expected_sha256=expected_sha256)
        path = self.local_path(key)
        actual = file_digest(path)
        if expected_sha256 is not None and actual != _digest(expected_sha256):
            raise IntegrityError('Local object failed SHA-256 verification')
        return {'sha256': actual, 'size': path.stat().st_size, 'metadata_sha256': None, 'version_id': None}

    def fetch(self, key: str, destination: Path, *, expected_sha256: str | None = None):
        validate_key(key)
        if expected_sha256 is not None:
            _digest(expected_sha256)
        destination = Path(destination)
        if self.backend == 'local':
            source = self.local_path(key)
            digest = file_digest(source)
            if expected_sha256 is not None and digest != expected_sha256:
                raise IntegrityError('Local object failed SHA-256 verification')
            if source == destination.resolve():
                return
            self._atomic_local_copy(source, destination, digest)
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.kb-fetch-', dir=destination.parent)
        try:
            with os.fdopen(fd, 'wb') as output:
                self._read_s3(key, expected_sha256=expected_sha256, output=output)
                output.flush()
                os.fsync(output.fileno())
            # A failed download never replaces a previously good destination.
            os.replace(name, destination)
        finally:
            Path(name).unlink(missing_ok=True)

    def uri(self, key: str) -> str:
        validate_key(key)
        return f's3://{self.cfg.s3_bucket}/{key}' if self.backend == 's3' else f'local://{key}'

    def health(self):
        self.ensure_bucket()
        return {'status': 'ok', 'backend': self.backend,
                'write_mode': self.write_mode if self.backend == 's3' else 'atomic-local-files'}


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(part)
    return digest.hexdigest()
