from kb.telemetry import traced, operation, event, failure
import tempfile
from pathlib import Path

from sqlalchemy import delete, select

from kb.config import settings
from kb.db import Chunk, Document, Folder, IndexedGeneration, ProcessingRequest, ProcessingProgress, ReplacementCleanup, audit, db_session, now
from kb.governance import GovernanceError, digest_json, lock_document, processing_scope, validate_approval, require_hybrid_ingestion
from kb.parsers import MissingDependency, ParseError, ParserLimitError, chunk_document, parse_document
from kb.processing_context import bound_processing_transfers
from kb.providers import ModelUnavailable, start_diagnostics
from kb.progress import report_progress
from kb.search import chunk_manifest, index_chunks, retention_cutoff
from kb.storage import ObjectStore, file_digest
from kb.vision import enrich_pdf_visuals
from kb.reading_order import order_pdf_blocks, evidence_snapshot


def _failure_details(exc, stage):
    # Public diagnostics use fixed messages/codes, never raw third-party errors.
    if isinstance(exc, GovernanceError):
        code, reason = 'approval_invalid', 'Source, processing scope or current maker/checker entitlement changed.'
    elif isinstance(exc, ParserLimitError):
        if str(exc) == 'Scanned PDF exceeds PARSER_MAX_PDF_OBJECTS':
            code, reason = 'pdf_scan_budget_exceeded', 'Scan/raster page tiles exceed PARSER_MAX_PDF_OBJECTS. Review a bounded split and its VL/image-transfer scope; limits were not raised automatically.'
        else:
            code, reason = 'parser_limit_exceeded', 'A reviewed parser resource limit was exceeded; review limits or split the source.'
    elif isinstance(exc, MissingDependency):
        code, reason = 'parser_dependency_missing', 'A required local parser dependency or model artifact is unavailable.'
    elif isinstance(exc, ParseError):
        code, reason = 'parser_failed', 'Local PDF/document extraction failed; operator parser review is required.'
    elif isinstance(exc, ModelUnavailable) and stage == 'visual_enrichment' and not settings().pdf_vision_enabled:
        code, reason = 'vl_approval_required', 'Unverified scan/raster content requires VL review, which is disabled in the approved local profile. Review a separate VL/image-transfer scope; do not bypass the external-transfer gate.'
    elif isinstance(exc, ModelUnavailable):
        code, reason = 'model_unavailable', 'An approved model, transfer gate or validated model response is unavailable.'
    else:
        code, reason = 'processing_failed', 'Processing failed; operator review of the indicated stage is required.'
    return code, f'[{code}; stage={stage}; {type(exc).__name__}] {reason} Submit a fresh maker request after resolving the cause; independent checker review is required.'


def _check_running(request_id):
    with db_session() as db:
        request = db.get(ProcessingRequest, request_id)
        if not request or request.status != 'processing':
            raise GovernanceError('Processing request is no longer running.')
        document = db.get(Document, request.document_id)
        validate_approval(db, request, document)


@bound_processing_transfers
@traced('ingestion.process')
def process_request(request_id: str) -> bool:
    """Stage a new immutable generation, then atomically publish SQL manifests.

    OpenSearch writes use new IDs, so partial/failing batches cannot overwrite
    the generation still served by SQL. Uncommitted IDs are never retrievable.
    """
    from opentelemetry import trace
    trace.get_current_span().set_attribute('kb.request_id', request_id)
    event('ingestion.started', request_id=request_id)
    document_id = None
    claimed = False
    stage = 'approval'
    diagnostics = start_diagnostics()
    try:
        with db_session() as db:
            request = db.get(ProcessingRequest, request_id)
            if not request:
                return False
            document_id = request.document_id
            doc = lock_document(db, document_id)
            db.refresh(request)
            if request.status == 'completed':
                return True  # Idempotent repeat: no parsing, transfer or index writes.
            if request.status != 'approved':
                return False
            if db.scalar(select(ReplacementCleanup.document_id).where(
                    ReplacementCleanup.replacement_id == document_id, ReplacementCleanup.status != 'completed').limit(1)):
                return False
            # Mark claimed before validation so invalid legacy/changed approvals
            # are failed once rather than permanently spinning in the queue.
            claimed = True
            validate_approval(db, request, doc)
            require_hybrid_ingestion(doc.title)
            if request.kind == 'initial' and doc.status != 'approved':
                raise GovernanceError('Document is not eligible for initial processing.')
            if request.kind == 'reprocess' and doc.status not in {'indexed', 'archived'}:
                raise GovernanceError('Document is not eligible for reprocessing.')
            request.status = 'processing'
            request.error = None
            if request.kind == 'initial':
                doc.status = 'processing'
                doc.error = None
            info = {'id': doc.id, 'title': doc.title, 'folder_id': doc.folder_id,
                    'staging_key': doc.staging_key, 'sha256': doc.sha256, 'latest_date': doc.latest_date,
                    'metadata': doc.metadata_json, 'version': doc.version}
            audit(db, 'pipeline', 'ingestion.claimed', document_id,
                  {'request_id': request_id, 'kind': request.kind, 'scope_sha256': request.scope_sha256})
        cfg = settings()
        store = ObjectStore()
        with tempfile.TemporaryDirectory(prefix='parse-', dir=cfg.data_dir) as temp:
            work = Path(temp)
            source = work / ('source' + Path(info['title']).suffix.lower())
            stage = 'source_fetch'
            report_progress(stage)
            store.fetch(info['staging_key'], source, expected_sha256=info['sha256'])
            if file_digest(source) != info['sha256']:
                raise GovernanceError('Staged source failed the approved SHA-256 integrity check.')
            _check_running(request_id)
            raw_key = f"raw/{info['folder_id']}/{document_id}/{request_id}/original{source.suffix}"
            stage = 'raw_storage'
            report_progress(stage)
            store.put_file(raw_key, source)
            stage = 'parsing'
            report_progress(stage)
            parsed = parse_document(source, work / 'assets')
            _check_running(request_id)
            if source.suffix == '.pdf':
                parsed = order_pdf_blocks(parsed)
                objects = list(parsed.layout_objects)
                objects.extend({key: asset[key] for key in
                                ('object_id', 'object_kind', 'page', 'bbox', 'extractor') if key in asset}
                               for asset in parsed.assets if asset.get('bbox'))
                with db_session() as debug_db:
                    audit(debug_db, 'pipeline', 'ingestion.pdf_layout', document_id,
                          {'request_id': request_id, 'source_sha256': info['sha256'],
                           'objects': objects, 'coordinate_origin': 'top-left'})
            stage = 'asset_storage'
            report_progress(stage)
            assets = []
            for asset in parsed.assets:
                path = Path(asset['path'])
                if not path.resolve().is_relative_to(work.resolve()):
                    raise ValueError('Parser returned an invalid asset path')
                key = f'assets/{document_id}/{path.name}'
                assets.append({**asset, 'path': store.uri(key), 'object_key': key})
                store.put_file(key, path)
            # Persist local extraction before model calls so failures retain evidence.
            stage = 'extracted_storage'
            report_progress(stage)
            extracted_key = f'extracted/{document_id}/{request_id}/content.json'
            extracted_uri = store.put_json(extracted_key, evidence_snapshot(parsed, assets))
            if source.suffix == '.pdf':
                stage = 'visual_enrichment'
                report_progress(stage)
                parsed = enrich_pdf_visuals(parsed, work / 'assets')
                parsed = order_pdf_blocks(parsed)
            parsed_key = f'raw-parsed/{document_id}/{request_id}/content.json'
            stage = 'parsed_storage'
            report_progress(stage)
            parsed_uri = store.put_json(parsed_key, evidence_snapshot(parsed, assets))
            stage = 'chunking'
            report_progress(stage)
            pieces = chunk_document(parsed, max_chars=cfg.chunk_max_chars, overlap=cfg.chunk_overlap)
            if not pieces:
                raise ValueError('No indexable content; review parser dependencies and the source.')
            payloads = []
            stored_assets = {(original['path'], original.get('page'), original.get('section')): stored
                             for original, stored in zip(parsed.assets, assets)}
            curation = 'source-extraction+generated-visual-v1' if any(block.metadata.get('generated') for block in parsed.blocks) else 'lossless-extractive-v1'
            for index, piece in enumerate(pieces):
                piece_assets = [stored_assets[(asset['path'], asset.get('page'), asset.get('section'))]
                                for asset in piece.get('assets', [])]
                if piece.get('image_asset'):
                    original = piece['image_asset']
                    piece['image_asset'] = stored_assets[(original['path'], original.get('page'), original.get('section'))]
                payloads.append({**piece, 'id': f'{document_id}:{request_id}:{index:06d}', 'document_id': document_id,
                    'generation_id': request_id, 'folder_id': info['folder_id'], 'title': info['title'], 'version': info['version'],
                    'latest_date': info['latest_date'], 'metadata': info['metadata'], 'source_uri': store.uri(raw_key),
                    'raw_parsed_uri': parsed_uri, 'extracted_uri': extracted_uri,
                    'raw_index_reference': f'{parsed_uri}#/blocks',
                    'source_block_references': [f'{parsed_uri}#/blocks/{number}' for number in piece['source_block_indices']],
                    'assets': piece_assets, 'source_sha256': info['sha256'], 'parser': parsed.parser,
                    'curation_method': curation})
            curated_key = f'curated/{document_id}/{request_id}/chunks.json'
            stage = 'curated_storage'
            report_progress(stage)
            store.put_json(curated_key, payloads)
            archived = info['latest_date'] < retention_cutoff()
            _check_running(request_id)
            if not archived:
                stage = 'indexing'
                report_progress(stage)
                index_chunks(payloads)
            stage = 'publication'
            report_progress(stage)
            with db_session() as db:
                document = lock_document(db, document_id)
                request = db.get(ProcessingRequest, request_id)
                if request.status != 'processing':
                    raise GovernanceError('Processing request was interrupted before publication.')
                validate_approval(db, request, document)
                db.execute(delete(Chunk).where(Chunk.document_id == document_id))
                if not archived:
                    for payload in payloads:
                        db.add(Chunk(id=payload['id'], document_id=document_id, folder_id=info['folder_id'],
                                     text=payload['text'] if cfg.search_backend == 'sqlite' else '',
                                     section=payload['section'], page=payload.get('page'), payload=chunk_manifest(payload)))
                generation = db.get(IndexedGeneration, document_id)
                if not generation:
                    generation = IndexedGeneration(document_id=document_id, request_id=request_id,
                                                   curated_key=curated_key, raw_parsed_key=parsed_key)
                    db.add(generation)
                else:
                    generation.request_id = request_id
                    generation.curated_key = curated_key
                    generation.raw_parsed_key = parsed_key
                    generation.committed_at = now()
                document.status = 'archived' if archived else 'indexed'
                document.raw_key = raw_key
                document.error = None
                document.checker_id = request.checker_id
                request.status = 'completed'
                progress = db.get(ProcessingProgress, request_id)
                if progress:
                    progress.stage = 'completed'
                    progress.details = {'completed': len(payloads), 'total': len(payloads)}
                    progress.updated_at = now()
                audit(db, 'pipeline', 'ingestion.completed', document_id,
                      {'request_id': request_id, 'chunks': len(payloads), 'parser': parsed.parser,
                       'sha256': info['sha256'], 'curation': curation, 'archived': archived,
                       'curated_key': curated_key, 'scope_sha256': request.scope_sha256})
        event('ingestion.completed', request_id=request_id, document_id=document_id, completed=len(payloads))
        return True
    except Exception as exc:
        failure(exc, request_id=request_id, document_id=document_id, stage=stage)
        if document_id and claimed:
            # Keep the old indexed generation and its retrievability on failure.
            with db_session() as db:
                document = lock_document(db, document_id)
                request = db.get(ProcessingRequest, request_id)
                if request and request.status in {'approved', 'processing'}:
                    code, message = _failure_details(exc, stage)
                    request.status = 'failed'
                    request.error = message
                    if request.kind == 'initial':
                        document.status = 'error'
                        document.error = request.error
                    audit(db, 'pipeline', 'ingestion.failed', document_id,
                          {'request_id': request_id, 'error_type': type(exc).__name__,
                           'error_code': code, 'stage': stage, 'request_id': request_id,
                           'model_diagnostics': diagnostics, 'error_detail': str(exc)[:500],
                           'previous_generation_preserved': request.kind == 'reprocess'})
        return False


def process_document(document_id: str) -> bool:
    with db_session() as db:
        request_id = db.scalar(select(ProcessingRequest.id).where(ProcessingRequest.document_id == document_id,
                                                                 ProcessingRequest.status == 'approved',
                                                                 ProcessingRequest.scope_sha256 == digest_json(processing_scope())).limit(1))
    return process_request(request_id) if request_id else False


def process_next() -> bool:
    from kb.replacement import cleanup_replacement
    cleanup_replacement()
    if not settings().opensearch_vector_search or not settings().external_models_enabled:
        return False
    with db_session() as db:
        request_id = db.scalar(select(ProcessingRequest.id).where(ProcessingRequest.status == 'approved',
                               ProcessingRequest.document_id.not_in(select(ReplacementCleanup.replacement_id).where(ReplacementCleanup.status != 'completed')),
                               ProcessingRequest.scope_sha256 == digest_json(processing_scope()))
                               .order_by(ProcessingRequest.created_at).limit(1))
    if request_id:
        process_request(request_id)
        return True
    return False


def archive_expired() -> int:
    from kb.search import remove_document
    with db_session() as db:
        if settings().database_url.startswith('sqlite'):
            db.connection().exec_driver_sql("UPDATE documents SET status=status WHERE id=''")
        documents = list(db.scalars(select(Document).where(Document.status == 'indexed', Document.latest_date < retention_cutoff()).with_for_update()))
        for document in documents:
            if db.scalar(select(ProcessingRequest.id).where(ProcessingRequest.document_id == document.id,
                                                            ProcessingRequest.status.in_({'pending', 'approved', 'processing'}))):
                continue
            remove_document(document.id)
            db.execute(delete(Chunk).where(Chunk.document_id == document.id))
            document.status = 'archived'
            audit(db, 'retention', 'document.archived', document.id)
        return sum(document.status == 'archived' for document in documents)
