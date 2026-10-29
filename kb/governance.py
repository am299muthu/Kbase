"""SQLite-backed maker/checker controls shared by HTTP, CLI and workers.

Only an authenticated HTTP checker review authorises work. Operator-supplied IDs,
legacy checker_id fields and maker metadata never constitute an approval.
"""
import hashlib
import json
import os
import secrets

from sqlalchemy import select, update

from kb.config import settings
from kb.db import ControlAttestation, Document, Employee, ProcessingRequest, audit, now
from kb.security import folder_roles

MAKER_ROLES = {'owner', 'delegate', 'maker'}
CHECKER_ROLES = {'owner', 'delegate', 'checker'}
EXTERNAL_CONTROLS = {'licence', 'risk', 'data_protection', 'legal'}
OPEN_REQUEST_STATES = {'pending', 'approved', 'processing'}


class GovernanceError(ValueError):
    def __init__(self, message, status_code=409):
        super().__init__(message)
        self.status_code = status_code


def require_hybrid_ingestion(title: str):
    cfg = settings()
    if (cfg.search_backend != 'opensearch' or not cfg.opensearch_vector_search
            or cfg.embedding_backend != 'openrouter' or not cfg.external_models_enabled
            or not cfg.guardrails_enabled):
        raise GovernanceError('Hybrid ingestion is mandatory. Select the pdf-hybrid runtime profile; BM25-only uploads and ingestion are disabled.', 409)
    if title.lower().endswith('.pdf') and not (cfg.pdf_vision_enabled and cfg.allow_external_vision_images):
        raise GovernanceError('PDF ingestion requires native parsing and approved VL image processing.', 409)


def digest_json(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def processing_scope() -> dict:
    """Public, explicit approval scope; never serialise settings/credentials wholesale."""
    cfg = settings()
    fields = (
        'search_backend', 'opensearch_url', 'opensearch_index', 'opensearch_vector_search',
        'opensearch_bm25_fallback_index', 'opensearch_hybrid_folders',
        'storage_backend', 's3_endpoint_url', 's3_bucket', 'aws_region', 's3_write_mode',
        'embedding_backend', 'embedding_dimensions', 'openrouter_embedding_model',
        'image_embeddings_enabled', 'image_embedding_dimensions', 'openrouter_image_embedding_model',
        'pdf_vision_enabled', 'pdf_scanned_vl_enabled', 'pdf_scan_tile_overlap',
        'allow_external_vision_images', 'vision_max_image_bytes', 'vision_max_image_pixels',
        'vision_max_page_fraction', 'external_models_enabled', 'allow_unmasked_external',
        'openrouter_base_url', 'openrouter_chat_path', 'openrouter_embedding_path',
        'openrouter_vision_model', 'openrouter_vision_fallback_model', 'openrouter_model', 'guardrails_enabled',
        'openrouter_guardrail_model', 'guardrail_max_chars', 'guardrail_max_entities',
        'llm_temperature', 'chunk_max_chars', 'chunk_overlap', 'retention_years',
    )
    config = {key: getattr(cfg, key, None) for key in fields}
    # These parser flags are read directly from the environment, not Settings.
    parser_defaults = {
        'PDF_PARSER': 'pypdf', 'OCR_ENABLED': 'false', 'OCR_LANGUAGE': 'eng',
        'OCR_TIMEOUT_SECONDS': '60', 'DOCLING_ARTIFACTS_PATH': '',
        'PARSER_MAX_PAGES': '10000', 'PARSER_MAX_FILE_BYTES': '134217728',
        'PARSER_MAX_IMAGE_PIXELS': '40000000', 'PARSER_MAX_PDF_OBJECTS': '100',
        'PARSER_MAX_CELLS': '1000000', 'PARSER_MAX_CHUNKS': '100000',
        'PARSER_MAX_ARCHIVE_ENTRIES': '20000', 'PARSER_MAX_ARCHIVE_BYTES': '268435456',
        'PARSER_MAX_TEXT_CHARS': '20000000', 'PARSER_MAX_BLOCKS': '200000',
        'PARSER_MAX_ASSET_BYTES': '20971520', 'PARSER_MAX_TOTAL_ASSET_BYTES': '268435456',
        'PARSER_MAX_ASSETS': '10000', 'PARSER_MAX_XML_DEPTH': '128',
        'DOC_CONVERSION_TIMEOUT_SECONDS': '120',
    }
    transfers = []
    if cfg.search_backend == 'opensearch' and cfg.opensearch_vector_search and cfg.embedding_backend == 'openrouter':
        transfers.append('masked-text-embeddings')
    if cfg.pdf_vision_enabled:
        transfers.append('bounded-document-images-vl')
        if cfg.pdf_scanned_vl_enabled:
            transfers.append('scanned-page-content-vl-overlapping-tiles')
    if cfg.image_embeddings_enabled:
        transfers.append('bounded-document-image-embeddings')
    if getattr(cfg, 'guardrails_enabled', False) and transfers:
        transfers.append('masked-text-guardrail')
    return {'policy_version': 'maker-checker-generations-v1', 'config': config,
            'parser': {key: os.environ.get(key, default) for key, default in parser_defaults.items()},
            'transfer_kinds': transfers}


def document_binding(document) -> str:
    return digest_json({'id': document.id, 'folder_id': document.folder_id, 'sha256': document.sha256,
                        'version': document.version, 'title': document.title, 'staging_key': document.staging_key,
                        'latest_date': document.latest_date, 'metadata': document.metadata_json,
                        'source_maker_id': document.maker_id})


def entitled(db, employee_id, folder_id, roles):
    employee = db.scalar(select(Employee).where(Employee.id == employee_id).execution_options(populate_existing=True))
    if not employee or not employee.active or not folder_roles(db, employee_id, folder_id).intersection(roles):
        raise GovernanceError('An active employee with the required folder entitlement is required.', 403)
    return employee


def lock_document(db, document_id):
    # The no-op update obtains SQLite's writer lock before eligibility is read.
    db.execute(update(Document).where(Document.id == document_id).values(status=Document.status))
    document = db.scalar(select(Document).where(Document.id == document_id).with_for_update().execution_options(populate_existing=True))
    if not document:
        raise GovernanceError('Document not found.', 404)
    return document


def request_processing(db, document_id, maker_id, *, kind, idempotency_key, expected_folder=None):
    if kind not in {'initial', 'reprocess'} or not idempotency_key or len(idempotency_key) > 120:
        raise GovernanceError('A valid processing kind and idempotency key are required.', 422)
    document = lock_document(db, document_id)
    require_hybrid_ingestion(document.title)
    if expected_folder is not None and document.folder_id != expected_folder:
        raise GovernanceError('Document does not belong to the exact requested folder.', 403)
    entitled(db, maker_id, document.folder_id, MAKER_ROLES)
    entitled(db, document.maker_id, document.folder_id, MAKER_ROLES)
    scope = processing_scope()
    binding = document_binding(document)
    existing = db.scalar(select(ProcessingRequest).where(ProcessingRequest.document_id == document_id,
                                                        ProcessingRequest.idempotency_key == idempotency_key))
    if existing:
        if (existing.kind != kind or existing.maker_id != maker_id or existing.binding_sha256 != binding
                or existing.scope_sha256 != digest_json(scope)):
            raise GovernanceError('Idempotency key already binds a different source, maker or processing scope.')
        return existing
    active = db.scalar(select(ProcessingRequest.id).where(ProcessingRequest.document_id == document_id,
                                                         ProcessingRequest.status.in_(OPEN_REQUEST_STATES)))
    if active:
        raise GovernanceError('Document already has an open processing request; review or cancel it first.')
    if kind == 'initial' and document.status not in {'pending', 'error', 'rejected', 'approved'}:
        raise GovernanceError('Initial processing requires an unindexed document.')
    if kind == 'initial' and maker_id != document.maker_id:
        raise GovernanceError('Only the source maker may submit initial processing.', 403)
    if kind == 'reprocess' and document.status not in {'indexed', 'archived'}:
        raise GovernanceError('Reprocessing is limited to already indexed or archived documents.')
    request = ProcessingRequest(id=secrets.token_hex(16), document_id=document.id, folder_id=document.folder_id,
                                kind=kind, idempotency_key=idempotency_key, maker_id=maker_id, status='pending',
                                source_sha256=document.sha256, source_version=document.version,
                                binding_sha256=binding, scope_json=scope, scope_sha256=digest_json(scope))
    db.add(request)
    if kind == 'initial':
        document.status = 'pending'
        document.checker_id = None
        document.error = None  # Previous failure remains on its request history.
    audit(db, maker_id, 'processing.requested', document.id,
          {'request_id': request.id, 'kind': kind, 'folder_id': document.folder_id,
           'sha256': document.sha256, 'version': document.version, 'binding_sha256': binding,
           'scope_sha256': request.scope_sha256, 'scope': scope})
    return request


def assert_binding(request, document, *, check_runtime=True):
    if (request.folder_id != document.folder_id or request.source_sha256 != document.sha256
            or request.source_version != document.version or request.binding_sha256 != document_binding(document)):
        raise GovernanceError('Source or metadata changed; submit a fresh maker/checker request.')
    if request.scope_sha256 != digest_json(request.scope_json):
        raise GovernanceError('Stored processing scope failed integrity validation.')
    if check_runtime:
        scope = processing_scope()
        if request.scope_sha256 != digest_json(scope) or request.scope_json != scope:
            raise GovernanceError('Processing/transfer settings changed; submit a fresh maker/checker request.')


def review_request(db, request_id, checker_id, *, decision, scope_sha256, note='', control_evidence=None):
    request = db.get(ProcessingRequest, request_id)
    if not request:
        raise GovernanceError('Processing request not found.', 404)
    document = lock_document(db, request.document_id)
    db.refresh(request)
    entitled(db, checker_id, document.folder_id, CHECKER_ROLES)
    entitled(db, request.maker_id, document.folder_id, MAKER_ROLES)
    entitled(db, document.maker_id, document.folder_id, MAKER_ROLES)
    if checker_id in {request.maker_id, document.maker_id}:
        raise GovernanceError('Maker and checker must be different employees, including owners.', 403)
    if request.status != 'pending':
        raise GovernanceError('Only pending processing requests may be reviewed.')
    if decision not in {'approve', 'reject'}:
        raise GovernanceError('Decision must be approve or reject.', 422)
    if decision == 'reject' and not note.strip():
        raise GovernanceError('Rejecting a request requires a review note.', 422)
    if decision == 'approve':
        # Checkers explicitly review the captured target profile; it need not be
        # the serving profile. Workers still require an exact runtime match.
        assert_binding(request, document, check_runtime=False)
        if scope_sha256 != request.scope_sha256:
            raise GovernanceError('Confirm the exact displayed processing scope digest.', 422)
        evidence = control_evidence or {}
        if document.metadata_json.get('domain') == 'external':
            if not document.metadata_json.get('licence_reference'):
                raise GovernanceError('External content needs a licence reference.')
            if set(evidence) != EXTERNAL_CONTROLS or any(not isinstance(value, str) or not value.strip() or len(value) > 500 for value in evidence.values()):
                raise GovernanceError('External review requires explicit licence, legal, risk and data_protection evidence references.', 422)
            for control, reference in evidence.items():
                db.add(ControlAttestation(request_id=request.id, control=control, checker_id=checker_id,
                                          evidence_reference=reference.strip()))
        request.status = 'approved'
    else:
        request.status = 'rejected'
    request.checker_id = checker_id
    request.reviewed_at = now()
    request.note = note
    if request.kind == 'initial':
        document.status = 'approved' if decision == 'approve' else 'rejected'
        document.checker_id = checker_id
    audit(db, checker_id, 'processing.' + decision, document.id,
          {'request_id': request.id, 'kind': request.kind, 'note': note,
           'sha256': request.source_sha256, 'version': request.source_version,
           'binding_sha256': request.binding_sha256, 'scope_sha256': request.scope_sha256,
           'controls': sorted((control_evidence or {}).keys()), 'approval_channel': 'authenticated-http'})
    return request


def validate_approval(db, request, document, *, check_runtime=True):
    if request.status not in {'approved', 'processing', 'completed'} or not request.checker_id or not request.reviewed_at:
        raise GovernanceError('A separately authenticated checker approval is required.')
    assert_binding(request, document, check_runtime=check_runtime)
    entitled(db, request.maker_id, document.folder_id, MAKER_ROLES)
    entitled(db, document.maker_id, document.folder_id, MAKER_ROLES)
    entitled(db, request.checker_id, document.folder_id, CHECKER_ROLES)
    if request.checker_id in {request.maker_id, document.maker_id}:
        raise GovernanceError('Maker/checker separation no longer holds.', 403)
    if document.metadata_json.get('domain') == 'external':
        controls = list(db.scalars(select(ControlAttestation).where(ControlAttestation.request_id == request.id)))
        if ({item.control for item in controls} != EXTERNAL_CONTROLS
                or any(item.checker_id != request.checker_id or not item.evidence_reference.strip() for item in controls)):
            raise GovernanceError('Independent external-content control evidence is missing.')


def require_current_approval(db, document, request_id=None):
    """Gate reindex/model-transfer integration; legacy checker_id alone is insufficient."""
    query = select(ProcessingRequest).where(ProcessingRequest.document_id == document.id,
                                           ProcessingRequest.status.in_({'approved', 'processing', 'completed'}))
    if request_id:
        query = query.where(ProcessingRequest.id == request_id)
    request = db.scalar(query.order_by(ProcessingRequest.created_at.desc()).limit(1))
    if not request:
        raise GovernanceError('No current version/scope-bound checker approval exists.')
    validate_approval(db, request, document)
    return request


def request_json(request):
    return {key: getattr(request, key) for key in (
        'id', 'document_id', 'folder_id', 'kind', 'status', 'maker_id', 'checker_id',
        'source_sha256', 'source_version', 'binding_sha256', 'scope_sha256',
        'scope_json', 'created_at', 'reviewed_at', 'error')}
