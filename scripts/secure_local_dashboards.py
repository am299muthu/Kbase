#!/usr/bin/env python3
"""Enable the installed Security Dashboards plugin with existing local accounts."""
import os
import ssl
import sys
import httpx
from pathlib import Path
import shutil
import subprocess
import yaml
from dotenv import dotenv_values
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from kb.config import settings as app_settings
app = app_settings()
context = ssl.create_default_context(cafile=app.opensearch_ca_certs)
context.load_cert_chain(root/'var/opensearch-admin/admin.pem', root/'var/opensearch-admin/admin-key.pem')
with httpx.Client(base_url=app.opensearch_url, verify=context, trust_env=False, timeout=20) as client:
    for name, body in [('roles/kb_dashboards_metadata', {'cluster_permissions': [], 'index_permissions': [{'index_patterns': ['*'], 'allowed_actions': ['indices:admin/get']}]}), ('rolesmapping/kb_dashboards_metadata', {'users':['dashboards_server']})]:
        client.put('/_plugins/_security/api/'+name,json=body).raise_for_status()
prefix = Path(subprocess.check_output(['brew', '--prefix'], text=True).strip())
config = prefix/'etc/opensearch-dashboards/opensearch_dashboards.yml'
backup = root/'var/opensearch-admin/dashboards-before-security.yml'
if not backup.exists():
    shutil.copyfile(config, backup)
    backup.chmod(0o600)
creds = dotenv_values(root/'var/opensearch-admin/dashboards.env')
settings = yaml.safe_load(config.read_text()) or {}
settings.update({'server.host':'127.0.0.1', 'opensearch.username':'dashboards_server',
    'opensearch.password':creds['DASHBOARDS_SERVER_PASSWORD'],
    'opensearch.requestHeadersAllowlist':['securitytenant','Authorization'],
    'opensearch_security.cookie.secure':False,
    'opensearch_security.multitenancy.enabled':False})
settings.pop('opensearch.requestHeadersWhitelist',None)
settings.pop('opensearch.customHeaders',None)
os.umask(0o077)
config.write_text(yaml.safe_dump(settings,sort_keys=False))
config.chmod(0o600)
print('Configured localhost-only Dashboards login with existing accounts; backup saved privately.')
