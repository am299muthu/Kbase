"""Confirmed same-folder replacements with resumable physical cleanup."""
import unicodedata

from fastapi import HTTPException
from sqlalchemy import delete, select

from kb.config import settings
from kb.db import Chunk, Document, IndexedGeneration, ProcessingRequest, ReplacementCleanup, audit, db_session
from kb.search import client
from kb.storage import ObjectStore


def filename_key(name):
    return unicodedata.normalize('NFC', name).casefold()


def replacement_candidates(db, folder_id, title, confirmed_ids, action='replace'):
    # Caller holds the folder write lock, including for SQLite.
    docs = list(db.scalars(select(Document).where(Document.folder_id == folder_id,
                                                Document.status == 'indexed').with_for_update()))
    matches = [doc for doc in docs if filename_key(doc.title) == filename_key(title)]
    ids = {doc.id for doc in matches}
    if ids != set(confirmed_ids) or (matches and not confirmed_ids):
        raise HTTPException(409, {'code': 'duplicate_filename',
            'message': 'A successfully indexed file with this name already exists in this folder. Replace it?',
            'documents': [{'id': doc.id, 'title': doc.title, 'version': doc.version, 'status': doc.status} for doc in matches]})
    if action == 'version':
        return []
    if ids and (any(doc.status == 'processing' for doc in matches) or db.scalar(
            select(ProcessingRequest.id).where(ProcessingRequest.document_id.in_(ids),
                                               ProcessingRequest.status == 'processing').limit(1))):
        raise HTTPException(409, 'An existing file is processing. Stop or finish that request before replacing it.')
    return matches


def schedule_replacement(db, documents, replacement, actor_id):
    for old in documents:
        for request in db.scalars(select(ProcessingRequest).where(
                ProcessingRequest.document_id == old.id, ProcessingRequest.status.in_({'pending', 'approved'}))):
            request.status = 'cancelled'
        old.status = 'superseded'
        db.add(ReplacementCleanup(document_id=old.id, replacement_id=replacement.id))
        audit(db, actor_id, 'document.replacement_confirmed', old.id, {'replacement_id': replacement.id})


def cleanup_replacement():
    with db_session() as db:
        job = db.scalar(select(ReplacementCleanup).where(ReplacementCleanup.status != 'completed').limit(1))
        if job is None:
            return False
        old = db.get(Document, job.document_id)
        document_id, folder_id = old.id, old.folder_id
        if old.status != 'superseded':
            raise RuntimeError('Replacement cleanup target is not superseded')
    try:
        cfg = settings()
        if cfg.search_backend == 'opensearch':
            search = client()
            for index in set(filter(None, [cfg.opensearch_index, cfg.opensearch_bm25_fallback_index])):
                result = search.delete_by_query(index=index, body={'query': {'term': {'document_id': document_id}}}, refresh=True)
                if result.get('failures') or result.get('timed_out') or result.get('version_conflicts'):
                    raise RuntimeError('Index cleanup incomplete')
        store = ObjectStore()
        for prefix in [f'staging/{folder_id}/{document_id}', f'raw/{folder_id}/{document_id}',
                       f'assets/{document_id}', f'extracted/{document_id}', f'raw-parsed/{document_id}', f'curated/{document_id}']:
            store.delete_prefix(prefix)
        with db_session() as db:
            db.execute(delete(Chunk).where(Chunk.document_id == document_id))
            db.execute(delete(IndexedGeneration).where(IndexedGeneration.document_id == document_id))
            job = db.get(ReplacementCleanup, document_id)
            job.status, job.error = 'completed', None
            audit(db, 'pipeline', 'document.replacement_cleaned', document_id, {'replacement_id': job.replacement_id})
    except Exception as exc:
        with db_session() as db:
            job = db.get(ReplacementCleanup, document_id)
            job.error = f'Cleanup will retry ({type(exc).__name__}).'
        return False
    return True
