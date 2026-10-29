import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values, load_dotenv
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
# Parser/converter libraries use environment variables too; honour the same .env.
load_dotenv(ROOT / '.env', override=False)
# Named profiles contain routing/gates only; credentials stay in private .env.
# Selecting a profile is explicit; never infer hybrid readiness from installed code.
_profile = os.environ.get('KB_RUNTIME_PROFILE', '').strip()
if _profile:
    if _profile not in {'local', 'hybrid', 'pdf-hybrid'}:
        raise ValueError('KB_RUNTIME_PROFILE must be local, hybrid or pdf-hybrid')
    _profile_values = dotenv_values(ROOT / 'profiles' / (_profile + '.env'), interpolate=False)
    _allowed = {'LLM_PROVIDER', 'EXTERNAL_MODELS_ENABLED', 'EMBEDDING_BACKEND',
        'OPENSEARCH_INDEX', 'OPENSEARCH_VECTOR_SEARCH', 'PDF_VISION_ENABLED',
        'ALLOW_EXTERNAL_VISION_IMAGES', 'IMAGE_EMBEDDINGS_ENABLED', 'STORAGE_BACKEND',
        'ALLOW_UNMASKED_EXTERNAL', 'GUARDRAILS_ENABLED',
        'OPENSEARCH_BM25_FALLBACK_INDEX', 'OPENSEARCH_HYBRID_FOLDERS'}
    if not _profile_values or set(_profile_values) - _allowed or any(value is None for value in _profile_values.values()):
        raise ValueError('Runtime profile is missing or contains unsupported settings')
    os.environ.update(_profile_values)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / '.env', extra='ignore')
    app_name: str = 'ABC Knowledge'
    app_mode: Literal['demo', 'infrastructure'] = 'demo'
    host: str = '127.0.0.1'
    port: int = 8000
    data_dir: Path = ROOT / 'var'
    corpus_dir: Path = ROOT / 'KM_Data'
    database_url: str = 'sqlite:///var/kbase.db'
    search_backend: Literal['sqlite', 'opensearch'] = 'opensearch'
    storage_backend: Literal['local', 's3'] = 'local'
    opensearch_url: str = 'http://localhost:9200'
    opensearch_index: str = 'abc-kb-curated-v1'
    opensearch_username: str = ''
    opensearch_password: str = ''
    opensearch_verify_certs: bool = True
    opensearch_ca_certs: str = ''
    opensearch_vector_search: bool = False
    opensearch_bm25_fallback_index: str = ''
    # Retained in captured approval scopes for compatibility with the initial batch.
    # New ingestion is hybrid in every entitled folder; this is not an ACL/filter.
    opensearch_hybrid_folders: list[str] = []
    s3_endpoint_url: str = ''
    s3_bucket: str = 'abc-knowledge'
    aws_access_key_id: str = ''
    aws_secret_access_key: str = ''
    aws_session_token: str = ''
    aws_region: str = 'ap-southeast-1'
    s3_addressing_style: Literal['auto', 'path', 'virtual'] = 'auto'
    s3_allow_insecure_http: bool = False
    s3_ca_bundle: str = ''
    s3_connect_timeout_seconds: float = 5.0
    s3_read_timeout_seconds: float = 60.0
    s3_max_attempts: int = 3
    s3_write_mode: Literal['conditional', 'serialized-local'] = 'conditional'
    demo_password: str = 'local-demo-change-me'
    session_ttl_hours: int = 8
    cookie_secure: bool = False
    max_upload_mb: int = 30
    retention_years: int = 7
    chunk_max_chars: int = 1800
    chunk_overlap: int = 200
    retrieval_k: int = 5
    embedding_dimensions: int = 384
    embedding_backend: Literal['hash', 'openrouter'] = 'hash'
    image_embeddings_enabled: bool = False
    image_embedding_dimensions: int = 0
    pdf_vision_enabled: bool = False
    pdf_scanned_vl_enabled: bool = True
    pdf_scan_tile_overlap: float = 0.08
    guardrails_enabled: bool = True
    openrouter_guardrail_model: str = ''
    guardrail_max_chars: int = 16000
    guardrail_max_entities: int = 500
    allow_external_vision_images: bool = False
    vision_max_image_bytes: int = 5242880
    vision_max_image_pixels: int = 4000000
    vision_max_page_fraction: float = 0.8
    llm_provider: Literal['extractive', 'openrouter'] = 'extractive'
    external_models_enabled: bool = False
    allow_unmasked_external: bool = False
    openrouter_api_key: str = ''
    openrouter_base_url: str = 'https://openrouter.ai'
    openrouter_chat_path: str = '/api/v1/chat/completions'
    openrouter_embedding_path: str = '/api/v1/embeddings'
    openrouter_model: str = 'nvidia/nemotron-3-ultra-550b-a55b:free'
    openrouter_vision_model: str = 'google/gemma-4-31b-it:free'
    openrouter_vision_fallback_model: str = ''
    openrouter_embedding_model: str = 'nvidia/nemotron-3.5-embed-1b:free'
    openrouter_image_embedding_model: str = ''
    llm_temperature: float = 0
    i_retry: int = 1
    e_retry: int = 1
    model_timeout_seconds: int = 90

    @model_validator(mode='after')
    def validate_mode(self):
        if bool(self.opensearch_bm25_fallback_index) != bool(self.opensearch_hybrid_folders):
            raise ValueError('Scoped hybrid retrieval requires both folders and a preserved BM25 index.')
        if self.opensearch_bm25_fallback_index:
            if (not self.opensearch_vector_search or self.opensearch_bm25_fallback_index == self.opensearch_index
                    or any(c in self.opensearch_bm25_fallback_index for c in '*?,/')):
                raise ValueError('Scoped hybrid retrieval requires a distinct exact BM25 index.')
        self.data_dir = (ROOT / self.data_dir).resolve()
        self.corpus_dir = (ROOT / self.corpus_dir).resolve()
        if self.database_url.startswith('sqlite:///'):
            path = self.database_url.removeprefix('sqlite:///')
            self.database_url = 'sqlite:///' + str((ROOT / path).resolve())
        if self.app_mode == 'infrastructure':
            if (self.search_backend != 'opensearch' or self.storage_backend != 's3'
                    or not self.database_url.startswith(('postgresql', 'sqlite:///'))):
                raise ValueError('Infrastructure mode requires OpenSearch, S3 and SQLite or PostgreSQL controls; no silent fallbacks.')
        if not 0 <= self.pdf_scan_tile_overlap <= 0.2:
            raise ValueError('Scanned-region overlap must be between 0 and 0.2')
        if not 1000 <= self.guardrail_max_chars <= 100000 or not 1 <= self.guardrail_max_entities <= 2000:
            raise ValueError('Guardrail request limits are invalid')
        if self.external_models_enabled and not self.guardrails_enabled:
            raise ValueError('External processing requires inference guardrails.')
        if self.s3_connect_timeout_seconds <= 0 or self.s3_read_timeout_seconds <= 0 or not 1 <= self.s3_max_attempts <= 10:
            raise ValueError('S3 timeout/retry limits are invalid')
        if self.embedding_dimensions < 8 or not 0 <= self.chunk_overlap < self.chunk_max_chars:
            raise ValueError('Invalid embedding dimensions or chunk overlap')
        if self.image_embeddings_enabled and (not self.openrouter_image_embedding_model or self.image_embedding_dimensions < 8
                or self.embedding_backend != 'openrouter' or not self.opensearch_vector_search):
            raise ValueError('Image embeddings require their own model/dimensions and OpenRouter vector search.')
        if self.vision_max_image_bytes <= 0 or self.vision_max_image_pixels <= 0:
            raise ValueError('Vision image limits must be positive')
        if not 0 < self.vision_max_page_fraction < 1:
            raise ValueError('Vision page coverage limit must be between 0 and 1, excluding whole pages')
        return self


@lru_cache
def settings() -> Settings:
    return Settings()
