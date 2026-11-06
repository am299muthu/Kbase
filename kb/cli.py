import argparse
import json
import os
import secrets
import time
from collections import Counter
from datetime import date
from pathlib import Path

from sqlalchemy import func, select

from kb.config import ROOT, settings
from kb.db import Document, Employee, Folder, Grant, audit, db_session, init_db
from kb.governance import GovernanceError, MAKER_ROLES, entitled, request_processing, require_hybrid_ingestion
from kb.pipeline import archive_expired, process_next
from kb.search import ensure_index
from kb.security import password_hash
from kb.storage import ObjectStore, file_digest

BUSINESSES = [
    ('retail-banking', 'Retail Banking', 'Consumer Financial Services', 'Customer Operations'),
    ('technology', 'Technology', 'Group Technology', 'Service Operations'),
    ('risk-compliance', 'Risk and Compliance', 'Group Risk', 'Compliance'),
    ('finance', 'Finance', 'Group Finance', 'Financial Control'),
    ('people', 'People', 'Group People', 'Employee Services'),
]
GIVEN = ['Alex', 'Blair', 'Casey', 'Drew', 'Ellis', 'Finley', 'Gray', 'Harper', 'Indigo', 'Jordan']


def seed():
    cfg = settings()
    if cfg.app_mode == 'infrastructure' and cfg.demo_password == 'local-demo-change-me':
        raise SystemExit('Set a unique DEMO_PASSWORD before provisioning infrastructure users. Replace demo auth with bank SSO before production.')
    records, controls = [], []
    with db_session() as db:
        for group, (folder_id, business, division, department) in enumerate(BUSINESSES):
            for position in range(10):
                employee_id = f'E{group * 10 + position + 1:03d}'
                role = ['owner', 'checker', 'maker', 'delegate'][position] if position < 4 else 'reader'
                record = {'id': employee_id, 'name': f'{GIVEN[position]} Demo{group + 1}', 'business_unit': business, 'synthetic': True}
                records.append(record)
                if not db.get(Employee, employee_id):
                    db.add(Employee(id=employee_id, name=record['name'], business_unit=business, password_hash=password_hash(cfg.demo_password)))
                controls.append({'folder_id': folder_id, 'employee_id': employee_id, 'role': role})
            db.flush()
            if not db.get(Folder, folder_id):
                db.add(Folder(id=folder_id, name=business, path=folder_id, owner_id=f'E{group * 10 + 1:03d}',
                              business_unit=business, division=division, department=department))
                db.flush()
            for control in controls[-10:]:
                existing = db.scalar(select(Grant).where(Grant.folder_id == folder_id, Grant.employee_id == control['employee_id'], Grant.role == control['role']))
                if not existing:
                    db.add(Grant(**control))
        audit(db, 'bootstrap', 'synthetic.staff_seeded', 'staff', {'count': 50})
    # Explicitly synthetic fixture files. Never export passwords or hashes.
    mock = ROOT / 'mockdata'
    mock.mkdir(exist_ok=True)
    (mock / 'employees.json').write_text(json.dumps(records, indent=2))
    (mock / 'access-control.json').write_text(json.dumps(controls, indent=2))


def onboard(path: Path, folder_id: str, maker_id: str, checker_id: str | None, approve: bool, latest_date: str | None = None) -> str | None:
    cfg = settings()
    if approve or checker_id:
        raise GovernanceError('CLI cannot impersonate a checker. Stage as maker, then use an independently authenticated HTTP checker review.', 403)
    require_hybrid_ingestion(path.name)
    if path.stat().st_size > cfg.max_upload_mb * 1024 * 1024:
        raise ValueError('File exceeds MAX_UPLOAD_MB; configure an appropriate reviewed limit.')
    from kb.parsers import SUPPORTED_EXTENSIONS
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError('Unsupported format; compressed wiki archives require a separate controlled importer.')
    with db_session() as db:
        folder = db.get(Folder, folder_id)
        if not folder:
            raise ValueError('Unknown folder')
        entitled(db, maker_id, folder_id, MAKER_ROLES)
        digest = file_digest(path)
        duplicate = db.scalar(select(Document.id).where(Document.folder_id == folder_id, Document.title == path.name, Document.sha256 == digest))
        if duplicate:
            return None
        document_id = secrets.token_hex(16)
        version = (db.scalar(select(func.max(Document.version)).where(Document.folder_id == folder_id, Document.title == path.name)) or 0) + 1
        key = f'staging/{folder.id}/{document_id}/original{path.suffix.lower()}'
        ObjectStore().put_file(key, path)
        metadata = {key: getattr(folder, key) for key in ['business_unit', 'division', 'department', 'region', 'organisation']}
        doc = Document(id=document_id, title=path.name, folder_id=folder_id, maker_id=maker_id, checker_id=None,
                       status='pending', staging_key=key, sha256=digest, version=version,
                       latest_date=latest_date or date.today().isoformat(), metadata_json=metadata)
        db.add(doc)
        audit(db, maker_id, 'document.uploaded', document_id, {'folder_id': folder_id, 'version': version, 'sha256': digest, 'operator_cli': True})
        request_processing(db, document_id, maker_id, kind='initial', idempotency_key='upload:' + document_id)
    return document_id


def seed_documents():
    """Only user-reviewed synthetic fixtures, never KM_Data."""
    fixtures = ROOT / 'mockdata' / 'policies'
    for group, (folder_id, *_rest) in enumerate(BUSINESSES):
        paths = sorted(fixtures.glob(folder_id + '*.md'))
        for path in paths:
            onboard(path, folder_id, f'E{group * 10 + 3:03d}', None, False)
    retail = sorted(fixtures.glob('retail-banking-policy.md'))
    if retail:
        from docx import Document as WordDocument
        target = settings().data_dir / 'synthetic-retail-policy.docx'
        document = WordDocument()
        for line in retail[0].read_text().splitlines():
            if line.startswith('#'):
                document.add_heading(line.lstrip('# ').strip(), level=min(len(line) - len(line.lstrip('#')), 3))
            elif line.strip():
                document.add_paragraph(line)
        document.save(target)
        onboard(target, 'retail-banking', 'E003', None, False)


def main():
    parser = argparse.ArgumentParser(description='ABC Knowledge Base operations. No external model calls unless enabled in .env.')
    sub = parser.add_subparsers(dest='command', required=True)
    setup = sub.add_parser('init', help='Create schema, object bucket/index and 50 synthetic employee records.')
    setup.add_argument('--demo-documents', action='store_true', help='Also ingest explicitly synthetic policy fixtures.')
    serve = sub.add_parser('serve')
    serve.add_argument('--port', type=int)
    serve.add_argument('--partial-corpus', action='store_true', help='Keep search on while the index is still being re-populated (startup gate becomes a warning).')
    serve.add_argument('--maintenance', action='store_true', help='Serve approvals/status with chat and the embedded worker paused.')
    sub.add_parser('inventory', help='Count corpus paths and sizes WITHOUT opening their content.')
    ingest = sub.add_parser('ingest', help='Operator-controlled, bounded and idempotent file onboarding.')
    ingest.add_argument('path', type=Path)
    ingest.add_argument('--folder', required=True)
    ingest.add_argument('--maker', required=True)
    ingest.add_argument('--checker', help='Removed: checker identity cannot be supplied by an operator.')
    ingest.add_argument('--limit', type=int, default=1)
    ingest.add_argument('--auto-approve', action='store_true', help='Removed: requires independent authenticated checker approval.')
    ingest.add_argument('--latest-date')
    reindex = sub.add_parser('reindex', help='Rebuild OpenSearch from already-approved curated objects; no new files or approvals.')
    reindex.add_argument('--folder')
    reindex.add_argument('--limit', type=int, default=1000)
    sub.add_parser('worker')
    sub.add_parser('archive')
    recovery = sub.add_parser('recover', help='Fail only exact interrupted requests after all workers are stopped; new checker approval required.')
    recovery.add_argument('--folder', required=True)
    recovery.add_argument('--document-id', action='append', required=True)
    reprocess = sub.add_parser('reprocess', help='Submit exact existing documents for maker/checker-reviewed reprocessing; does not execute.')
    reprocess.add_argument('--folder', required=True)
    reprocess.add_argument('--document-id', action='append', required=True)
    reprocess.add_argument('--maker', required=True)
    reprocess.add_argument('--idempotency-key', required=True)
    plan = sub.add_parser('object-plan', help='Create an offline manifest for explicit object keys only.')
    plan.add_argument('--keys-file', type=Path, required=True)
    plan.add_argument('--manifest', type=Path, required=True)
    migrate = sub.add_parser('object-migrate', help='Verify migration offline by default; --apply performs explicitly confirmed S3 writes.')
    migrate.add_argument('--manifest', type=Path, required=True)
    migrate.add_argument('--manifest-sha256', required=True)
    migrate.add_argument('--apply', action='store_true')
    migrate.add_argument('--confirm-bucket')
    migrate.add_argument('--confirm-endpoint')
    migrate.add_argument('--receipt', type=Path)
    provision = sub.add_parser('object-provision', help='Explicitly provision only the exact confirmed configured S3 bucket.')
    provision.add_argument('--confirm-bucket', required=True)
    provision.add_argument('--no-versioning', action='store_true')
    provision.add_argument('--no-public-access-block', action='store_true')
    sub.add_parser('profile-check', help='Read-only hybrid readiness check; never activates or sends model requests.')
    sub.add_parser('mcp-check', help='Discover tools and exercise an authenticated MCP lookup using synthetic E003.')
    args = parser.parse_args()
    cfg = settings()
    if args.command == 'object-plan':
        from kb.storage_migration import plan_migration
        print(json.dumps(plan_migration(cfg.data_dir / 'objects', json.loads(args.keys_file.read_text()), args.manifest)))
        return
    if args.command == 'object-migrate':
        from kb.storage_migration import copy_migration
        print(json.dumps(copy_migration(args.manifest, expected_manifest_sha256=args.manifest_sha256,
            source_root=cfg.data_dir / 'objects', apply=args.apply,
            store=ObjectStore() if args.apply else None, confirm_bucket=args.confirm_bucket,
            confirm_endpoint=args.confirm_endpoint, receipt_path=args.receipt)))
        return
    if args.command == 'object-provision':
        print(json.dumps(ObjectStore().provision_bucket(confirm_bucket=args.confirm_bucket,
            enable_versioning=not args.no_versioning, block_public_access=not args.no_public_access_block)))
        return
    if args.command == 'profile-check':
        from kb.runtime import check_hybrid_readiness
        report = check_hybrid_readiness()
        print(json.dumps(report, indent=2))
        if not report['ready']:
            raise SystemExit(2)
        return
    if args.command == 'ingest' and (args.auto_approve or args.checker):
        parser.error('CLI auto-approval is disabled. Stage as maker, then sign in separately as checker.')
    init_db()
    if args.command == 'init':
        seed()
        ObjectStore().ensure_bucket()
        ensure_index()
        if args.demo_documents:
            seed_documents()
        print('Initialised controls. Synthetic documents, if staged, require independent checker review before ingestion.')
    elif args.command == 'serve':
        import uvicorn
        if args.maintenance:
            os.environ['KB_MAINTENANCE'] = '1'
        if args.partial_corpus:
            os.environ['KB_PARTIAL_CORPUS'] = '1'
        uvicorn.run('kb.app:app', host=cfg.host, port=args.port or cfg.port)
    elif args.command == 'inventory':
        files = [path for path in cfg.corpus_dir.rglob('*') if path.is_file() and not path.name.startswith('.')]
        print(json.dumps({'files': len(files), 'bytes': sum(path.stat().st_size for path in files), 'extensions': dict(Counter(path.suffix.lower() for path in files))}, indent=2))
    elif args.command == 'ingest':
        if not 1 <= args.limit <= 1000:
            parser.error('--limit must be 1–1000; use repeated bounded batches for large loads')
        if args.latest_date:
            if date.fromisoformat(args.latest_date) > date.today():
                parser.error('Latest date cannot be in the future')
        from kb.parsers import SUPPORTED_EXTENSIONS
        path = args.path.resolve()
        paths = [path] if path.is_file() else sorted(path.rglob('*'))
        counts = Counter()
        for source in paths:
            if not source.is_file() or source.name.startswith('.') or source.suffix.lower() not in SUPPORTED_EXTENSIONS:
                counts['unsupported_or_not_file'] += 1
                continue
            if counts['new'] >= args.limit:
                break
            try:
                doc_id = onboard(source, args.folder, args.maker, None, False, args.latest_date)
                counts['pending_checker' if doc_id else 'unchanged'] += 1
                if doc_id:
                    counts['new'] += 1
            except Exception as exc:
                counts['errors'] += 1
                print(f'Skipped one file: {type(exc).__name__}. Source text was not logged.')
        print(json.dumps(counts))
    elif args.command == 'reindex':
        if not 1 <= args.limit <= 1000:
            parser.error('--limit must be 1–1000')
        from kb.reindex import reindex_documents
        result = reindex_documents(args.folder, args.limit)
        print(json.dumps(result))
        if result['errors']:
            raise SystemExit(1)
    elif args.command == 'worker':
        print('Worker running. Press Ctrl-C to stop.')
        try:
            while True:
                if not process_next():
                    time.sleep(2)
        except KeyboardInterrupt:
            pass
    elif args.command == 'archive':
        print(json.dumps({'archived': archive_expired()}))
    elif args.command == 'recover':
        from kb.reprocess import recover_requests
        print(json.dumps({'failed_for_fresh_review': recover_requests(args.folder, args.document_id)}))
    elif args.command == 'reprocess':
        from kb.reprocess import request_reprocessing
        print(json.dumps({'requests': request_reprocessing(args.folder, args.document_id, args.maker, args.idempotency_key)}))
    elif args.command == 'mcp-check':
        import asyncio
        from kb.security import issue_session
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        import sys
        async def check():
            with db_session() as db:
                if not db.get(Employee, 'E003'):
                    raise SystemExit('Run init first.')
                token = issue_session(db, 'E003')
            try:
                async with stdio_client(StdioServerParameters(command=sys.executable, args=['-m', 'kb.mcp_server'], cwd=str(ROOT), env={**os.environ, 'KB_MCP_SESSION': token})) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        result = await session.call_tool('lookupemployeeaccesscontrol', {})
                        if result.isError or len(tools.tools) < 5:
                            raise RuntimeError('MCP smoke check failed')
                        print(json.dumps({'tools': [tool.name for tool in tools.tools], 'authenticated_call': 'ok'}))
            finally:
                from kb.db import LoginSession
                from sqlalchemy import delete
                import hashlib
                with db_session() as db:
                    db.execute(delete(LoginSession).where(LoginSession.token_hash == hashlib.sha256(token.encode()).hexdigest()))
        asyncio.run(check())


if __name__ == '__main__':
    main()
