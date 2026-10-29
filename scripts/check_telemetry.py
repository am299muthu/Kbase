#!/usr/bin/env python3
"""Synthetic end-to-end smoke: SDK -> Data Prepper -> OpenSearch. No model calls."""
import json
import os
from pathlib import Path
import sys
import time
import uuid
import subprocess
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kb.config import ROOT, settings
os.environ['KB_TELEMETRY_ENABLED'] = 'true'
from kb.telemetry import operation, event, flush, propagation_env
from opentelemetry.trace import SpanKind
import httpx

smoke_id = uuid.uuid4().hex
with operation('telemetry.smoke', kind=SpanKind.SERVER, smoke_id=smoke_id) as parent:
    trace_id = format(parent.get_span_context().trace_id, '032x')
    event('telemetry.smoke', smoke_id=smoke_id)
    with operation('telemetry.smoke.mcp', kind=SpanKind.CLIENT):
        subprocess.run([sys.executable, '-c', "from kb.telemetry import operation, event, flush; from opentelemetry.trace import SpanKind; exec(\"with operation('telemetry.smoke.child', kind=SpanKind.SERVER):\\n event('telemetry.smoke.child')\"); flush()"], env={**os.environ, **propagation_env()}, check=True)
    try:
        with operation('telemetry.synthetic_failure', smoke_id=smoke_id):
            raise ValueError('PRIVATE_SMOKE_MESSAGE_MUST_NOT_BE_EXPORTED')
    except ValueError:
        pass
flush()
cfg = settings()
secret = json.loads((ROOT/'var/telemetry/secrets.json').read_text())
with httpx.Client(base_url=cfg.opensearch_url, verify=cfg.opensearch_ca_certs,
    auth=('kb_telemetry_writer', secret['writer']), trust_env=False, timeout=10) as client:
    deadline = time.monotonic() + 55
    found = {}
    while time.monotonic() < deadline:
        for index in ('kb-logs-*', 'otel-v1-apm-span*'):
            response = client.post('/'+index+'/_search', json={'size': 20, 'query': {'term': {'traceId': trace_id}}})
            if response.status_code == 200:
                found[index] = response.json()['hits']['hits']
        if all(found.get(index) for index in ('kb-logs-*', 'otel-v1-apm-span*')):
            break
        time.sleep(2)
    assert all(found.get(index) for index in ('kb-logs-*', 'otel-v1-apm-span*')), 'Logs or traces did not arrive within 55 seconds'
    assert 'PRIVATE_SMOKE_MESSAGE' not in json.dumps(found), 'Sensitive exception text leaked'
    spans = [hit['_source'] for hit in found['otel-v1-apm-span*']]
    assert any(span.get('status.code') == 2 for span in spans), 'Missing error span'
    print(json.dumps({'success': True, 'trace_id': trace_id, 'logs': len(found['kb-logs-*']), 'spans': len(spans)}))
