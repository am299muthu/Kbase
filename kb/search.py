from kb.telemetry import traced, operation, event, failure
import hashlib
import math
import re
import tempfile
from pathlib import Path
from datetime import date

from sqlalchemy import select

from kb.config import settings
from kb.progress import report_progress
from kb.db import Chunk, Document, Folder, IndexedGeneration, ProcessingRequest
from kb.providers import embeddings, image_embeddings, image_query_embedding, validate_visual_region
from kb.security import permitted_folders


def retention_cutoff() -> str:
    today = date.today()
    try:
        return today.replace(year=today.year - settings().retention_years).isoformat()
    except ValueError:  # February 29 in a non-leap target year.
        return today.replace(year=today.year - settings().retention_years, day=28).isoformat()


def client():
    from opensearchpy import OpenSearch
    cfg = settings()
    return OpenSearch(hosts=[cfg.opensearch_url], http_auth=(cfg.opensearch_username, cfg.opensearch_password) if cfg.opensearch_username else None,
                      verify_certs=cfg.opensearch_verify_certs, ca_certs=cfg.opensearch_ca_certs or None,
                      timeout=30, max_retries=1, retry_on_timeout=True)


def index_for_document(db, document) -> str:
    """Keep later BM25-only uploads readable beside the approved hybrid batch."""
    cfg = settings()
    if not cfg.opensearch_bm25_fallback_index:
        return cfg.opensearch_index
    generation = db.get(IndexedGeneration, document.id)
    request = db.get(ProcessingRequest, generation.request_id) if generation else None
    if request:
        index = request.scope_json.get('config', {}).get('opensearch_index')
        if index in {cfg.opensearch_index, cfg.opensearch_bm25_fallback_index}:
            return index
        raise ValueError('Published generation uses an index outside the configured retrieval scope.')
    return cfg.opensearch_bm25_fallback_index


def index_definition() -> dict:
    definition = {'settings': {'index': {'number_of_shards': 1, 'number_of_replicas': 0}},
        'mappings': {'dynamic': False, 'properties': {
            'text': {'type': 'text'}, 'title': {'type': 'text'}, 'section': {'type': 'text'},
            'document_id': {'type': 'keyword'}, 'folder_id': {'type': 'keyword'}, 'generation_id': {'type': 'keyword'}, 'latest_date': {'type': 'date'},
            'embedding_model': {'type': 'keyword'}, 'image_embedding_model': {'type': 'keyword'}}}}
    if settings().opensearch_vector_search:
        definition['settings']['index']['knn'] = True
        definition['mappings']['properties']['vector'] = {
            'type': 'knn_vector', 'dimension': settings().embedding_dimensions,
            'method': {'name': 'hnsw', 'engine': 'lucene', 'space_type': 'cosinesimil'}}
        definition['mappings']['_meta'] = {'text_embedding_model': _text_model()}
        if settings().image_embeddings_enabled:
            definition['mappings']['properties']['image_vector'] = {
                'type': 'knn_vector', 'dimension': settings().image_embedding_dimensions,
                'method': {'name': 'hnsw', 'engine': 'lucene', 'space_type': 'cosinesimil'}}
            definition['mappings']['_meta']['image_embedding_model'] = settings().openrouter_image_embedding_model
    return definition


def _text_model() -> str:
    cfg = settings()
    return cfg.openrouter_embedding_model if cfg.embedding_backend == 'openrouter' else 'offline-hash-v1'


def _validate_vectors(mapping: dict):
    cfg = settings()
    properties = mapping.get('properties', {})
    if properties.get('generation_id', {}).get('type') != 'keyword':
        raise RuntimeError('Vector index must support published generation filtering; create a fresh compatible index.')
    expected = [('vector', cfg.embedding_dimensions, 'text_embedding_model', _text_model())]
    if cfg.image_embeddings_enabled:
        expected.append(('image_vector', cfg.image_embedding_dimensions, 'image_embedding_model', cfg.openrouter_image_embedding_model))
    for field, dimensions, metadata_key, model in expected:
        vector = properties.get(field, {})
        if (vector.get('type') != 'knn_vector' or vector.get('dimension') != dimensions
                or vector.get('method', {}).get('engine') != 'lucene'
                or vector.get('method', {}).get('name') != 'hnsw'
                or vector.get('method', {}).get('space_type') != 'cosinesimil'
                or mapping.get('_meta', {}).get(metadata_key) != model):
            raise RuntimeError('Vector mapping/model is incompatible; create a fresh index for the configured text/image models.')


def ensure_index():
    if settings().search_backend != 'opensearch':
        return
    search = client()
    index = settings().opensearch_index
    if not search.indices.exists(index=index):
        search.indices.create(index=index, body=index_definition())
    else:
        mapping = search.indices.get_mapping(index=index)[index]['mappings']
        if settings().opensearch_vector_search:
            _validate_vectors(mapping)


def _visual_vector(chunk: dict, cache: dict) -> list[float] | None:
    cfg = settings()
    asset = chunk.get('image_asset')
    if not cfg.image_embeddings_enabled or not asset:
        return None
    from kb.storage import ObjectStore
    validate_visual_region(asset)
    digest = asset.get('sha256', '')
    if not re.fullmatch(r'[0-9a-f]{64}', digest) or asset.get('object_key') != f'assets/{chunk["document_id"]}/{digest}.png':
        raise ValueError('Image embedding asset does not match document/object lineage')
    if digest not in cache:
        with tempfile.TemporaryDirectory(prefix='image-embedding-', dir=cfg.data_dir) as directory:
            path = Path(directory) / 'object.png'
            ObjectStore().fetch(asset['object_key'], path)
            if path.stat().st_size > cfg.vision_max_image_bytes:
                raise ValueError('Image embedding asset exceeds the transfer limit')
            data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('Image embedding asset failed integrity validation')
        cache[digest] = image_embeddings([data])[0]
    return cache[digest]


@traced('ingestion.index')
def index_chunks(chunks: list[dict]):
    if settings().search_backend != 'opensearch':
        raise ValueError('Hybrid indexing requires OpenSearch.')
    from opensearchpy.helpers import bulk
    cfg = settings()
    if not cfg.opensearch_vector_search or cfg.embedding_backend != 'openrouter' or not cfg.external_models_enabled:
        raise ValueError('Hybrid indexing with real embeddings is mandatory; BM25-only index writes are disabled.')
    ensure_index()
    visual_cache = {}
    for start in range(0, len(chunks), 32):
        batch = chunks[start:start + 32]
        if cfg.opensearch_vector_search:
            vectors = embeddings([chunk['text'] for chunk in batch])
            actions = []
            for chunk, vector in zip(batch, vectors):
                source = {**chunk, 'vector': vector, 'embedding_model': _text_model()}
                visual_vector = _visual_vector(chunk, visual_cache)
                if visual_vector is not None:
                    source.update(image_vector=visual_vector, image_embedding_model=cfg.openrouter_image_embedding_model)
                actions.append({'_index': cfg.opensearch_index, '_id': chunk['id'], '_source': source})
        else:
            actions = [{'_index': cfg.opensearch_index, '_id': chunk['id'], '_source': chunk} for chunk in batch]
        report_progress('indexing', completed=start, total=len(chunks))
        bulk(client(), actions, raise_on_error=True)
        report_progress('indexing', completed=start + len(batch), total=len(chunks))
    client().indices.refresh(index=cfg.opensearch_index)


def remove_document(document_id: str):
    if settings().search_backend == 'opensearch':
        client().delete_by_query(index=settings().opensearch_index, body={'query': {'term': {'document_id': document_id}}}, refresh=True)


def chunk_manifest(payload: dict) -> dict:
    """Keep SQL lineage/integrity metadata, not a second searchable content copy."""
    if settings().search_backend == 'sqlite':
        return payload
    return {key: value for key, value in payload.items() if key != 'text'} | {
        'text_sha256': hashlib.sha256(payload['text'].encode()).hexdigest()}


def verified_payload(record: Chunk, indexed: dict) -> dict | None:
    if indexed.get('document_id') != record.document_id or indexed.get('folder_id') != record.folder_id or not isinstance(indexed.get('text'), str):
        return None
    expected = record.payload.get('text_sha256') or hashlib.sha256(record.text.encode()).hexdigest()
    if hashlib.sha256(indexed['text'].encode()).hexdigest() != expected:
        return None
    return {**record.payload, 'text': indexed['text']}


@traced('retrieval.hybrid')
def search_documents(db, employee_id: str, query: str, top_k: int = 5) -> list[dict]:
    top_k = max(1, min(top_k, 12))
    folders = permitted_folders(db, employee_id)
    if not folders:
        return []
    # Current SQL grants drive prefilters; candidates are rechecked below.
    # Generation terms suit this prototype; large deployments need a scalable
    # publication/read model rather than an unbounded terms filter.
    if settings().search_backend == 'opensearch':
        cfg = settings()
        filters = [{'terms': {'folder_id': folders}}, {'range': {'latest_date': {'gte': retention_cutoff()}}}]
        generations = list(db.scalars(select(IndexedGeneration.request_id).join(Document,
            Document.id == IndexedGeneration.document_id).where(Document.folder_id.in_(folders),
            Document.status == 'indexed', Document.latest_date >= retention_cutoff())))
        # Exclude staged/obsolete generations before rank fusion; legacy chunks
        # without a generation retain their existing SQL integrity recheck.
        published = [{'bool': {'must_not': [{'exists': {'field': 'generation_id'}}]}}]
        if generations:
            published.append({'terms': {'generation_id': generations}})
        filters.append({'bool': {'should': published, 'minimum_should_match': 1}})
        lexical = {'size': top_k * 4, '_source': {'excludes': ['vector', 'image_vector']}, 'query': {'bool': {'filter': filters,
                    'must': [{'multi_match': {'query': query, 'fields': ['text', 'title^2', 'section^1.5']}}]}}}
        lexical_indexes = ','.join(filter(None, [cfg.opensearch_index, cfg.opensearch_bm25_fallback_index]))
        queries = [(lexical_indexes, lexical)]
        if cfg.opensearch_vector_search:
            _validate_vectors(client().indices.get_mapping(index=cfg.opensearch_index)[cfg.opensearch_index]['mappings'])
            vector = embeddings([query])[0]
            text_filters = filters + [{'term': {'embedding_model': _text_model()}}]
            queries.append((cfg.opensearch_index, {'size': top_k * 4, '_source': {'excludes': ['vector', 'image_vector']}, 'query': {'knn': {'vector': {
                        'vector': vector, 'k': top_k * 4, 'filter': {'bool': {'filter': text_filters}}}}}}))
            if cfg.image_embeddings_enabled:
                visual_query = image_query_embedding(query)
                image_filters = filters + [{'term': {'image_embedding_model': cfg.openrouter_image_embedding_model}}]
                queries.append((cfg.opensearch_index, {'size': top_k * 4, '_source': {'excludes': ['vector', 'image_vector']}, 'query': {'knn': {'image_vector': {
                            'vector': visual_query, 'k': top_k * 4, 'filter': {'bool': {'filter': image_filters}}}}}}))
        ranked = {}
        for index, body in queries:
            mode = 'knn' if 'knn' in body['query'] else 'bm25'
            with operation('opensearch.' + mode, retrieval_mode=mode):
                hits = client().search(index=index, body=body)['hits']['hits']
                event('retrieval.results', retrieval_mode=mode, result_count=len(hits))
            for rank, hit in enumerate(hits, 1):
                chunk_id = hit['_id']
                entry = ranked.setdefault(chunk_id, {'score': 0.0, 'payload': {**hit['_source'], 'id': chunk_id}})
                entry['score'] += 1 / (60 + rank)
        candidates = [item['payload'] for item in sorted(ranked.values(), key=lambda x: x['score'], reverse=True)]
    else:
        # Explicit small-corpus demo lexical retrieval; not advertised as semantic or scalable.
        terms = set(re.findall(r'\w+', query.lower())) - {'the', 'is', 'a', 'an', 'what', 'how', 'are', 'my', 'can', 'of', 'and', 'to', 'for', 'in'}
        ranked = []
        eligible = select(Chunk).join(Document, Document.id == Chunk.document_id).where(Chunk.folder_id.in_(folders), Document.status == 'indexed', Document.latest_date >= retention_cutoff())
        for chunk in db.scalars(eligible):
            words = re.findall(r'\w+', chunk.text.lower())
            matches = sum(1 + math.log1p(words.count(term)) for term in terms if term in words)
            if matches:
                ranked.append((matches / (1 + len(words) / 500), chunk.payload))
        candidates = [payload for _, payload in sorted(ranked, key=lambda item: item[0], reverse=True)]
    results = []
    for chunk in candidates:
        document = db.get(Document, chunk.get('document_id'))
        # Second entitlement/status check defeats stale/orphaned index entries and index-side spoofing.
        record = db.get(Chunk, chunk.get('id'))
        if (document and document.folder_id in folders and document.status == 'indexed' and document.latest_date >= retention_cutoff()
                and record and record.document_id == document.id and record.folder_id == document.folder_id):
            payload = verified_payload(record, chunk) if settings().search_backend == 'opensearch' else record.payload
            if payload is not None:
                results.append({**payload, 'title': document.title, 'latest_date': document.latest_date})
        if len(results) == top_k:
            break
    return results


@traced('retrieval.section')
def get_section(db, employee_id: str, chunk_id: str) -> dict | None:
    chunk = db.get(Chunk, chunk_id)
    if not chunk or chunk.folder_id not in permitted_folders(db, employee_id):
        return None
    doc = db.get(Document, chunk.document_id)
    if not doc or doc.status != 'indexed' or doc.latest_date < retention_cutoff():
        return None
    if settings().search_backend == 'opensearch':
        from opensearchpy import NotFoundError
        try:
            indexed = client().get(index=index_for_document(db, doc), id=chunk_id, _source_excludes='vector,image_vector')['_source']
        except NotFoundError:
            return None
        payload = verified_payload(chunk, indexed)
        return {**payload, 'title': doc.title} if payload is not None else None
    return {**chunk.payload, 'title': doc.title}
