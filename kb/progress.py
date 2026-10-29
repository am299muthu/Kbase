"""Content-free live ingestion progress, scoped to an active processing request."""
from kb.processing_context import _scope


def report_progress(stage=None, **details):
    scope = _scope.get()
    if scope is None:
        return
    from kb.telemetry import event
    event('ingestion.progress', request_id=scope[0], stage=stage, **details)
    from kb.db import ProcessingProgress, ProcessingRequest, db_session, now
    with db_session() as db:
        request = db.get(ProcessingRequest, scope[0])
        if request is None or request.status != 'processing':
            return
        row = db.get(ProcessingProgress, request.id)
        if row is None:
            row = ProcessingProgress(request_id=request.id, stage=stage or 'processing', details={})
            db.add(row)
        if stage and stage != row.stage:
            row.stage = stage
            row.details = {}
        allowed = {'completed', 'total', 'model', 'attempt', 'http_status'}
        if set(details) - allowed:
            raise ValueError('Unsupported progress metadata')
        row.details = {**row.details, **details}
        row.updated_at = now()
