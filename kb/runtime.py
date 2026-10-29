"""Read-only hybrid startup gate. Configuration flags alone do not prove readiness."""
import math

from sqlalchemy import select

from kb.config import settings
from kb.db import Chunk, Document, db_session
from kb.governance import require_current_approval
from kb.search import _validate_vectors, client, index_for_document, retention_cutoff, verified_payload
from kb.storage import ObjectStore


def check_hybrid_readiness() -> dict:
    cfg = settings()
    blocking = []
    if cfg.search_backend != 'opensearch' or not cfg.opensearch_vector_search:
        blocking.append('OpenSearch hybrid retrieval is not enabled')
    if cfg.embedding_backend != 'openrouter' or cfg.llm_provider != 'openrouter':
        blocking.append('Configured text embedding/inference adapters must use OpenRouter')
    if not cfg.external_models_enabled or not cfg.openrouter_api_key or not cfg.guardrails_enabled:
        blocking.append('Approved external processing, private key and guardrails are required')
    if cfg.storage_backend != 's3':
        blocking.append('Configure S3-compatible file storage; SQLite controls may remain')
    if blocking:
        return {'ready': False, 'index': cfg.opensearch_index, 'blocking': blocking, 'verified_chunks': 0}
    count = 0
    try:
        ObjectStore().ensure_bucket()
        search = client()
        mapping = search.indices.get_mapping(index=cfg.opensearch_index)[cfg.opensearch_index]['mappings']
        _validate_vectors(mapping)
        if search.count(index=cfg.opensearch_index)['count'] == 0:
            return {'ready': False, 'index': cfg.opensearch_index,
                    'blocking': ['Target index is empty; approved corpus enrichment/vector population is incomplete'],
                    'verified_chunks': 0}
        with db_session() as db:
            documents = list(db.scalars(select(Document).where(Document.status == 'indexed',
                                                               Document.latest_date >= retention_cutoff())))
            if not documents:
                raise ValueError('No indexed corpus exists')
            for document in documents:
                document_index = index_for_document(db, document)
                needs_vectors = document_index == cfg.opensearch_index
                if needs_vectors:
                    require_current_approval(db, document)
                records = list(db.scalars(select(Chunk).where(Chunk.document_id == document.id)))
                if not records:
                    raise ValueError('An indexed document has no current chunks')
                for start in range(0, len(records), 64):
                    batch = records[start:start + 64]
                    rows = search.mget(index=document_index, body={'ids': [record.id for record in batch]})['docs']
                    if len(rows) != len(batch):
                        raise ValueError('Incomplete target index')
                    for record, row in zip(batch, rows):
                        source = row.get('_source', {})
                        if (not row.get('found') or row['_id'] != record.id
                                or verified_payload(record, source) is None
                                or (needs_vectors and source.get('embedding_model') != cfg.openrouter_embedding_model)):
                            raise ValueError('Missing or inconsistent corpus chunk')
                        if not needs_vectors:
                            continue
                        vectors = [(source.get('vector'), cfg.embedding_dimensions)]
                        if cfg.image_embeddings_enabled and record.payload.get('image_asset'):
                            if source.get('image_embedding_model') != cfg.openrouter_image_embedding_model:
                                raise ValueError('Missing image-model metadata')
                            vectors.append((source.get('image_vector'), cfg.image_embedding_dimensions))
                        for vector, dimension in vectors:
                            if (not isinstance(vector, list) or len(vector) != dimension or not any(vector)
                                    or any(type(value) not in (int, float) or not math.isfinite(value) for value in vector)):
                                raise ValueError('Missing or incompatible corpus vectors')
                        count += 1
        if count == 0:
            raise ValueError('No current hybrid chunks were verified')
    except Exception as exc:
        # Never leak driver errors, document text, credentials or raw API bodies.
        blocking.append('Target corpus/storage/approval verification failed (' + type(exc).__name__ + ')')
    return {'ready': not blocking, 'index': cfg.opensearch_index, 'blocking': blocking, 'verified_chunks': count}
