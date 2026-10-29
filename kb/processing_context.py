"""Recheck document approval before every outbound ingestion/reindex request."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

_scope = ContextVar('kb_processing_transfer_scope', default=None)


@contextmanager
def approved_transfer_scope(request_id: str, *, require_running=False):
    token = _scope.set((request_id, require_running))
    try:
        yield
    finally:
        _scope.reset(token)


def bound_processing_transfers(function):
    @wraps(function)
    def invoke(request_id, *args, **kwargs):
        with approved_transfer_scope(request_id, require_running=True):
            return function(request_id, *args, **kwargs)
    return invoke


def check_transfer_approval():
    scope = _scope.get()
    if scope is None:
        return  # Interactive queries use their authenticated entitlement path.
    from kb.db import Document, ProcessingRequest, db_session
    from kb.governance import GovernanceError, validate_approval
    request_id, require_running = scope
    with db_session() as db:
        request = db.get(ProcessingRequest, request_id)
        if request is None or (require_running and request.status != 'processing'):
            raise GovernanceError('Processing approval is no longer active.')
        document = db.get(Document, request.document_id)
        if document is None:
            raise GovernanceError('Approved source is no longer available.')
        validate_approval(db, request, document)
