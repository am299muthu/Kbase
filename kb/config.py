import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values, load_dotenv
from pydantic import AliasChoices, Field, field_validator, model_validator
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
    model_config = SettingsConfigDict(env_file=ROOT / '.env', extra='ignore', populate_by_name=True)
    app_name: str = 'ABC Knowledge'
    app_mode: Literal['demo', 'infrastructure'] = 'demo'
    host: str = '127.0.0.1'
    port: int = 8000
    data_dir: Path = ROOT / 'var'
    corpus_dir: Path = ROOT / 'KM_Data'
    database_url: str = 'sqlite:///var/kbase.db'
    # SQLite journal. WAL is fastest on a local disk but needs shared memory that network file systems
    # (a CML project disk) do not give reliably; use 'delete' there.
    sqlite_journal_mode: Literal['wal', 'delete', 'truncate', 'persist'] = 'wal'
    search_backend: Literal['sqlite', 'opensearch'] = 'opensearch'
    # Which engine SEARCH_BACKEND=opensearch talks to. The OPENSEARCH_* settings (URL, index, login)
    # keep their names for both, because approval scopes record them. Elasticsearch needs 8.0 or later.
    search_engine: Literal['opensearch', 'elasticsearch'] = 'opensearch'
    # Windows/offline demo: allow uploads and ingestion with SQLite search, hash embeddings and no
    # external models. Off by default; validated below so it can never weaken the hybrid profiles.
    local_ingestion_enabled: bool = False
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
    embedding_backend: Literal['hash', 'openrouter', 'local'] = 'hash'
    # EMBEDDING_BACKEND=local: a model from the local Hugging Face cache, run on this machine.
    local_embedding_model: str = 'BAAI/bge-small-en-v1.5'
    image_embeddings_enabled: bool = False
    image_embedding_dimensions: int = 0
    pdf_vision_enabled: bool = False
    pdf_scanned_vl_enabled: bool = True
    pdf_scan_tile_overlap: float = 0.08
    guardrails_enabled: bool = True
    openrouter_guardrail_model: str = Field('', validation_alias=AliasChoices(
        'GUARDRAIL_MODEL', 'OPENROUTER_GUARDRAIL_MODEL'))  # blank: the answer model
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
    # Model names. Each can be set by its plain name (ANSWER_MODEL, VISION_MODEL, ...) or by the
    # older OPENROUTER_* name; the attribute names stay, because approval scopes record them.
    openrouter_model: str = Field('nvidia/nemotron-3-ultra-550b-a55b:free',
                                  validation_alias=AliasChoices('ANSWER_MODEL', 'OPENROUTER_MODEL'))
    openrouter_vision_model: str = Field('google/gemma-4-31b-it:free',
                                         validation_alias=AliasChoices('VISION_MODEL', 'OPENROUTER_VISION_MODEL'))
    openrouter_vision_fallback_model: str = Field('', validation_alias=AliasChoices(
        'VISION_FALLBACK_MODEL', 'OPENROUTER_VISION_FALLBACK_MODEL'))
    openrouter_embedding_model: str = Field('nvidia/nemotron-3.5-embed-1b:free', validation_alias=AliasChoices(
        'EMBEDDING_MODEL', 'OPENROUTER_EMBEDDING_MODEL'))
    openrouter_image_embedding_model: str = Field('', validation_alias=AliasChoices(
        'IMAGE_EMBEDDING_MODEL', 'OPENROUTER_IMAGE_EMBEDDING_MODEL'))
    # One endpoint per API model. Each model KBase calls over an API (all but GLiNER, which runs in
    # the Docling Python) can have its own OpenAI-compatible endpoint: a base URL such as
    # https://models.example/granite/v1 (the full .../chat/completions or .../embeddings URL also
    # works) and its own key. A blank URL uses the shared OPENROUTER_BASE_URL gateway and paths; a
    # blank key uses OPENROUTER_API_KEY. The planner and the guardrail fall back to the answer model's
    # endpoint. URLs must use HTTPS.
    # MODEL_API=openai sends plain OpenAI-compatible requests, for an internal model server: no
    # OpenRouter price ceilings or routing fields, and models are tried in the order configured
    # (no ':free'-first ordering, no paid-model ceilings).
    model_api: Literal['openrouter', 'openai'] = 'openrouter'
    answer_model_url: str = ''           # answers (ANSWER_MODEL)
    answer_model_key: str = ''
    planner_model: str = ''              # splits a question into searches; blank: ANSWER_MODEL
    planner_model_url: str = ''
    planner_model_key: str = ''
    guardrail_model_url: str = ''        # grounding checks (GUARDRAIL_MODEL)
    guardrail_model_key: str = ''
    vision_model_url: str = ''           # charts and scanned regions (VISION_MODEL)
    vision_model_key: str = ''
    embedding_model_url: str = ''        # search vectors (EMBEDDING_MODEL)
    embedding_model_key: str = ''
    image_embedding_model_url: str = ''  # image vectors (IMAGE_EMBEDDING_MODEL)
    image_embedding_model_key: str = ''
    # Paid models are used only after every ':free' model in a chain failed, and only with these
    # ceilings (USD per million tokens). 0 disables paid models entirely (fail closed).
    openrouter_paid_max_price_prompt: float = 0.0
    openrouter_paid_max_price_completion: float = 0.0
    llm_temperature: float = 0
    i_retry: int = 1
    e_retry: int = 1
    model_timeout_seconds: int = 90

    @field_validator('embedding_backend', 'llm_provider', mode='before')
    @classmethod
    def plain_api_name(cls, value):
        # 'api' is the plain name for the model-API adapter, whatever serves it; the code keeps
        # calling it 'openrouter', so approval scopes and profiles stay as they are.
        return 'openrouter' if isinstance(value, str) and value.strip().lower() == 'api' else value

    @model_validator(mode='after')
    def validate_mode(self):
        for name in ('answer', 'planner', 'guardrail', 'vision', 'embedding', 'image_embedding'):
            url = getattr(self, f'{name}_model_url')
            if url and not url.startswith('https://'):
                raise ValueError(f'{name.upper()}_MODEL_URL must use HTTPS.')
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
        # Two local setups, both with nothing leaving the machine: the offline demo (SQLite keyword
        # search, hash vectors) and the opt-in local hybrid profile (OpenSearch BM25 + vectors from a
        # local embedding model).
        offline = self.search_backend == 'sqlite' and self.embedding_backend == 'hash'
        local_hybrid = (self.search_backend == 'opensearch' and self.opensearch_vector_search
                        and self.embedding_backend == 'local')
        if self.local_ingestion_enabled and (self.app_mode != 'demo' or self.storage_backend != 'local'
                or self.external_models_enabled or not (offline or local_hybrid)):
            raise ValueError('Local ingestion is only for the offline demo: demo mode, local storage, no external models, and either '
                             'SQLite search with hash embeddings or OpenSearch hybrid search with local embeddings.')
        if self.embedding_backend == 'local' and not self.local_ingestion_enabled:
            raise ValueError('Local embeddings are only for the local demo profiles (LOCAL_INGESTION_ENABLED=true).')
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
        if self.openrouter_paid_max_price_prompt < 0 or self.openrouter_paid_max_price_completion < 0:
            raise ValueError('Paid model price ceilings must not be negative.')
        if ',' in self.openrouter_embedding_model:
            raise ValueError('OPENROUTER_EMBEDDING_MODEL must be one model: vectors from different models cannot share a field.')
        if self.image_embeddings_enabled and ',' in self.openrouter_image_embedding_model:
            raise ValueError('OPENROUTER_IMAGE_EMBEDDING_MODEL must be one image-capable model when image embeddings are enabled: vectors from different models cannot share a field.')
        if self.vision_max_image_bytes <= 0 or self.vision_max_image_pixels <= 0:
            raise ValueError('Vision image limits must be positive')
        if not 0 < self.vision_max_page_fraction <= 1:
            raise ValueError('Vision page coverage limit must be in (0, 1]; 1 disables the cap so whole-page objects may be sent')
        return self


@lru_cache
def settings() -> Settings:
    return Settings()


def worker_python_flags() -> list[str]:
    """How the parsing and GLiNER worker scripts are started (KB_WORKER_PYTHON_FLAG). -I (isolated, the
    default) also ignores user-installed packages, which is where pip installs them in a CML session;
    -P keeps those but still never imports from the script's folder or the working folder."""
    flag = os.environ.get('KB_WORKER_PYTHON_FLAG', '-I').strip()
    if flag not in {'-I', '-P'}:
        raise ValueError('KB_WORKER_PYTHON_FLAG must be -I or -P')
    return [flag]


def model_chain(*values: str) -> list[str]:
    """Ordered model IDs from comma-separated settings: ':free' models first, then paid.

    Paid models are dropped unless explicit price ceilings are configured, so a paid model can
    never be reached by accident. Order inside each group follows the configuration order.
    """
    cfg = settings()
    seen, free, paid = set(), [], []
    for value in values:
        for item in str(value or '').split(','):
            model = item.strip().strip('\'"')
            if not model or model in seen:
                continue
            seen.add(model)
            (free if model.endswith(':free') else paid).append(model)
    if getattr(cfg, 'model_api', 'openrouter') == 'openai':
        # An internal model server has no free or paid tiers: keep the configured order.
        return [m for m in dict.fromkeys(item.strip().strip('\'"') for value in values
                                         for item in str(value or '').split(',')) if m]
    paid_allowed = cfg.openrouter_paid_max_price_prompt > 0 and cfg.openrouter_paid_max_price_completion > 0
    return free + (paid if paid_allowed else [])
