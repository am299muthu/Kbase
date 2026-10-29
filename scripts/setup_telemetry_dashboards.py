#!/usr/bin/env python3
"""Create Discover patterns and trace-to-log correlation for local Dashboards."""
import json
import httpx
from pathlib import Path
from dotenv import dotenv_values
credentials = dotenv_values(Path(__file__).resolve().parents[1] / 'var/opensearch-admin/dashboards.env')
with httpx.Client(base_url='http://127.0.0.1:5601', headers={'osd-xsrf': 'kb-setup'}, auth=(credentials['DASHBOARDS_USER'], credentials['DASHBOARDS_PASSWORD']), trust_env=False, timeout=30) as client:
    for identity, title, field in [('kb-logs', 'kb-logs-*', 'time'), ('kb-traces', 'otel-v1-apm-span-*', 'startTime')]:
        result = client.post('/api/saved_objects/index-pattern/'+identity+'?overwrite=true',
            json={'attributes': {'title': title, 'timeFieldName': field}})
        result.raise_for_status()
    result = client.post('/api/opensearch-dashboards/settings', json={'changes': {
        'observability:traceAnalyticsCorrelatedLogsIndices': 'kb-logs-*',
        'observability:traceAnalyticsCorrelatedLogsFieldMappings': json.dumps({
            'serviceName': 'serviceName', 'spanId': 'spanId', 'timestamp': 'time', 'traceId': 'traceId'})}})
    result.raise_for_status()
    print('Created kb-logs and kb-traces Discover patterns and trace/log correlation settings.')
