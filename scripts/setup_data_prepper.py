#!/usr/bin/env python3
"""Provision local Data Prepper TLS/config and its dedicated OpenSearch writer.

Uses the existing local admin certificate only during provisioning. Never
changes kb_service permissions, corpus indexes or document approvals.
"""
import json
import os
from pathlib import Path
import secrets
import ssl
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from kb.config import ROOT, settings


def private_json(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(value, out, indent=2)
    path.chmod(0o600)


def main():
    os.umask(0o077)
    home = ROOT / 'var/telemetry'
    home.mkdir(parents=True, exist_ok=True)
    secret_file = home / 'secrets.json'
    if not secret_file.exists():
        private_json(secret_file, {key: secrets.token_urlsafe(32) for key in ('receiver', 'writer', 'keystore')})
    secret = json.loads(secret_file.read_text())
    openssl = '/opt/homebrew/opt/openssl@3/bin/openssl'
    if not Path(openssl).exists():
        openssl = 'openssl'
    if not (home / 'server.pem').exists():
        subprocess.run([openssl, 'req', '-x509', '-newkey', 'rsa:3072', '-nodes', '-days', '365',
            '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1',
            '-keyout', str(home / 'server-key.pem'), '-out', str(home / 'server.pem')],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    env = {**os.environ, 'KB_KEYSTORE_PASSWORD': secret['keystore']}
    subprocess.run([openssl, 'pkcs12', '-export', '-in', str(home / 'server.pem'),
        '-inkey', str(home / 'server-key.pem'), '-out', str(home / 'server.p12'),
        '-passout', 'env:KB_KEYSTORE_PASSWORD'], env=env, check=True)
    cfg = settings()
    context = ssl.create_default_context(cafile=cfg.opensearch_ca_certs)
    context.load_cert_chain(ROOT / 'var/opensearch-admin/admin.pem', ROOT / 'var/opensearch-admin/admin-key.pem')
    with httpx.Client(base_url=cfg.opensearch_url, verify=context, trust_env=False, timeout=20) as client:
        role = {'cluster_permissions': ['cluster_monitor', 'cluster_composite_ops', 'indices:admin/template/get',
                    'indices:admin/template/put', 'indices:admin/index_template/get', 'indices:admin/index_template/put'],
                'index_permissions': [{'index_patterns': ['*'], 'allowed_actions': ['indices:admin/aliases/get']},
                    {'index_patterns': ['kb-logs-*', 'otel-v1-apm-span*', 'otel-v1-apm-service-map*'],
                    'allowed_actions': ['crud', 'create_index', 'manage']}]}
        for path, body in [
            ('roles/kb_telemetry_writer', role),
            ('internalusers/kb_telemetry_writer', {'password': secret['writer']}),
            ('rolesmapping/kb_telemetry_writer', {'users': ['kb_telemetry_writer']})]:
            response = client.put('/_plugins/_security/api/' + path, json=body)
            if response.is_error:
                raise RuntimeError(f'OpenSearch provisioning failed: {path} HTTP {response.status_code}')
        # Explicit time/ID types make Discover filtering and correlation predictable.
        template = {'index_patterns': ['kb-logs-*'], 'template': {
            'settings': {'number_of_shards': 1, 'number_of_replicas': 0},
            'mappings': {'properties': {'time': {'type': 'date_nanos'},
                'traceId': {'type': 'keyword'}, 'spanId': {'type': 'keyword'},
                'severityText': {'type': 'keyword'}, 'body': {'type': 'keyword'},
                'attributes': {'properties': {'duration_ms': {'type': 'double'}}}}}}}
        response = client.put('/_index_template/kb-logs', json=template)
        response.raise_for_status()
    sink_base = {'hosts': [cfg.opensearch_url], 'cert': cfg.opensearch_ca_certs,
        'username': 'kb_telemetry_writer', 'password': secret['writer'], 'insecure': False,
        'max_retries': 5, 'bulk_size': 1, 'flush_timeout': 1000, 'number_of_shards': 1, 'number_of_replicas': 0}
    def sink(name, **extra):
        return [{'opensearch': {**sink_base, **extra, 'dlq_file': str(home / (name + '-dlq.json'))}}]
    def source(port):
        return {'port': port, 'ssl': True, 'sslKeyCertChainFile': str(home / 'server.pem'),
            'sslKeyFile': str(home / 'server-key.pem'), 'thread_count': 4, 'max_connection_count': 32,
            'authentication': {'http_basic': {'username': 'kb_telemetry', 'password': secret['receiver']}}}
    buffer = {'bounded_blocking': {'buffer_size': 2048, 'batch_size': 64}}
    pipelines = {
        'kb-logs': {'source': {'otel_logs_source': source(21892)}, 'buffer': buffer,
            'sink': sink('logs', index='kb-logs-%{yyyy.MM.dd}')},
        'kb-traces': {'delay': 100, 'source': {'otel_trace_source': source(21890)}, 'buffer': buffer,
            'sink': [{'pipeline': {'name': 'kb-spans'}}, {'pipeline': {'name': 'kb-service-map'}}]},
        'kb-spans': {'source': {'pipeline': {'name': 'kb-traces'}}, 'buffer': buffer,
            'processor': [{'otel_traces': {}}], 'sink': sink('spans', index_type='trace-analytics-raw')},
        'kb-service-map': {'source': {'pipeline': {'name': 'kb-traces'}}, 'buffer': buffer,
            'processor': [{'service_map': {'window_duration': 30}}],
            'sink': sink('service-map', index_type='trace-analytics-service-map')},
    }
    private_json(home / 'pipelines.yaml', pipelines)
    private_json(home / 'data-prepper-config.yaml', {'ssl': True, 'serverPort': 4900,
        'keyStoreFilePath': str(home / 'server.p12'), 'keyStorePassword': secret['keystore'],
        'privateKeyPassword': secret['keystore'],
        'authentication': {'http_basic': {'username': 'kb_telemetry', 'password': secret['receiver']}},
        'peer_forwarder': {'discovery_mode': 'local_node', 'ssl': True,
            'ssl_certificate_file': str(home / 'server.pem'), 'ssl_key_file': str(home / 'server-key.pem'),
            'authentication': {'mutual_tls': {}}}})
    private_json(home / 'client.json', {'ca': str(home / 'server.pem'), 'password': secret['receiver'],
        'traces': 'https://localhost:21890', 'logs': 'https://localhost:21892'})
    print('Provisioned dedicated telemetry writer and private TLS configuration in var/telemetry.')

if __name__ == '__main__':
    main()
