import asyncio
import contextlib
import hashlib
import json
import os
import re
import secrets
import tempfile
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError

from kb.agent import answer_question
from kb.config import ROOT, settings
from kb.db import Audit, Document, Employee, Folder, Grant, LoginSession, ProcessingRequest, audit, db_session, init_db
from kb.governance import GovernanceError, MAKER_ROLES, entitled, lock_document, request_json, request_processing, review_request
from kb.pipeline import process_next
from kb.reprocess import cancel_request, request_reprocessing
from kb.search import client as search_client
from kb.security import ROLES, authenticate, folder_roles, issue_session, password_matches, permitted_folders, pii, require_role
from kb.storage import ObjectStore, file_digest


@asynccontextmanager
async def lifespan(app):
    from kb.telemetry import configure
    configure()
    init_db()
    await asyncio.to_thread(ObjectStore().ensure_bucket)
    maintenance = os.environ.get('KB_MAINTENANCE') == '1'
    if settings().opensearch_vector_search and not maintenance:
        from kb.runtime import check_hybrid_readiness
        report = await asyncio.to_thread(check_hybrid_readiness)
        if not report['ready']:
            raise RuntimeError('Hybrid startup blocked: ' + '; '.join(report['blocking']))
    async def worker():
        while True:
            try:
                worked = await asyncio.to_thread(process_next)
                if not worked:
                    await asyncio.sleep(2)
            except Exception:
                await asyncio.sleep(5)
    task = asyncio.create_task(worker()) if not maintenance else None
    yield
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title=settings().app_name, lifespan=lifespan)
from kb.telemetry import TelemetryMiddleware
app.add_middleware(TelemetryMiddleware)
app.mount('/static', StaticFiles(directory=ROOT / 'kb' / 'static'), name='static')
login_attempts = defaultdict(deque)


@app.exception_handler(GovernanceError)
async def governance_error_handler(request, exc):
    from fastapi.responses import JSONResponse
    return JSONResponse({'detail': str(exc)}, status_code=exc.status_code)


@app.middleware('http')
async def browser_security(request, call_next):
    if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
        origin = request.headers.get('origin')
        if request.headers.get('sec-fetch-site') == 'cross-site' or (origin and urlsplit(origin).netloc != request.headers.get('host')):
            from fastapi.responses import JSONResponse
            return JSONResponse({'detail': 'Cross-origin state changes are not allowed.'}, status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    response.headers['Cache-Control'] = 'no-store'
    return response


def connection():
    with db_session() as db:
        yield db


def user_context(request: Request, db=Depends(connection)):
    return authenticate(db, request.cookies.get('kb_session'))


def user_json(user):
    return {'id': user.id, 'name': user.name, 'business_unit': user.business_unit, 'pii_unmasked': user.pii_unmasked}


class LoginBody(BaseModel):
    employee_id: str = Field(max_length=32)
    password: str = Field(max_length=200)


class ChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=5, ge=1, le=12)


class ReviewBody(BaseModel):
    decision: str
    request_id: str = Field(min_length=1, max_length=32)
    scope_sha256: str = Field(default='', max_length=64)
    note: str = Field(default='', max_length=500)
    control_evidence: dict[str, str] = Field(default_factory=dict)


class ReprocessBody(BaseModel):
    folder_id: str = Field(min_length=1, max_length=64)
    document_ids: list[str] = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(min_length=1, max_length=120)


class FolderBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    parent_id: str | None = None


class GrantBody(BaseModel):
    employee_id: str = Field(max_length=32)
    role: str
    enabled: bool


class MetadataBody(BaseModel):
    latest_date: date | None = None
    metadata: dict = Field(default_factory=dict)


def validate_metadata(metadata: dict):
    if not isinstance(metadata, dict):
        raise HTTPException(422, 'Metadata must be a JSON object.')
    allowed = {'description', 'classification', 'domain', 'owner_note', 'licence_reference'}
    if len(json.dumps(metadata)) > 6000 or set(metadata) - allowed:
        raise HTTPException(422, 'Metadata must use documented fields and be under 6 KB.')
    if any(not isinstance(value, (str, bool)) for value in metadata.values()):
        raise HTTPException(422, 'Metadata values must be strings or booleans.')
    return metadata


def folder_json(db, folder, user):
    result = {key: getattr(folder, key) for key in ['id', 'name', 'path', 'parent_id', 'owner_id', 'business_unit', 'division', 'department', 'region', 'organisation']} | {'roles': sorted(folder_roles(db, user.id, folder.id))}
    return pii.protect(result, user.pii_unmasked)


def document_json(db, document, user):
    result = {key: getattr(document, key) for key in ['id', 'title', 'folder_id', 'status', 'version', 'latest_date', 'maker_id', 'checker_id', 'created_at', 'error']}
    result['folder_path'] = db.get(Folder, document.folder_id).path
    result['metadata'] = document.metadata_json
    request = db.scalar(select(ProcessingRequest).where(ProcessingRequest.document_id == document.id)
                        .order_by(ProcessingRequest.created_at.desc()).limit(1))
    result['processing_request'] = request_json(request) if request else None
    # Technical source/scope digests must not be PII-masked; they are review tokens.
    # IDs and source keys are technical; descriptive content is filtered at all UI exits.
    if not user.pii_unmasked:
        for key in ['title', 'metadata', 'folder_path']:
            result[key] = pii.protect(result[key])
    return result


@app.get('/')
def index():
    return FileResponse(ROOT / 'kb' / 'static' / 'index.html')


@app.get('/api/demo-users')
def demo_users(db=Depends(connection)):
    if settings().app_mode != 'demo':
        return {'users': [], 'demo_password': None}
    return {'users': [user_json(user) for user in db.scalars(select(Employee).where(Employee.active == True).order_by(Employee.id))],
            'demo_password': settings().demo_password}


@app.post('/api/login')
def login(body: LoginBody, request: Request, response: Response, db=Depends(connection)):
    address = request.client.host if request.client else 'unknown'
    attempts = login_attempts[address]
    now = time.monotonic()
    while attempts and now - attempts[0] > 300:
        attempts.popleft()
    if len(attempts) >= 10:
        raise HTTPException(429, 'Too many login attempts. Try again in five minutes.')
    user = db.get(Employee, body.employee_id)
    if not user or not user.active or not password_matches(body.password, user.password_hash):
        attempts.append(now)
        raise HTTPException(401, 'Invalid employee ID or password.')
    attempts.clear()
    token = issue_session(db, user.id)
    response.set_cookie('kb_session', token, httponly=True, secure=settings().cookie_secure, samesite='strict', max_age=settings().session_ttl_hours * 3600)
    audit(db, user.id, 'auth.login', user.id)
    return {'user': user_json(user)}


@app.post('/api/logout')
def logout(request: Request, response: Response, db=Depends(connection)):
    token = request.cookies.get('kb_session', '')
    db.execute(delete(LoginSession).where(LoginSession.token_hash == hashlib.sha256(token.encode()).hexdigest()))
    response.delete_cookie('kb_session')
    return {'ok': True}


@app.get('/api/me')
def me(user=Depends(user_context)):
    return {'user': user_json(user)}


@app.get('/api/folders')
def folders(user=Depends(user_context), db=Depends(connection)):
    allowed = permitted_folders(db, user.id)
    return {'folders': [folder_json(db, folder, user) for folder in db.scalars(select(Folder).where(Folder.id.in_(allowed)).order_by(Folder.path))]}


@app.post('/api/folders')
def create_folder(body: FolderBody, user=Depends(user_context), db=Depends(connection)):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 _-]{0,79}', body.name):
        raise HTTPException(422, 'Use letters, numbers, spaces, hyphens or underscores for folder names.')
    if not body.parent_id:
        raise HTTPException(403, 'Business roots are provisioned by the Data Office; create a subfolder under a root you manage.')
    parent = db.get(Folder, body.parent_id)
    require_role(db, user.id, body.parent_id, {'owner', 'delegate'})
    if not parent:
        raise HTTPException(404, 'Folder not found.')
    folder = Folder(id=secrets.token_hex(12), name=body.name, path=parent.path + '/' + body.name,
                    parent_id=parent.id, owner_id=user.id, business_unit=parent.business_unit,
                    division=parent.division, department=parent.department, region=parent.region, organisation=parent.organisation)
    db.add(folder)
    try:
        db.flush()
    except IntegrityError:
        raise HTTPException(409, 'A folder with this path already exists.')
    db.add(Grant(folder_id=folder.id, employee_id=user.id, role='owner'))
    audit(db, user.id, 'folder.created', folder.id)
    return {'folder': folder_json(db, folder, user)}


@app.get('/api/employees')
def employees(user=Depends(user_context), db=Depends(connection)):
    if not any(folder_roles(db, user.id, folder_id) & {'owner', 'delegate'} for folder_id in permitted_folders(db, user.id)):
        raise HTTPException(403, 'Folder management permission required.')
    return {'employees': [user_json(employee) for employee in db.scalars(select(Employee).order_by(Employee.id))]}


@app.get('/api/folders/{folder_id}/grants')
def grants(folder_id: str, user=Depends(user_context), db=Depends(connection)):
    require_role(db, user.id, folder_id, {'owner', 'delegate'})
    rows = db.execute(select(Grant, Employee).join(Employee, Employee.id == Grant.employee_id).where(Grant.folder_id == folder_id))
    return {'grants': [{'employee_id': employee.id, 'employee_name': employee.name, 'role': grant.role} for grant, employee in rows]}


@app.put('/api/folders/{folder_id}/grants')
def change_grant(folder_id: str, body: GrantBody, user=Depends(user_context), db=Depends(connection)):
    if body.role not in ROLES:
        raise HTTPException(422, 'Unknown role.')
    # Transaction-wide writer/row lock also serializes last-owner checks on SQLite.
    require_role(db, user.id, folder_id, {'owner', 'delegate'})
    db.execute(update(Folder).where(Folder.id == folder_id).values(owner_id=Folder.owner_id))
    folder = db.scalar(select(Folder).where(Folder.id == folder_id).with_for_update())
    roles = folder_roles(db, user.id, folder_id)
    require_role(db, user.id, folder_id, {'owner', 'delegate'})
    if 'owner' not in roles and body.role in {'owner', 'delegate'}:
        raise HTTPException(403, 'Only an owner may delegate ownership.')
    if not db.get(Employee, body.employee_id):
        raise HTTPException(404, 'Employee not found.')
    existing = db.scalar(select(Grant).where(Grant.folder_id == folder_id, Grant.employee_id == body.employee_id, Grant.role == body.role))
    if body.enabled and not existing:
        db.add(Grant(folder_id=folder_id, employee_id=body.employee_id, role=body.role))
    elif not body.enabled and existing:
        if body.role == 'owner':
            owners = list(db.scalars(select(Grant).where(Grant.folder_id == folder_id, Grant.role == 'owner')))
            if len(owners) <= 1:
                raise HTTPException(409, 'Every folder must retain at least one direct owner.')
            if folder.owner_id == body.employee_id:
                folder.owner_id = next(owner.employee_id for owner in owners if owner.employee_id != body.employee_id)
        db.delete(existing)
    audit(db, user.id, 'folder.grant_changed', folder_id, body.model_dump())
    return {'ok': True}


@app.get('/api/documents')
def documents(user=Depends(user_context), db=Depends(connection)):
    allowed = permitted_folders(db, user.id)
    rows = db.scalars(select(Document).where(Document.folder_id.in_(allowed)).order_by(Document.created_at.desc()).limit(500))
    return {'documents': [document_json(db, document, user) for document in rows]}


@app.post('/api/documents')
def upload_document(file: UploadFile = File(...), folder_id: str = Form(...), latest_date: str | None = Form(None), metadata: str = Form('{}'), user=Depends(user_context), db=Depends(connection)):
    entitled(db, user.id, folder_id, MAKER_ROLES)
    folder = db.scalar(select(Folder).where(Folder.id == folder_id).with_for_update())
    title = Path((file.filename or '').replace('\\', '/')).name
    from kb.governance import require_hybrid_ingestion
    require_hybrid_ingestion(title)
    if not title or len(title) > 255 or any(ord(char) < 32 for char in title):
        raise HTTPException(422, 'Invalid filename.')
    from kb.parsers import SUPPORTED_EXTENSIONS
    if Path(title).suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise HTTPException(422, 'Unsupported file format. Wiki archives require a separate reviewed import.')
    try:
        business_metadata = validate_metadata(json.loads(metadata))
        if not isinstance(business_metadata, dict):
            raise ValueError()
        current_date = date.fromisoformat(latest_date) if latest_date else date.today()
    except (ValueError, TypeError):
        raise HTTPException(422, 'Invalid metadata JSON or latest date.')
    if current_date > date.today():
        raise HTTPException(422, 'Latest date cannot be in the future.')
    # Business fields are always server-derived, never accepted from the maker.
    business_metadata |= {key: getattr(folder, key) for key in ['business_unit', 'division', 'department', 'region', 'organisation']}
    doc_id = secrets.token_hex(16)
    version = (db.scalar(select(func.max(Document.version)).where(Document.folder_id == folder_id, Document.title == title)) or 0) + 1
    with tempfile.NamedTemporaryFile(dir=settings().data_dir, suffix=Path(title).suffix) as temp:
        total = 0
        while part := file.file.read(1024 * 1024):
            total += len(part)
            if total > settings().max_upload_mb * 1024 * 1024:
                raise HTTPException(413, 'File exceeds MAX_UPLOAD_MB.')
            temp.write(part)
        if not total:
            raise HTTPException(422, 'Empty files cannot be onboarded.')
        temp.flush()
        source = Path(temp.name)
        digest = file_digest(source)
        key = f'staging/{folder.id}/{doc_id}/original{Path(title).suffix.lower()}'
        ObjectStore().put_file(key, source)
    document = Document(id=doc_id, title=title, folder_id=folder_id, version=version, latest_date=current_date.isoformat(), maker_id=user.id,
                        metadata_json=business_metadata, staging_key=key, sha256=digest, status='pending')
    db.add(document)
    try:
        db.flush()
    except IntegrityError:
        raise HTTPException(409, 'A concurrent upload changed this version. Please retry.')
    audit(db, user.id, 'document.uploaded', document.id, {'sha256': digest, 'version': version, 'folder_id': folder_id})
    request_processing(db, document.id, user.id, kind='initial', idempotency_key='upload:' + document.id)
    return {'document': document_json(db, document, user)}


@app.post('/api/documents/{document_id}/review')
def review_document(document_id: str, body: ReviewBody, user=Depends(user_context), db=Depends(connection)):
    request = db.get(ProcessingRequest, body.request_id)
    if not request or request.document_id != document_id:
        raise HTTPException(404, 'Processing request not found for this document.')
    review_request(db, request.id, user.id, decision=body.decision, scope_sha256=body.scope_sha256,
                   note=pii.mask(body.note), control_evidence={key: pii.mask(value) for key, value in body.control_evidence.items()})
    return {'document': document_json(db, db.get(Document, document_id), user)}


@app.post('/api/reprocessing')
def reprocess_documents(body: ReprocessBody, user=Depends(user_context)):
    return {'requests': request_reprocessing(body.folder_id, body.document_ids, user.id, body.idempotency_key)}


@app.post('/api/processing-requests/{request_id}/cancel')
def cancel_processing(request_id: str, user=Depends(user_context), db=Depends(connection)):
    return {'request': request_json(cancel_request(db, request_id, user.id))}


@app.post('/api/documents/{document_id}/retry')
def retry_document(document_id: str, user=Depends(user_context), db=Depends(connection)):
    document = lock_document(db, document_id)
    entitled(db, user.id, document.folder_id, MAKER_ROLES)
    if document.status not in {'error', 'pending', 'rejected', 'approved'}:
        raise HTTPException(409, 'Use an exact-ID reprocessing request for an indexed document.')
    # Also provides a path to submit legacy pending/auto-approved documents for
    # a real checker review. No legacy approval is silently grandfathered.
    request_processing(db, document.id, user.id, kind='initial', idempotency_key='retry:' + secrets.token_hex(16))
    return {'document': document_json(db, document, user)}


def _failure_audit(db, request_ids):
    """Latest ingestion.failed audit detail per request id."""
    found = {}
    if not request_ids:
        return found
    for item in db.scalars(select(Audit).where(Audit.action == 'ingestion.failed').order_by(Audit.id.desc()).limit(2000)):
        rid = (item.details or {}).get('request_id')
        if rid in request_ids and rid not in found:
            found[rid] = item
    return found


@app.get('/api/processing-requests/{request_id}/pdf-layout')
def pdf_layout(request_id: str, user=Depends(user_context), db=Depends(connection)):
    request = db.get(ProcessingRequest, request_id)
    if not request:
        raise HTTPException(404, 'Request not found.')
    document = db.get(Document, request.document_id)
    entitled(db, user.id, document.folder_id, {'maker', 'checker', 'owner', 'delegate'})
    for entry in db.scalars(select(Audit).where(Audit.action == 'ingestion.pdf_layout')
                           .order_by(Audit.id.desc())):
        if (entry.details or {}).get('request_id') == request_id:
            return {**entry.details, 'captured_at': entry.created_at}
    # Older runs already persisted visual-object geometry in their parsed artifact.
    from botocore.exceptions import ClientError
    try:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'parsed.json'
            ObjectStore().fetch(f'raw-parsed/{document.id}/{request.id}/content.json', path)
            parsed = json.loads(path.read_text())
        objects = [{key: asset[key] for key in
                    ('object_id', 'object_kind', 'page', 'bbox', 'extractor') if key in asset}
                   for asset in parsed.get('assets', []) if asset.get('bbox')]
        return {'request_id': request.id, 'objects': objects, 'coordinate_origin': 'top-left',
                'coverage': 'Legacy run: stored visual crops only; text-region geometry requires a new run.'}
    except FileNotFoundError:
        pass
    except ClientError as exc:
        if str(exc.response.get('Error', {}).get('Code')) not in {'NoSuchKey', '404', 'NotFound'}:
            raise
    raise HTTPException(404, 'No geometry snapshot for this run. Snapshots are captured on new PDF runs after parsing completes.')


@app.get('/api/ingestion')
def ingestion_status(user=Depends(user_context), db=Depends(connection)):
    """Per-document ingestion monitor: latest request, stage and failure diagnostics."""
    allowed = permitted_folders(db, user.id)
    docs = list(db.scalars(select(Document).where(Document.folder_id.in_(allowed)).order_by(Document.created_at.desc()).limit(500)))
    requests = {}
    for request in db.scalars(select(ProcessingRequest).where(ProcessingRequest.document_id.in_([d.id for d in docs]))
                              .order_by(ProcessingRequest.created_at)):
        requests.setdefault(request.document_id, []).append(request)
    from kb.db import ProcessingProgress
    progress = {p.request_id: p for p in db.scalars(select(ProcessingProgress).where(
        ProcessingProgress.request_id.in_([r.id for items in requests.values() for r in items])))}
    latest = {doc_id: max(items, key=lambda r: (
        r.status in {'pending', 'approved', 'processing'},
        progress[r.id].updated_at if r.id in progress else r.created_at))
        for doc_id, items in requests.items()}
    failures = _failure_audit(db, {r.id for r in latest.values() if r.status == 'failed'})
    roles_cache = {}
    items = []
    counts = defaultdict(int)
    for doc in docs:
        request = latest.get(doc.id)
        if doc.folder_id not in roles_cache:
            roles_cache[doc.folder_id] = folder_roles(db, user.id, doc.folder_id)
        roles = roles_cache[doc.folder_id]
        if request is None:
            stage = 'no_request'
        elif request.status == 'failed':
            stage = 'failed'
        else:
            stage = {'pending': 'awaiting_checker', 'approved': 'queued', 'processing': 'processing',
                     'completed': 'indexed', 'cancelled': 'cancelled', 'rejected': 'rejected'}.get(request.status, request.status)
            if doc.status == 'indexed' and request.status in {'cancelled', 'rejected'}:
                stage = 'indexed'  # A cancelled replacement does not remove the published generation.
        counts[stage] += 1
        failure = None
        if request is not None and request.status == 'failed':
            details = (failures.get(request.id).details if request.id in failures else {}) or {}
            can_see = bool(roles & {'owner', 'delegate', 'maker', 'checker'})
            failure = {'message': request.error, 'code': details.get('error_code'), 'stage': details.get('stage'),
                       'error_type': details.get('error_type'),
                       'detail': pii.mask(details.get('error_detail') or '') if can_see else None,
                       'model_responses': ([{**entry, 'response': pii.mask(entry.get('response') or '')}
                                            for entry in details.get('model_diagnostics', [])] if can_see else []),
                       'diagnostics_visible': can_see}
        history = [{'id': r.id, 'kind': r.kind, 'status': r.status, 'created_at': r.created_at, 'reviewed_at': r.reviewed_at}
                   for r in requests.get(doc.id, [])][-6:]
        current = progress.get(request.id) if request else None
        items.append({'document_id': doc.id, 'title': pii.protect(doc.title, user.pii_unmasked), 'folder_id': doc.folder_id,
                      'current_step': current.stage if current else (failure or {}).get('stage'),
                      'progress': current.details if current else {},
                      'progress_updated_at': current.updated_at if current else None,
                      'folder_path': pii.protect(db.get(Folder, doc.folder_id).path, user.pii_unmasked),
                      'document_status': doc.status, 'version': doc.version, 'stage': stage,
                      'request_id': request.id if request else None, 'request_kind': request.kind if request else None,
                      'request_status': request.status if request else None,
                      'maker_id': request.maker_id if request else doc.maker_id, 'checker_id': request.checker_id if request else None,
                      'created_at': request.created_at if request else doc.created_at,
                      'reviewed_at': request.reviewed_at if request else None,
                      'failure': failure, 'history': history,
                      'can_retry': bool(request and request.status == 'failed' and roles & {'owner', 'delegate', 'maker', 'checker'})})
    return {'items': items, 'counts': dict(counts), 'generated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}


@app.post('/api/processing-requests/{request_id}/retry')
def retry_failed_request(request_id: str, user=Depends(user_context), db=Depends(connection)):
    """Retry a failed request. A maker or checker may re-queue it directly when the
    existing checker approval still binds the unchanged source, metadata and scope;
    otherwise a fresh maker request (needing checker review) is created."""
    from kb.governance import CHECKER_ROLES, validate_approval
    failed = db.get(ProcessingRequest, request_id)
    if not failed:
        raise HTTPException(404, 'Processing request not found.')
    document = lock_document(db, failed.document_id)
    roles = folder_roles(db, user.id, document.folder_id)
    if not roles & (set(MAKER_ROLES) | set(CHECKER_ROLES)):
        raise HTTPException(403, 'Maker or checker access is required to retry.')
    db.refresh(failed)
    if failed.status != 'failed':
        raise HTTPException(409, 'Only failed processing requests can be retried.')
    blocking = db.scalar(select(ProcessingRequest.id).where(
        ProcessingRequest.document_id == document.id, ProcessingRequest.id != failed.id,
        (ProcessingRequest.status.in_({'pending', 'approved', 'processing'}))
        | ((ProcessingRequest.status == 'completed') & (ProcessingRequest.created_at > failed.created_at))).limit(1))
    if blocking:
        raise HTTPException(409, 'A newer or open processing request already exists for this document.')
    if failed.kind == 'initial' and document.status not in {'error', 'approved'}:
        raise HTTPException(409, 'Document is not in a retryable state.')
    if failed.kind == 'reprocess' and document.status not in {'indexed', 'archived'}:
        raise HTTPException(409, 'Document is not in a retryable state.')
    still_approved = bool(failed.checker_id and failed.reviewed_at)
    approved_config = failed.scope_json.get('config', {})
    still_approved = still_approved and bool(
        approved_config.get('opensearch_vector_search') and approved_config.get('external_models_enabled')
        and approved_config.get('embedding_backend') == 'openrouter'
        and (not document.title.lower().endswith('.pdf') or (
            approved_config.get('pdf_vision_enabled') and approved_config.get('allow_external_vision_images'))))
    if still_approved:
        failed.status = 'approved'  # validate_approval accepts approved requests only
        try:
            validate_approval(db, failed, document, check_runtime=False)
        except GovernanceError:
            still_approved = False
    if still_approved:
        failed.error = None
        if failed.kind == 'initial':
            document.status = 'approved'
            document.error = None
        audit(db, user.id, 'processing.retried_under_existing_approval', document.id,
              {'request_id': failed.id, 'checker_id': failed.checker_id, 'scope_sha256': failed.scope_sha256})
        return {'request': request_json(failed), 'mode': 'requeued'}
    failed.status = 'failed'
    entitled(db, user.id, document.folder_id, MAKER_ROLES)
    if failed.kind == 'initial':
        document.maker_id = user.id
    new = request_processing(db, document.id, user.id, kind=failed.kind, idempotency_key='retry:' + secrets.token_hex(16))
    audit(db, user.id, 'processing.retry_requested', document.id, {'previous_request_id': failed.id, 'request_id': new.id})
    return {'request': request_json(new), 'mode': 'new_request'}


@app.patch('/api/documents/{document_id}/metadata')
def update_metadata(document_id: str, body: MetadataBody, user=Depends(user_context), db=Depends(connection)):
    document = lock_document(db, document_id)
    entitled(db, user.id, document.folder_id, MAKER_ROLES)
    if document.status in {'approved', 'processing'}:
        raise HTTPException(409, 'Cancel pending work or wait for ingestion before updating metadata.')
    if db.scalar(select(ProcessingRequest.id).where(ProcessingRequest.document_id == document.id,
                                                    ProcessingRequest.status == 'processing')):
        raise HTTPException(409, 'Wait for active reprocessing to finish before updating metadata.')
    values = validate_metadata(body.metadata)
    if body.latest_date:
        if body.latest_date > date.today():
            raise HTTPException(422, 'Latest date cannot be in the future.')
        document.latest_date = body.latest_date.isoformat()
    document.metadata_json = {**document.metadata_json, **values}
    # Every metadata/date change invalidates the complete approval binding.
    # Existing chunks remain durable, but retrieval pauses until a fresh review.
    for request in db.scalars(select(ProcessingRequest).where(ProcessingRequest.document_id == document.id,
                                                             ProcessingRequest.status.in_({'pending', 'approved'}))):
        request.status = 'cancelled'
        audit(db, user.id, 'processing.invalidated', document.id,
              {'request_id': request.id, 'reason': 'metadata_changed'})
    document.status = 'pending'
    document.checker_id = None
    # The editor becomes responsible maker for the changed source metadata.
    document.maker_id = user.id
    request_processing(db, document.id, user.id, kind='initial', idempotency_key='metadata:' + secrets.token_hex(16))
    audit(db, user.id, 'document.metadata_updated', document.id, {'fields': sorted(values), 'latest_date': document.latest_date})
    return {'document': document_json(db, document, user)}


@app.get('/api/audit')
def audit_events(user=Depends(user_context), db=Depends(connection)):
    folders = permitted_folders(db, user.id)
    docs = list(db.scalars(select(Document.id).where(Document.folder_id.in_(folders))))
    rows = db.scalars(select(Audit).where((Audit.actor_id == user.id) | Audit.target_id.in_(folders + docs)).order_by(Audit.id.desc()).limit(200))
    return {'events': [{'id': item.id, 'created_at': item.created_at, 'actor_id': item.actor_id, 'action': item.action,
                        'target_id': item.target_id, 'details': pii.protect(item.details, user.pii_unmasked)} for item in rows]}


@app.post('/chat')
async def chat(body: ChatBody, request: Request, user=Depends(user_context)):
    if os.environ.get('KB_MAINTENANCE') == '1':
        raise HTTPException(503, 'Search is paused during ingestion maintenance. The Ingestion monitor remains available.')
    if not body.message.strip():
        raise HTTPException(422, 'Enter a question.')
    return await answer_question(request.cookies['kb_session'], body.message, body.top_k)


@app.get('/health')
async def health():
    services = {}
    with db_session() as db:
        try:
            db.execute(text('SELECT 1'))
            services['database'] = {'status': 'ok', 'backend': 'sqlite' if settings().database_url.startswith('sqlite') else 'postgresql'}
        except Exception:
            services['database'] = {'status': 'unavailable'}
    try:
        services['storage'] = await asyncio.to_thread(ObjectStore().health)
    except Exception:
        services['storage'] = {'status': 'unavailable'}
    try:
        if settings().search_backend == 'opensearch':
            status = await asyncio.to_thread(lambda: search_client().cluster.health()['status'])
            services['search'] = {'status': 'ok' if status in {'green', 'yellow'} else 'unavailable', 'cluster': status, 'backend': 'opensearch',
                                  'index': settings().opensearch_index, 'retrieval': 'hybrid' if settings().opensearch_vector_search else 'bm25',
                                  'ingestion_policy': 'hybrid_required',
                                  'bm25_fallback_index': settings().opensearch_bm25_fallback_index,
                                  'embeddings_used': settings().opensearch_vector_search}
        else:
            services['search'] = {'status': 'ok', 'backend': 'sqlite-lexical-demo'}
    except Exception:
        services['search'] = {'status': 'unavailable', 'backend': settings().search_backend}
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        import sys
        async with asyncio.timeout(15):
            async with stdio_client(StdioServerParameters(command=sys.executable, args=['-m', 'kb.mcp_server'], cwd=str(ROOT), env=dict(os.environ))) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    services['mcp'] = {'status': 'ok', 'tools': len(result.tools), 'transport': 'stdio'}
    except Exception:
        services['mcp'] = {'status': 'unavailable'}
    services['pii'] = {'status': 'ok' if settings().app_mode == 'demo' or pii.mode != 'rules-only-demo'
                       or (settings().external_models_enabled and settings().guardrails_enabled) else 'unavailable',
                       'detector': pii.mode, 'inference_guardrails_enabled': settings().guardrails_enabled and settings().external_models_enabled,
                       'model_availability_probed': False}
    maintenance = os.environ.get('KB_MAINTENANCE') == '1'
    if settings().opensearch_vector_search and not maintenance:
        from kb.runtime import check_hybrid_readiness
        readiness = await asyncio.to_thread(check_hybrid_readiness)
        services['search']['status'] = 'ok' if readiness['ready'] else 'unavailable'
        services['search']['corpus_verified'] = readiness['ready']
        services['search']['verified_chunks'] = readiness['verified_chunks']
    return {'status': 'maintenance' if maintenance else ('ok' if all(item['status'] == 'ok' for item in services.values()) else 'degraded'),
            'maintenance': maintenance, 'mode': settings().app_mode, 'services': services,
            'model': settings().llm_provider, 'external_models_enabled': settings().external_models_enabled}
