"""One hour of wall time per ingestion attempt, measured from its durable claim."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from kb.db import Audit, ProcessingProgress, ProcessingRequest, audit, db_session
from kb.governance import lock_document

LIMIT = timedelta(hours=1)


def latest_claim(db, request):
    return db.scalar(select(Audit.created_at).where(
        Audit.target_id == request.document_id, Audit.action == 'ingestion.claimed',
        Audit.details['request_id'].as_string() == request.id
    ).order_by(Audit.id.desc()).limit(1))


def remaining_seconds(started):
    return (datetime.fromisoformat(started) + LIMIT - datetime.now(timezone.utc)).total_seconds()


def fail_timeout(db, request, document):
    progress = db.get(ProcessingProgress, request.id)
    stage = progress.stage if progress else 'processing'
    request.status = 'failed'
    request.error = f'[processing_timeout; stage={stage}] Processing exceeded the one-hour limit.'
    if request.kind == 'initial':
        document.status = 'error'
        document.error = request.error
    audit(db, 'pipeline', 'ingestion.failed', document.id, {
        'request_id': request.id, 'stage': stage, 'error_code': 'processing_timeout',
        'error_type': 'TimeoutError', 'previous_generation_preserved': request.kind == 'reprocess'})


def expire_overdue(exclude=None):
    with db_session() as db:
        overdue = [(r.id, r.document_id) for r in db.scalars(select(ProcessingRequest).where(
            ProcessingRequest.status == 'processing', ProcessingRequest.id != exclude))
            if (started := latest_claim(db, r)) and remaining_seconds(started) <= 0]
    count = 0
    for request_id, document_id in overdue:
        with db_session() as db:
            document = lock_document(db, document_id)
            request = db.get(ProcessingRequest, request_id)
            if request.status == 'processing':
                started = latest_claim(db, request)
                if started and remaining_seconds(started) <= 0:
                    fail_timeout(db, request, document)
                    count += 1
    return count
