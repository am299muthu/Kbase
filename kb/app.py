import asyncio
import contextlib
import hashlib
import json
import logging
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
from kb.db import Audit, Document, Employee, Folder, Grant, LoginSession, ProcessingRequest, ReplacementCleanup, audit, db_session, init_db
from kb.governance import GovernanceError, MAKER_ROLES, digest_json, entitled, processing_scope, lock_document, request_json, request_processing, review_request
from kb.pipeline import process_next
from kb.reprocess import cancel_request, request_reprocessing
from kb.search import client as search_client
from kb.security import ROLES, authenticate, folder_roles, issue_session, password_matches, permitted_folders, require_role
from kb.storage import ObjectStore, file_digest

# Shared with ingestion: PDFium must not run concurrently across threads.
from kb.pdf_render_lock import pdf_render_lock as _pdf_render_lock


@asynccontextmanager
async def lifespan(app):
    from kb.telemetry import configure
    configure()
    init_db()
    from kb.graph_build import recover_interrupted
    recover_interrupted()  # a graph build cut off by a restart is marked failed, not left running
    from kb.entities import recover_interrupted as recover_scans
    recover_scans()  # the same for an entity scan
    await asyncio.to_thread(ObjectStore().ensure_bucket)
    maintenance = os.environ.get('KB_MAINTENANCE') == '1'
    if settings().opensearch_vector_search and not maintenance:
        from kb.runtime import check_hybrid_readiness
        report = await asyncio.to_thread(check_hybrid_readiness)
        if not report['ready']:
            if os.environ.get('KB_PARTIAL_CORPUS') != '1':
                raise RuntimeError('Hybrid startup blocked: ' + '; '.join(report['blocking']))
            # Explicit operator choice (serve --partial-corpus): keep search available while re-indexing continues.
            logging.getLogger('uvicorn.error').warning('Serving a PARTIAL corpus: %s', '; '.join(report['blocking']))
    local_embedding = settings().embedding_backend == 'local' and not maintenance
    if local_embedding:
        # Load the embedding model once now; each question's helper process then reuses it.
        from kb import local_embeddings
        await asyncio.to_thread(local_embeddings.warm)
    async def worker():
        while True:
            try:
                worked = await asyncio.to_thread(process_next)
                if not worked:
                    await asyncio.sleep(2)
            except Exception:
                await asyncio.sleep(5)
    # Parallel ingestion workers. Each claims a request under the document lock, so two workers never
    # parse the same document. Local Docling + Granite needs about 4 GB of GPU memory per worker.
    try:
        worker_count = max(1, min(8, int(os.environ.get('INGEST_WORKERS', '1'))))
    except ValueError:
        worker_count = 1
    tasks = [asyncio.create_task(worker()) for _ in range(worker_count)] if not maintenance else []
    yield
    for task in tasks:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    if local_embedding:
        local_embeddings.stop()


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
    return {'id': user.id, 'name': user.name, 'business_unit': user.business_unit}


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
    return result


def document_json(db, document, user):
    result = {key: getattr(document, key) for key in ['id', 'title', 'folder_id', 'status', 'version', 'latest_date', 'maker_id', 'checker_id', 'created_at', 'error']}
    result['folder_path'] = db.get(Folder, document.folder_id).path
    result['metadata'] = document.metadata_json
    request = db.scalar(select(ProcessingRequest).where(ProcessingRequest.document_id == document.id)
                        .order_by(ProcessingRequest.created_at.desc()).limit(1))
    result['processing_request'] = request_json(request) if request else None
    result['cleanup_pending'] = bool(db.scalar(select(ReplacementCleanup.document_id).where(ReplacementCleanup.replacement_id == document.id, ReplacementCleanup.status != 'completed').limit(1)))
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
    rows = db.scalars(select(Document).where(Document.folder_id.in_(allowed), Document.status != 'superseded').order_by(Document.created_at.desc()).limit(500))
    return {'documents': [document_json(db, document, user) for document in rows]}


@app.post('/api/documents')
def upload_document(file: UploadFile = File(...), folder_id: str = Form(...), latest_date: str | None = Form(None), metadata: str = Form('{}'), replace_ids: str = Form('[]'), duplicate_action: str = Form(''), user=Depends(user_context), db=Depends(connection)):
    entitled(db, user.id, folder_id, MAKER_ROLES)
    db.execute(update(Folder).where(Folder.id == folder_id).values(owner_id=Folder.owner_id))
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
    try:
        confirmed_ids = json.loads(replace_ids)
        if not isinstance(confirmed_ids, list) or any(not isinstance(item, str) for item in confirmed_ids):
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(422, 'Invalid replacement confirmation.')
    from kb.replacement import replacement_candidates, schedule_replacement, filename_key
    if duplicate_action not in {'', 'replace', 'version'} or (confirmed_ids and not duplicate_action):
        raise HTTPException(422, 'Choose replace or keep as a new version.')
    replaced = replacement_candidates(db, folder_id, title, confirmed_ids, duplicate_action)
    # Business fields are always server-derived, never accepted from the maker.
    business_metadata |= {key: getattr(folder, key) for key in ['business_unit', 'division', 'department', 'region', 'organisation']}
    doc_id = secrets.token_hex(16)
    versions = db.scalars(select(Document).where(Document.folder_id == folder_id))
    version = max((doc.version for doc in versions if filename_key(doc.title) == filename_key(title)), default=0) + 1
    # A closed file inside a temporary folder: Windows cannot reopen a NamedTemporaryFile while it is open.
    with tempfile.TemporaryDirectory(dir=settings().data_dir) as temp_dir:
        source = Path(temp_dir) / ('upload' + Path(title).suffix)
        total = 0
        with source.open('wb') as temp:
            while part := file.file.read(1024 * 1024):
                total += len(part)
                if total > settings().max_upload_mb * 1024 * 1024:
                    raise HTTPException(413, 'File exceeds MAX_UPLOAD_MB.')
                temp.write(part)
        if not total:
            raise HTTPException(422, 'Empty files cannot be onboarded.')
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
    schedule_replacement(db, replaced, document, user.id)
    return {'document': document_json(db, document, user), 'cleanup_pending': bool(replaced)}


@app.post('/api/documents/{document_id}/review')
def review_document(document_id: str, body: ReviewBody, user=Depends(user_context), db=Depends(connection)):
    request = db.get(ProcessingRequest, body.request_id)
    if not request or request.document_id != document_id:
        raise HTTPException(404, 'Processing request not found for this document.')
    review_request(db, request.id, user.id, decision=body.decision, scope_sha256=body.scope_sha256,
                   note=body.note, control_evidence={key: value for key, value in body.control_evidence.items()})
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


def _checker_pdf_request(db, user, request_id):
    from kb.governance import CHECKER_ROLES, assert_binding
    request = db.get(ProcessingRequest, request_id)
    if not request:
        raise HTTPException(404, 'Processing request not found.')
    document = db.get(Document, request.document_id)
    entitled(db, user.id, document.folder_id, CHECKER_ROLES)
    if user.id in {request.maker_id, document.maker_id}:
        raise HTTPException(403, 'A different checker must review the source.')
    if request.status != 'pending':
        raise HTTPException(409, 'Source review is available only while awaiting checker approval.')
    assert_binding(request, document, check_runtime=False)
    if not document.title.lower().endswith('.pdf'):
        raise HTTPException(400, 'This source is not a PDF.')
    return request, document


def _checker_pdf_preview(request_id, user, db, page_number=None):
    # Checker source review is a narrow permission for a pending approval, not
    # a general entitlement to view unmasked indexed documents.
    request, document = _checker_pdf_request(db, user, request_id)
    import io
    import pypdfium2 as pdfium
    with tempfile.TemporaryDirectory() as temp:
        source = Path(temp) / 'source.pdf'
        ObjectStore().fetch(document.staging_key, source, expected_sha256=request.source_sha256)
        with _pdf_render_lock, pdfium.PdfDocument(str(source)) as pdf:
            page_count = len(pdf)
            if page_number is None:
                page_sizes = []
                for index in range(page_count):
                    page = pdf[index]
                    try:
                        page_sizes.append(list(page.get_size()))
                    finally:
                        page.close()
                result = {'request_id': request.id, 'page_count': page_count, 'page_sizes': page_sizes,
                          'source_sha256': request.source_sha256, 'version': request.source_version}
            else:
                if not 1 <= page_number <= page_count:
                    raise HTTPException(404, 'Page not found.')
                page = pdf[page_number-1]
                try:
                    width, height = page.get_size()
                    bitmap = page.render(scale=min(1.5, 1800 / max(width, height)))
                    try:
                        output = io.BytesIO()
                        bitmap.to_pil().save(output, format='PNG')
                        result = output.getvalue()
                    finally:
                        bitmap.close()
                finally:
                    page.close()
    db.refresh(request)
    db.refresh(document)
    _checker_pdf_request(db, user, request_id)
    audit(db, user.id, 'processing.checker_source_viewed', document.id,
          {'request_id': request.id, 'page': page_number, 'source_sha256': request.source_sha256})
    return result


@app.get('/api/processing-requests/{request_id}/review-pdf')
def checker_pdf_info(request_id: str, user=Depends(user_context), db=Depends(connection)):
    return _checker_pdf_preview(request_id, user, db)


@app.get('/api/processing-requests/{request_id}/review-pdf/pages/{page_number}')
def checker_pdf_page(request_id: str, page_number: int, user=Depends(user_context), db=Depends(connection)):
    content = _checker_pdf_preview(request_id, user, db, page_number)
    return Response(content, media_type='image/png', headers={'Cache-Control': 'no-store'})


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


@app.get('/api/processing-requests/{request_id}/pdf-layout/annotated.pdf')
def annotated_pdf(request_id: str, layer: str = 'all', user=Depends(user_context), db=Depends(connection)):
    if layer not in {'all', 'layout', 'crops'}:
        raise HTTPException(400, 'Choose all, layout or crops.')
    geometry = pdf_layout(request_id, user, db)
    request = db.get(ProcessingRequest, request_id)
    document = db.get(Document, request.document_id)
    if not document.title.lower().endswith('.pdf'):
        raise HTTPException(400, 'PDF source required.')
    from kb.pdf_debug import annotate_pdf
    with tempfile.TemporaryDirectory() as temp:
        source = Path(temp) / 'source.pdf'
        # Use the original retained for this exact processing run.
        ObjectStore().fetch(f'raw/{document.folder_id}/{document.id}/{request.id}/original.pdf', source)
        content = annotate_pdf(source, geometry['objects'], layer)
    entitled(db, user.id, document.folder_id, {'maker', 'checker', 'owner', 'delegate'})
    db.refresh(user)
    audit(db, user.id, 'ingestion.pdf_debug_download', document.id, {'request_id': request.id, 'layer': layer})
    return Response(content, media_type='application/pdf', headers={
        'Content-Disposition': f'attachment; filename="pdf-layout-{request.id}-{layer}.pdf"',
        'Cache-Control': 'no-store'})


@app.get('/api/processing-requests/{request_id}/pdf-layout/data')
def pdf_extracted_data(request_id: str, user=Depends(user_context), db=Depends(connection)):
    """Tables and chart data extracted in this run, for the object inspector. Same roles as the
    page images; read from the run's parsed record, no model call."""
    pdf_layout(request_id, user, db)
    request = db.get(ProcessingRequest, request_id)
    document = db.get(Document, request.document_id)
    from botocore.exceptions import ClientError
    from kb.pdf_debug import extracted_data
    items = []
    try:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'parsed.json'
            ObjectStore().fetch(f'raw-parsed/{document.id}/{request.id}/content.json', path)
            items = extracted_data(json.loads(path.read_text(encoding='utf-8')))
    except FileNotFoundError:
        pass
    except ClientError as exc:
        if str(exc.response.get('Error', {}).get('Code')) not in {'NoSuchKey', '404', 'NotFound'}:
            raise
    entitled(db, user.id, document.folder_id, {'maker', 'checker', 'owner', 'delegate'})
    db.refresh(user)
    audit(db, user.id, 'ingestion.pdf_debug_data', document.id, {'request_id': request.id, 'items': len(items)})
    return {'request_id': request.id, 'items': items}


@app.get('/api/processing-requests/{request_id}/pdf-layout/page/{page_number}')
def pdf_debug_page(request_id: str, page_number: int, user=Depends(user_context), db=Depends(connection)):
    pdf_layout(request_id, user, db)
    request = db.get(ProcessingRequest, request_id)
    document = db.get(Document, request.document_id)
    import io
    import pypdfium2 as pdfium
    with tempfile.TemporaryDirectory() as temp:
        source = Path(temp) / 'source.pdf'
        ObjectStore().fetch(f'raw/{document.folder_id}/{document.id}/{request.id}/original.pdf', source)
        with _pdf_render_lock, pdfium.PdfDocument(str(source)) as pdf:
            if not 1 <= page_number <= len(pdf):
                raise HTTPException(404, 'Page not found.')
            page = pdf[page_number-1]
            width, height = page.get_size()
            scale = min(1.5, 1800 / max(width, height))
            bitmap = page.render(scale=scale)
            output = io.BytesIO()
            bitmap.to_pil().save(output, format='PNG')
            bitmap.close()
            page.close()
    entitled(db, user.id, document.folder_id, {'maker', 'checker', 'owner', 'delegate'})
    db.refresh(user)
    audit(db, user.id, 'ingestion.pdf_debug_page', document.id, {'request_id': request.id, 'page': page_number})
    return Response(output.getvalue(), media_type='image/png', headers={'Cache-Control':'no-store'})


@app.get('/api/ingestion')
def ingestion_status(user=Depends(user_context), db=Depends(connection)):
    """Per-document ingestion monitor: latest request, stage and failure diagnostics."""
    allowed = permitted_folders(db, user.id)
    docs = list(db.scalars(select(Document).where(Document.folder_id.in_(allowed), Document.status != 'superseded').order_by(Document.created_at.desc()).limit(500)))
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
    cleanups = {job.replacement_id: job for job in db.scalars(select(ReplacementCleanup).where(ReplacementCleanup.status != 'completed'))}
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
        if doc.id in cleanups:
            stage = 'cleaning_up'
        counts[stage] += 1
        failure = None
        if request is not None and request.status == 'failed':
            details = (failures.get(request.id).details if request.id in failures else {}) or {}
            can_see = bool(roles & {'owner', 'delegate', 'maker', 'checker'})
            failure = {'message': request.error, 'code': details.get('error_code'), 'stage': details.get('stage'),
                       'error_type': details.get('error_type'),
                       'detail': details.get('error_detail') or '' if can_see else None,
                       'model_responses': ([{**entry, 'response': entry.get('response') or ''}
                                            for entry in details.get('model_diagnostics', [])] if can_see else []),
                       'diagnostics_visible': can_see}
        history = [{'id': r.id, 'kind': r.kind, 'status': r.status, 'created_at': r.created_at, 'reviewed_at': r.reviewed_at}
                   for r in requests.get(doc.id, [])][-6:]
        current = progress.get(request.id) if request else None
        items.append({'document_id': doc.id, 'title': doc.title, 'folder_id': doc.folder_id,
                      'current_step': (cleanups[doc.id].error or 'Removing previous files and index entries') if doc.id in cleanups else (current.stage if current else (failure or {}).get('stage')),
                      'progress': current.details if current else {},
                      'progress_updated_at': current.updated_at if current else None,
                      'folder_path': db.get(Folder, doc.folder_id).path,
                      'document_status': doc.status, 'version': doc.version, 'stage': stage,
                      'request_id': request.id if request else None, 'request_kind': request.kind if request else None,
                      'request_status': request.status if request else None,
                      'maker_id': request.maker_id if request else doc.maker_id, 'checker_id': request.checker_id if request else None,
                      'created_at': request.created_at if request else doc.created_at,
                      'reviewed_at': request.reviewed_at if request else None,
                      'failure': failure, 'history': history,
                      'can_cancel': bool(request and request.status in {'pending', 'approved'} and request.maker_id == user.id and roles & set(MAKER_ROLES)),
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
    # The approval only carries over if it was granted for exactly the settings the workers run now;
    # otherwise the worker would skip it forever, so a fresh maker request is created instead.
    still_approved = still_approved and failed.scope_sha256 == digest_json(processing_scope())
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
    if document.status == 'superseded':
        raise HTTPException(409, 'This file has been replaced.')
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
                        'target_id': item.target_id, 'details': item.details} for item in rows]}


# ---------------------------------------------------------------- knowledge graph
# Anyone with a grant on a folder can see its graph. Folder owners and checkers can build it and
# review its links (the same people who approve documents). Only approved links reach answers.
GRAPH_EDIT_ROLES = {'owner', 'checker'}


class GraphBuildBody(BaseModel):
    folder_id: str = Field(min_length=1, max_length=64)
    document_ids: list[str] = Field(min_length=1, max_length=200)


class GraphReviewBody(BaseModel):
    decision: str
    note: str = Field(default='', max_length=500)


class GraphBulkReviewBody(BaseModel):
    folder_id: str = Field(min_length=1, max_length=64)
    link_ids: list[str] = Field(min_length=1, max_length=2000)
    decision: str
    note: str = Field(default='', max_length=500)


class GraphNodeBody(BaseModel):
    node_type: str = Field(min_length=1, max_length=40)


def _graph_folder(db, user, folder_id: str, edit: bool = False) -> set:
    if folder_id not in permitted_folders(db, user.id):
        raise HTTPException(404, 'Folder not found.')
    roles = folder_roles(db, user.id, folder_id)
    if edit and not roles & GRAPH_EDIT_ROLES:
        raise HTTPException(403, 'Only the folder owner or a checker can change its knowledge graph.')
    return roles


def _link_json(link, nodes) -> dict:
    return {'id': link.id, 'source': link.source_id, 'target': link.target_id, 'predicate': link.predicate,
            'relation': link.relation, 'confidence': link.confidence, 'score': link.score, 'status': link.status,
            'reviewed_by': link.reviewed_by, 'reviewed_at': link.reviewed_at, 'review_note': link.review_note,
            'source_label': nodes[link.source_id].label if link.source_id in nodes else link.source_id,
            'target_label': nodes[link.target_id].label if link.target_id in nodes else link.target_id}


def _apply_review(link, decision: str, user_id: str, note: str) -> None:
    if decision not in {'approve', 'reject', 'reopen'}:
        raise HTTPException(422, 'Decision must be approve, reject or reopen.')
    from kb.db import now
    link.status = {'approve': 'approved', 'reject': 'rejected', 'reopen': 'proposed'}[decision]
    link.reviewed_by, link.reviewed_at, link.review_note = (user_id, now(), note) if decision != 'reopen' else ('', '', '')


@app.get('/api/graph/ontology')
def graph_ontology(user=Depends(user_context)):
    from kb.ontology import node_types, predicates, version
    return {'version': version(), 'types': node_types(), 'predicates': predicates()}


@app.get('/api/graph')
def graph_view(folder_id: str, user=Depends(user_context), db=Depends(connection)):
    from kb.db import Chunk, GraphBuild, GraphEvidence, GraphLink, GraphNode
    from kb.graph_build import LARGE_DOCUMENT_CHUNKS
    roles = _graph_folder(db, user, folder_id)
    nodes = {n.id: n for n in db.scalars(select(GraphNode).where(GraphNode.folder_id == folder_id))}
    links = list(db.scalars(select(GraphLink).where(GraphLink.folder_id == folder_id)))
    evidence_counts = dict(db.execute(select(GraphEvidence.link_id, func.count()).where(
        GraphEvidence.link_id.in_([l.id for l in links])).group_by(GraphEvidence.link_id)).all())
    builds = db.scalars(select(GraphBuild).where(GraphBuild.folder_id == folder_id).order_by(GraphBuild.created_at.desc()).limit(5))
    can_edit = bool(roles & GRAPH_EDIT_ROLES)
    documents = []
    if can_edit:
        counts = dict(db.execute(select(Chunk.document_id, func.count()).where(Chunk.folder_id == folder_id).group_by(Chunk.document_id)).all())
        for document in db.scalars(select(Document).where(Document.folder_id == folder_id, Document.status == 'indexed').order_by(Document.title)):
            chunks = counts.get(document.id, 0)
            documents.append({'id': document.id, 'title': document.title, 'version': document.version, 'chunks': chunks,
                              'large': chunks > LARGE_DOCUMENT_CHUNKS})
    return {
        'folder_id': folder_id, 'can_build': can_edit, 'can_review': can_edit, 'documents': documents,
        'nodes': [{'id': n.id, 'label': n.label, 'type': n.node_type, 'type_source': n.type_source,
                   'documents': n.document_ids} for n in nodes.values()],
        'links': [{**_link_json(l, nodes), 'evidence': evidence_counts.get(l.id, 0)} for l in links],
        'builds': [{'id': b.id, 'status': b.status, 'stage': b.stage, 'started_by': b.started_by, 'created_at': b.created_at,
                    'finished_at': b.finished_at, 'backend': b.backend, 'ontology_version': b.ontology_version,
                    'documents': len(b.document_ids or []), 'counts': b.counts, 'error': b.error} for b in builds],
    }


@app.get('/api/graph/links/{link_id}')
def graph_link(link_id: str, user=Depends(user_context), db=Depends(connection)):
    from kb.db import GraphEvidence, GraphLink, GraphNode
    link = db.get(GraphLink, link_id)
    if link is None:
        raise HTTPException(404, 'Link not found.')
    _graph_folder(db, user, link.folder_id)
    nodes = {n.id: n for n in (db.get(GraphNode, link.source_id), db.get(GraphNode, link.target_id)) if n}
    titles = {d.id: d.title for d in db.scalars(select(Document).where(Document.folder_id == link.folder_id))}
    evidence = [{'document_id': e.document_id, 'title': titles.get(e.document_id, e.document_id), 'chunk_id': e.chunk_id,
                 'page': e.page, 'quote': e.quote}
                for e in db.scalars(select(GraphEvidence).where(GraphEvidence.link_id == link.id))]
    return {'link': _link_json(link, nodes), 'evidence': evidence}


@app.post('/api/graph/links/{link_id}/review')
def graph_review(link_id: str, body: GraphReviewBody, user=Depends(user_context), db=Depends(connection)):
    from kb.db import GraphLink
    link = db.get(GraphLink, link_id)
    if link is None:
        raise HTTPException(404, 'Link not found.')
    _graph_folder(db, user, link.folder_id, edit=True)
    _apply_review(link, body.decision, user.id, body.note)
    audit(db, user.id, f'graph.link_{body.decision}', link.folder_id, {'link_id': link.id, 'predicate': link.predicate, 'note': body.note})
    return {'id': link.id, 'status': link.status}


@app.post('/api/graph/review')
def graph_bulk_review(body: GraphBulkReviewBody, user=Depends(user_context), db=Depends(connection)):
    from kb.db import GraphLink
    _graph_folder(db, user, body.folder_id, edit=True)
    links = list(db.scalars(select(GraphLink).where(GraphLink.folder_id == body.folder_id, GraphLink.id.in_(body.link_ids))))
    for link in links:
        _apply_review(link, body.decision, user.id, body.note)
    audit(db, user.id, f'graph.links_{body.decision}', body.folder_id, {'count': len(links), 'note': body.note})
    return {'changed': len(links)}


@app.patch('/api/graph/nodes/{node_id}')
def graph_node_type(node_id: str, body: GraphNodeBody, user=Depends(user_context), db=Depends(connection)):
    from kb.db import GraphNode
    from kb.ontology import node_types
    node = db.get(GraphNode, node_id)
    if node is None:
        raise HTTPException(404, 'Node not found.')
    _graph_folder(db, user, node.folder_id, edit=True)
    if body.node_type not in node_types():
        raise HTTPException(422, 'Choose a type from the ontology.')
    before = node.node_type
    node.node_type, node.type_source = body.node_type, 'human'
    audit(db, user.id, 'graph.node_retyped', node.folder_id, {'node_id': node.id, 'before': before, 'after': body.node_type})
    return {'id': node.id, 'type': node.node_type}


@app.post('/api/graph/builds')
def graph_start_build(body: GraphBuildBody, user=Depends(user_context), db=Depends(connection)):
    from kb.graph_build import BuildError, launch, start_build
    _graph_folder(db, user, body.folder_id, edit=True)
    try:
        build = start_build(db, body.folder_id, body.document_ids, user.id)
    except BuildError as exc:
        raise HTTPException(409, str(exc))
    audit(db, user.id, 'graph.build_started', body.folder_id, {'build_id': build.id, 'documents': len(body.document_ids), 'backend': build.backend})
    db.commit()
    launch(build.id)
    return {'id': build.id, 'status': build.status}


@app.get('/api/graph/builds/{build_id}')
def graph_build_status(build_id: str, user=Depends(user_context), db=Depends(connection)):
    from kb.db import GraphBuild
    build = db.get(GraphBuild, build_id)
    if build is None:
        raise HTTPException(404, 'Build not found.')
    _graph_folder(db, user, build.folder_id)
    return {'id': build.id, 'status': build.status, 'stage': build.stage, 'counts': build.counts, 'error': build.error,
            'finished_at': build.finished_at}


class EntityScanBody(BaseModel):
    folder_id: str


@app.get('/api/entities')
def entities_view(folder_id: str, user=Depends(user_context), db=Depends(connection)):
    """Each indexed document's kind and main entities from the latest entity scan (kb/entities.py)."""
    from kb.entities import folder_view
    roles = _graph_folder(db, user, folder_id)
    return {**folder_view(db, folder_id), 'can_scan': bool(roles & GRAPH_EDIT_ROLES)}


@app.get('/api/entities/documents/{document_id}')
def entities_document(document_id: str, user=Depends(user_context), db=Depends(connection)):
    from kb.entities import document_view
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(404, 'Document not found.')
    _graph_folder(db, user, document.folder_id)
    return {**document_view(db, document_id), 'title': document.title}


@app.post('/api/entities/scans')
def entities_start_scan(body: EntityScanBody, user=Depends(user_context), db=Depends(connection)):
    from kb.entities import ScanError, launch, start_scan
    _graph_folder(db, user, body.folder_id, edit=True)
    try:
        scan = start_scan(db, body.folder_id, user.id)
    except ScanError as exc:
        raise HTTPException(409, str(exc))
    audit(db, user.id, 'entities.scan_started', body.folder_id, {'scan_id': scan.id, 'documents': len(scan.document_ids)})
    db.commit()
    launch(scan.id)
    return {'id': scan.id, 'status': scan.status}


class LimitsBody(BaseModel):
    values: dict[str, int | None]


@app.get('/api/settings/processing-limits')
def get_processing_limits(user=Depends(user_context), db=Depends(connection)):
    """Every signed-in employee can see the limits; only developers (KB_DEVELOPERS) can change them."""
    from kb.processing_limits import describe, is_developer
    return {'limits': describe(db), 'can_edit': is_developer(user.id)}


@app.put('/api/settings/processing-limits')
def put_processing_limits(body: LimitsBody, user=Depends(user_context), db=Depends(connection)):
    from kb.processing_limits import describe, is_developer, save
    if not is_developer(user.id):
        raise HTTPException(403, 'Only developers can change processing time limits.')
    try:
        changes = save(db, body.values, user.id)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if changes:
        audit(db, user.id, 'settings.processing_limits', 'processing-limits', {'changes': changes})
    return {'limits': describe(db), 'can_edit': True, 'changed': len(changes)}


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
            services['search'] = {'status': 'ok' if status in {'green', 'yellow'} else 'unavailable', 'cluster': status, 'backend': settings().search_engine,
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
    maintenance = os.environ.get('KB_MAINTENANCE') == '1'
    if settings().opensearch_vector_search and not maintenance:
        from kb.runtime import check_hybrid_readiness
        readiness = await asyncio.to_thread(check_hybrid_readiness)
        partial = os.environ.get('KB_PARTIAL_CORPUS') == '1' and not readiness['ready']
        services['search']['status'] = 'ok' if readiness['ready'] else ('partial' if partial else 'unavailable')
        services['search']['corpus_verified'] = readiness['ready']
        services['search']['verified_chunks'] = readiness['verified_chunks']
        if partial:
            services['search']['note'] = 'Serving a partial corpus while re-indexing continues.'
    return {'status': 'maintenance' if maintenance else ('ok' if all(item['status'] == 'ok' for item in services.values()) else 'degraded'),
            'maintenance': maintenance, 'mode': settings().app_mode, 'services': services,
            'model': settings().llm_provider, 'external_models_enabled': settings().external_models_enabled}
