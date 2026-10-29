#!/usr/bin/env python3
"""Read-only service diagnostics; optional synthetic OpenRouter model probes."""
import argparse
import base64
import io
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import ssl
import sys
import time
from urllib.parse import quote, urlsplit

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
CHECKS = ('app', 'opensearch', 'postgres', 'openrouter-embedding', 'openrouter-inference',
          'openrouter-image-embedding', 'openrouter-vision')


@dataclass
class Result:
    check: str
    status: str
    detail: str
    duration_ms: float = 0


def skipped(name, detail):
    return Result(name, 'skipped', detail)


def failure(name, detail):
    return Result(name, 'fail', detail)


def endpoint(value, *, external=False):
    parsed = urlsplit(value)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        raise ValueError('Invalid service URL')
    if external and parsed.scheme != 'https':
        raise ValueError('External probes require HTTPS')
    return value.rstrip('/')


def tls_context(cfg):
    if cfg.get('OPENSEARCH_VERIFY_CERTS', 'true').lower() not in {'true', '1', 'yes'}:
        raise ValueError('Health checks refuse disabled OpenSearch certificate verification')
    ca = cfg.get('OPENSEARCH_CA_CERTS')
    return ssl.create_default_context(cafile=str((ROOT / ca).resolve()) if ca else None)


def response_error(name, response):
    if response.is_error or response.is_redirect:
        return failure(name, f'HTTP {response.status_code}; response body withheld.')
    return None


def check_app(cfg, args):
    base = endpoint(args.app_url or cfg.get('HEALTH_APP_URL') or 'http://127.0.0.1:8000')
    with httpx.Client(timeout=args.timeout, follow_redirects=False, trust_env=False) as web:
        response = web.get(base + '/health')
    error = response_error('app', response)
    if error:
        return error
    data = response.json()
    services = data.get('services')
    if data.get('status') != 'ok' or not isinstance(services, dict) or not services:
        return failure('app', 'Application reports unavailable or malformed health.')
    if any(not isinstance(value, dict) or value.get('status') != 'ok' for value in services.values()):
        return failure('app', 'One or more application dependencies report unavailable.')
    return Result('app', 'ok', 'GET /health: application and reported dependencies healthy.')


def check_opensearch(cfg, args):
    base = endpoint(cfg.get('OPENSEARCH_URL') or 'http://localhost:9200')
    username = cfg.get('OPENSEARCH_USERNAME', '')
    password = cfg.get('OPENSEARCH_PASSWORD', '')
    if password and not username:
        return failure('opensearch', 'Password configured without a username.')
    if username and urlsplit(base).scheme != 'https':
        return failure('opensearch', 'Refusing to send service credentials over HTTP.')
    auth = (username, password) if username else None
    with httpx.Client(timeout=args.timeout, verify=tls_context(cfg), auth=auth,
                      follow_redirects=False, trust_env=False) as web:
        for suffix in ('/', '/_cluster/health'):
            response = web.get(base + suffix)
            error = response_error('opensearch', response)
            if error:
                return error
            data = response.json()
            if suffix == '/' and not isinstance(data.get('version'), dict):
                return failure('opensearch', 'Root endpoint did not return server version metadata.')
            if suffix != '/' and (data.get('status') != 'green' or data.get('timed_out')):
                return failure('opensearch', 'Cluster is degraded, unavailable, or health request timed out.')
        index = cfg.get('OPENSEARCH_INDEX') or 'abc-kb-curated-v1'
        if any(character in index for character in ('*', '?', ',', '/')) or index in {'.', '..', '_all'}:
            return failure('opensearch', 'Configure one exact index name for its readiness check.')
        response = web.head(base + '/' + quote(index, safe=''))
        error = response_error('opensearch', response)
        if error:
            return error
    return Result('opensearch', 'ok', 'Root, cluster health (green), and configured index endpoints reachable; no document reads.')


def check_postgres(cfg, args):
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    raw = cfg.get('POSTGRES_HEALTH_URL') or cfg.get('DATABASE_URL', '')
    if not raw or not raw.startswith(('postgresql://', 'postgresql+', 'postgres://')):
        return skipped('postgres', 'No PostgreSQL DSN; set POSTGRES_HEALTH_URL (app DATABASE_URL may remain SQLite).')
    if raw.startswith('postgres://'):
        raw = 'postgresql://' + raw.removeprefix('postgres://')
    url = make_url(raw).set(drivername='postgresql+psycopg')
    engine = create_engine(url, connect_args={
        'connect_timeout': max(1, math.ceil(args.timeout)),
        'options': f'-c statement_timeout={max(1, int(args.timeout * 1000))}',
    }, hide_parameters=True)
    try:
        with engine.connect() as connection:
            if connection.execute(text('SELECT 1')).scalar_one() != 1:
                return failure('postgres', 'Authenticated SELECT 1 returned an unexpected result.')
    finally:
        engine.dispose()
    return Result('postgres', 'ok', 'Authenticated database connection and SELECT 1 passed; no table reads or writes.')


def _synthetic_image():
    from PIL import Image, ImageDraw
    image = Image.new('RGB', (128, 128), 'white')
    ImageDraw.Draw(image).rectangle((24, 24, 104, 104), fill='blue')
    stream = io.BytesIO()
    image.save(stream, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(stream.getvalue()).decode('ascii')


def check_model(cfg, args, *, embedding, image=False, vision=False):
    name = ('openrouter-image-embedding' if image else 'openrouter-embedding') if embedding else ('openrouter-vision' if vision else 'openrouter-inference')
    key = cfg.get('OPENROUTER_API_KEY', '')
    if not key:
        return skipped(name, 'OPENROUTER_API_KEY is missing; configure a rotated key privately.')
    model_key = ('OPENROUTER_IMAGE_EMBEDDING_MODEL' if image else 'OPENROUTER_EMBEDDING_MODEL') if embedding else ('OPENROUTER_VISION_MODEL' if vision else 'OPENROUTER_MODEL')
    model = cfg.get(model_key, '')
    if not model:
        return skipped(name, f'{model_key} is missing; configure the exact model ID.')
    if not args.probe_models:
        return skipped(name, 'Configured but not invoked; --probe-models authorizes synthetic API calls that may incur charges.')
    # Never send this credential to an alternative endpoint or follow a redirect.
    base = endpoint(cfg.get('OPENROUTER_BASE_URL') or 'https://openrouter.ai', external=True)
    if base != 'https://openrouter.ai':
        return failure(name, 'Probe only supports the official https://openrouter.ai origin.')
    path_key = 'OPENROUTER_EMBEDDING_PATH' if embedding else 'OPENROUTER_CHAT_PATH'
    expected_path = '/api/v1/embeddings' if embedding else '/api/v1/chat/completions'
    if cfg.get(path_key, expected_path) != expected_path:
        return failure(name, f'{path_key} must match the official API path for this diagnostic.')
    # A zero-price ceiling is a routing constraint, not an independently verified price guarantee.
    provider = {'allow_fallbacks': False, 'max_price': {'prompt': 0, 'completion': 0}}
    if embedding:
        dimension_key = 'IMAGE_EMBEDDING_DIMENSIONS' if image else 'EMBEDDING_DIMENSIONS'
        expected = int(cfg.get(dimension_key) or ('0' if image else '384'))
        if expected < 1:
            return failure(name, f'{dimension_key} must be positive.')
        inputs = [{'content': [{'type': 'image_url', 'image_url': {'url': _synthetic_image()}}]}] if image else ['Synthetic health check: blue sky.']
        payload = {'model': model, 'input': inputs, 'provider': provider}
    else:
        content = [{'type': 'text', 'text': 'Describe this synthetic shape briefly.'},
                   {'type': 'image_url', 'image_url': {'url': _synthetic_image()}}] if vision else 'Synthetic health check. Reply with OK.'
        payload = {'model': model, 'messages': [{'role': 'user', 'content': content}],
                   'max_tokens': 64 if vision else 32, 'stream': False, 'provider': provider}
    with httpx.Client(timeout=args.timeout, follow_redirects=False, trust_env=False) as web:
        response = web.post(base + expected_path, headers={'Authorization': 'Bearer ' + key}, json=payload)
    error = response_error(name, response)
    if error:
        return error
    data = response.json()
    if data.get('error'):
        return failure(name, 'Provider returned an error envelope; body withheld.')
    if embedding:
        rows = data.get('data')
        vector = rows[0].get('embedding') if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict) else None
        if (not isinstance(vector, list) or not vector
                or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in vector)
                or not any(value != 0 for value in vector)):
            return failure(name, 'Embedding response has no valid, finite, nonzero vector.')
        if len(vector) != expected:
            return failure(name, f'Embedding dimension mismatch: received {len(vector)}, expected {expected}.')
        return Result(name, 'ok', f'Synthetic embedding request passed; {len(vector)} dimensions; vector withheld.')
    choices = data.get('choices')
    message = choices[0].get('message') if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
    content = message.get('content') if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        return failure(name, 'Inference response contains no assistant text; body withheld.')
    return Result(name, 'ok', 'Synthetic inference request returned assistant text; content withheld.')


def run_check(name, cfg, args):
    started = time.perf_counter()
    try:
        if name == 'app':
            result = check_app(cfg, args)
        elif name == 'opensearch':
            result = check_opensearch(cfg, args)
        elif name == 'postgres':
            result = check_postgres(cfg, args)
        else:
            result = check_model(cfg, args, embedding=name in {'openrouter-embedding', 'openrouter-image-embedding'},
                                 image=name == 'openrouter-image-embedding', vision=name == 'openrouter-vision')
    except httpx.TimeoutException:
        result = failure(name, 'HTTP request timed out.')
    except httpx.RequestError:
        result = failure(name, 'Connection, DNS, or TLS validation failed; exception details withheld.')
    except Exception as exc:
        # Driver exceptions/URLs/provider bodies can contain credentials or source text.
        result = failure(name, f'{type(exc).__name__}: configuration, connection, or response validation failed; details withheld.')
    result.duration_ms = round((time.perf_counter() - started) * 1000, 2)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env', help='Configuration file; process environment takes precedence.')
    parser.add_argument('--checks', nargs='+', choices=('all',) + CHECKS, default=['all'])
    parser.add_argument('--app-url', help='App base URL; defaults to HEALTH_APP_URL or http://127.0.0.1:8000.')
    parser.add_argument('--timeout', type=float, default=15, help='Per-request/connect timeout in seconds (default: 15).')
    parser.add_argument('--probe-models', action='store_true', help='Authorize synthetic OpenRouter API calls; may incur charges, even for IDs labelled free. Does not enable app model processing.')
    parser.add_argument('--json', action='store_true', help='Emit machine-readable JSON without credentials or response bodies.')
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 120:
        parser.error('--timeout must be greater than 0 and at most 120 seconds')
    cfg = {key: value for key, value in dotenv_values(args.env_file, interpolate=False).items() if value is not None}
    cfg.update(os.environ)
    profile = cfg.get('KB_RUNTIME_PROFILE', '').strip()
    if profile:
        if profile not in {'local', 'hybrid', 'pdf-hybrid'}:
            parser.error('KB_RUNTIME_PROFILE must be local, hybrid or pdf-hybrid')
        values = dotenv_values(ROOT / 'profiles' / (profile + '.env'), interpolate=False)
        allowed = {'LLM_PROVIDER', 'EXTERNAL_MODELS_ENABLED', 'EMBEDDING_BACKEND',
            'OPENSEARCH_INDEX', 'OPENSEARCH_VECTOR_SEARCH', 'PDF_VISION_ENABLED',
            'ALLOW_EXTERNAL_VISION_IMAGES', 'IMAGE_EMBEDDINGS_ENABLED', 'STORAGE_BACKEND',
            'ALLOW_UNMASKED_EXTERNAL', 'GUARDRAILS_ENABLED'}
        if not values or set(values) - allowed or any(value is None for value in values.values()):
            parser.error('Runtime profile is missing or contains unsupported settings')
        cfg.update(values)
    selected = CHECKS if 'all' in args.checks else tuple(dict.fromkeys(args.checks))
    results = [run_check(name, cfg, args) for name in selected]
    exit_code = 1 if any(r.status == 'fail' for r in results) else 2 if any(r.status == 'skipped' for r in results) else 0
    overall = {0: 'ok', 1: 'fail', 2: 'incomplete'}[exit_code]
    if args.json:
        print(json.dumps({'status': overall, 'checked_at': datetime.now(timezone.utc).isoformat(),
                          'checks': [asdict(result) for result in results], 'exit_code': exit_code}, indent=2))
    else:
        for result in results:
            print(f'{result.status.upper():7} {result.check:23} {result.duration_ms:9.2f} ms  {result.detail}')
        print(f'Overall: {overall}; exit code {exit_code}')
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
