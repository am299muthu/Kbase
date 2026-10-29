"""Rebuild OpenSearch from approved curated objects, without reparsing or reapproving."""
import hashlib
import json
import tempfile
from pathlib import Path

from sqlalchemy import delete, select

from kb.config import settings
from kb.db import Chunk, Document, IndexedGeneration, audit, db_session
from kb.governance import require_current_approval
from kb.processing_context import approved_transfer_scope
from kb.search import chunk_manifest, client, index_chunks, retention_cutoff
from kb.storage import ObjectStore


def reindex_documents(folder_id: str | None = None, limit: int = 1000) -> dict:
    cfg = settings()
    if cfg.search_backend != 'opensearch':
        raise ValueError('Reindex requires SEARCH_BACKEND=opensearch; it never falls back to SQLite search.')
    query = select(Document.id).where(Document.status == 'indexed', Document.latest_date >= retention_cutoff()).order_by(Document.id)
    if folder_id:
        query = query.where(Document.folder_id == folder_id)
    with db_session() as db:
        ids = list(db.scalars(query.limit(limit)))
    counts = {'documents': 0, 'chunks': 0, 'errors': 0}
    store = ObjectStore()
    for document_id in ids:
        try:
            with db_session() as db:
                document = db.get(Document, document_id)
                approval_id = None
                if cfg.opensearch_vector_search and cfg.embedding_backend == 'openrouter':
                    approval_id = require_current_approval(db, document).id
                generation = db.get(IndexedGeneration, document_id)
                generation_id = generation.request_id if generation else None
                curated_key = generation.curated_key if generation else f'curated/{document_id}/chunks.json'
                info = {'title': document.title, 'folder_id': document.folder_id, 'sha256': document.sha256,
                        'latest_date': document.latest_date, 'metadata': document.metadata_json,
                        'version': document.version}
                manifests = {row.id: row.payload for row in db.scalars(select(Chunk).where(Chunk.document_id == document_id))}
            with tempfile.TemporaryDirectory(prefix='reindex-', dir=cfg.data_dir) as temp:
                source = Path(temp) / 'chunks.json'
                store.fetch(curated_key, source)
                payloads = json.loads(source.read_text())
            if not isinstance(payloads, list) or not payloads:
                raise ValueError('Curated chunks are missing')
            for payload in payloads:
                if (payload['document_id'] != document_id or payload['folder_id'] != info['folder_id']
                        or payload['source_sha256'] != info['sha256'] or not isinstance(payload['text'], str)):
                    raise ValueError('Curated lineage mismatch')
                payload.update(title=info['title'], latest_date=info['latest_date'], metadata=info['metadata'])
            if (len({payload['id'] for payload in payloads}) != len(payloads)
                    or set(manifests) != {payload['id'] for payload in payloads}):
                raise ValueError('Curated IDs differ from the published generation')
            for payload in payloads:
                expected = manifests[payload['id']].get('text_sha256')
                if expected and hashlib.sha256(payload['text'].encode()).hexdigest() != expected:
                    raise ValueError('Curated text differs from the published integrity manifest')
            if approval_id:
                with approved_transfer_scope(approval_id):
                    index_chunks(payloads)
            else:
                index_chunks(payloads)
            # Verify indexed text before removing its legacy SQL cache. Original curated objects remain intact.
            search = client()
            for start in range(0, len(payloads), 100):
                batch = payloads[start:start + 100]
                stored = search.mget(index=cfg.opensearch_index, body={'ids': [p['id'] for p in batch]}, _source_includes=['text'])['docs']
                for expected, actual in zip(batch, stored):
                    if not actual.get('found') or actual['_id'] != expected['id'] or hashlib.sha256(actual['_source']['text'].encode()).hexdigest() != hashlib.sha256(expected['text'].encode()).hexdigest():
                        raise ValueError('OpenSearch text verification failed')
            with db_session() as db:
                # Respect concurrent metadata/review updates; no permissions or approvals are modified.
                from sqlalchemy import update
                db.execute(update(Document).where(Document.id == document_id).values(status=Document.status))
                document = db.get(Document, document_id)
                current_generation = db.get(IndexedGeneration, document_id)
                if ((current_generation.request_id if current_generation else None) != generation_id
                        or document.status != 'indexed' or document.sha256 != info['sha256']
                        or document.latest_date != info['latest_date'] or document.metadata_json != info['metadata']
                        or document.folder_id != info['folder_id'] or document.title != info['title']
                        or document.version != info['version']):
                    raise ValueError('Document or generation changed during reindex; retry after ingestion settles')
                if cfg.opensearch_vector_search and cfg.embedding_backend == 'openrouter':
                    require_current_approval(db, document)
                db.execute(delete(Chunk).where(Chunk.document_id == document_id))
                for payload in payloads:
                    db.add(Chunk(id=payload['id'], document_id=document_id, folder_id=payload['folder_id'], text='',
                                 section=payload['section'], page=payload.get('page'), payload=chunk_manifest(payload)))
                audit(db, 'operator', 'search.reindexed', document_id, {'index': cfg.opensearch_index, 'chunks': len(payloads),
                      'retrieval': 'hybrid' if cfg.opensearch_vector_search else 'bm25', 'sql_content_cache': False})
            counts['documents'] += 1
            counts['chunks'] += len(payloads)
        except Exception as exc:
            counts['errors'] += 1
            # Never print source text or third-party error bodies.
            print(json.dumps({'document_id': document_id, 'error_type': type(exc).__name__}), flush=True)
    return counts
