"""Exact-ID, idempotent reprocessing requests. Never walks the source corpus."""
from sqlalchemy import select

from kb.db import Document, ProcessingRequest, audit, db_session
from kb.governance import GovernanceError, MAKER_ROLES, entitled, lock_document, request_json, request_processing


def request_reprocessing(folder_id: str, document_ids: list[str], maker_id: str, idempotency_key: str) -> list[dict]:
    if not folder_id or not document_ids or len(document_ids) > 100 or len(set(document_ids)) != len(document_ids):
        raise GovernanceError('Supply an exact folder and 1–100 distinct existing document IDs.', 422)
    # All-or-nothing selection validation and request creation; no file/object reads.
    with db_session() as db:
        documents = list(db.scalars(select(Document).where(Document.id.in_(document_ids))))
        if len(documents) != len(document_ids) or any(document.folder_id != folder_id for document in documents):
            raise GovernanceError('Every exact document ID must exist in the requested folder.', 422)
        requests = [request_processing(db, document_id, maker_id, kind='reprocess',
                                       idempotency_key=idempotency_key, expected_folder=folder_id)
                    for document_id in sorted(document_ids)]
        return [request_json(request) for request in requests]


def cancel_request(db, request_id, maker_id):
    request = db.get(ProcessingRequest, request_id)
    if not request:
        raise GovernanceError('Processing request not found.', 404)
    document = lock_document(db, request.document_id)
    db.refresh(request)
    entitled(db, maker_id, document.folder_id, MAKER_ROLES)
    if maker_id != request.maker_id:
        raise GovernanceError('Only the requesting maker may cancel this request.', 403)
    if request.status not in {'pending', 'approved'}:
        raise GovernanceError('Only pending/approved work can be cancelled; stop workers before recovering interrupted work.')
    request.status = 'cancelled'
    if request.kind == 'initial':
        document.status = 'pending'
        document.checker_id = None
    audit(db, maker_id, 'processing.cancelled', document.id, {'request_id': request.id})
    return request


def recover_requests(folder_id: str, document_ids: list[str]) -> int:
    """Operator recovery is bounded and fails work, never resurrects approval."""
    if not folder_id or not document_ids or len(document_ids) > 100 or len(set(document_ids)) != len(document_ids):
        raise GovernanceError('Recovery requires an exact folder and distinct document IDs.', 422)
    with db_session() as db:
        documents = [lock_document(db, document_id) for document_id in sorted(document_ids)]
        if any(document.folder_id != folder_id for document in documents):
            raise GovernanceError('Recovery selection contains a different folder.', 422)
        count = 0
        for document in documents:
            for request in db.scalars(select(ProcessingRequest).where(ProcessingRequest.document_id == document.id,
                                                                       ProcessingRequest.status == 'processing')):
                request.status = 'failed'
                request.error = 'Interrupted work recovered; a new request and checker approval are required.'
                if request.kind == 'initial':
                    document.status = 'error'
                    document.error = request.error
                audit(db, 'operator', 'processing.recovered', document.id, {'request_id': request.id})
                count += 1
        return count
