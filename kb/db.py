import hashlib
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from kb.config import settings


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Base(DeclarativeBase):
    pass


class Employee(Base):
    __tablename__ = 'employees'
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    business_unit: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(Text)
    # Legacy database column retained for existing SQLite schemas; unused by application policy.
    pii_unmasked: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Folder(Base):
    __tablename__ = 'folders'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    path: Mapped[str] = mapped_column(String(700), unique=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey('folders.id'), nullable=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey('employees.id'))
    business_unit: Mapped[str] = mapped_column(String(100))
    division: Mapped[str] = mapped_column(String(100))
    department: Mapped[str] = mapped_column(String(100))
    region: Mapped[str] = mapped_column(String(80), default='Singapore')
    organisation: Mapped[str] = mapped_column(String(100), default='ABC Bank')


class Grant(Base):
    __tablename__ = 'grants'
    __table_args__ = (UniqueConstraint('folder_id', 'employee_id', 'role'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    folder_id: Mapped[str] = mapped_column(ForeignKey('folders.id'), index=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey('employees.id'), index=True)
    role: Mapped[str] = mapped_column(String(20))


class Document(Base):
    __tablename__ = 'documents'
    __table_args__ = (UniqueConstraint('folder_id', 'title', 'version'),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    folder_id: Mapped[str] = mapped_column(ForeignKey('folders.id'), index=True)
    status: Mapped[str] = mapped_column(String(30), default='pending', index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    latest_date: Mapped[str] = mapped_column(String(10))
    maker_id: Mapped[str] = mapped_column(ForeignKey('employees.id'))
    checker_id: Mapped[str | None] = mapped_column(ForeignKey('employees.id'), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    staging_key: Mapped[str] = mapped_column(Text)
    raw_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256: Mapped[str] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ProcessingRequest(Base):
    """Version-bound human approval and durable processing queue (not maker metadata)."""
    __tablename__ = 'processing_requests'
    __table_args__ = (UniqueConstraint('document_id', 'idempotency_key'),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey('documents.id'), index=True)
    folder_id: Mapped[str] = mapped_column(ForeignKey('folders.id'), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    idempotency_key: Mapped[str] = mapped_column(String(120))
    maker_id: Mapped[str] = mapped_column(ForeignKey('employees.id'))
    checker_id: Mapped[str | None] = mapped_column(ForeignKey('employees.id'), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default='pending', index=True)
    source_sha256: Mapped[str] = mapped_column(String(64))
    source_version: Mapped[int] = mapped_column(Integer)
    binding_sha256: Mapped[str] = mapped_column(String(64))
    scope_json: Mapped[dict] = mapped_column(JSON)
    scope_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    reviewed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    note: Mapped[str] = mapped_column(Text, default='')
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ProcessingProgress(Base):
    """Latest live step; separate table keeps existing requests/schema compatible."""
    __tablename__ = 'processing_progress'
    request_id: Mapped[str] = mapped_column(ForeignKey('processing_requests.id'), primary_key=True)
    stage: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)


class ReplacementCleanup(Base):
    """Durable deletion work, committed before any external object is removed."""
    __tablename__ = 'replacement_cleanups'
    document_id: Mapped[str] = mapped_column(ForeignKey('documents.id'), primary_key=True)
    replacement_id: Mapped[str] = mapped_column(ForeignKey('documents.id'), index=True)
    status: Mapped[str] = mapped_column(String(20), default='pending')
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ControlAttestation(Base):
    """Explicit checker evidence; maker-supplied approval booleans have no authority."""
    __tablename__ = 'control_attestations'
    __table_args__ = (UniqueConstraint('request_id', 'control'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(ForeignKey('processing_requests.id'), index=True)
    control: Mapped[str] = mapped_column(String(40))
    checker_id: Mapped[str] = mapped_column(ForeignKey('employees.id'))
    evidence_reference: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class IndexedGeneration(Base):
    """Atomic SQL pointer to the only generation authorised for retrieval/reindex."""
    __tablename__ = 'indexed_generations'
    document_id: Mapped[str] = mapped_column(ForeignKey('documents.id'), primary_key=True)
    request_id: Mapped[str] = mapped_column(ForeignKey('processing_requests.id'))
    curated_key: Mapped[str] = mapped_column(Text)
    raw_parsed_key: Mapped[str] = mapped_column(Text)
    committed_at: Mapped[str] = mapped_column(String(40), default=now)


class Chunk(Base):
    __tablename__ = 'chunks'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey('documents.id'), index=True)
    folder_id: Mapped[str] = mapped_column(ForeignKey('folders.id'), index=True)
    text: Mapped[str] = mapped_column(Text)
    section: Mapped[str] = mapped_column(Text)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON)


class LoginSession(Base):
    __tablename__ = 'sessions'
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey('employees.id'))
    expires_at: Mapped[str] = mapped_column(String(40))


class ProcessingSetting(Base):
    """A processing time limit saved on the settings page; see kb.processing_limits."""
    __tablename__ = 'processing_settings'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[int] = mapped_column(Integer)
    updated_by: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[str] = mapped_column(String(40), default=now)


class GraphBuild(Base):
    """One run of the graph builder over a folder's chosen documents; see kb.graph_build."""
    __tablename__ = 'graph_builds'
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    folder_id: Mapped[str] = mapped_column(ForeignKey('folders.id'), index=True)
    status: Mapped[str] = mapped_column(String(20), default='queued')  # queued, running, done, failed
    stage: Mapped[str] = mapped_column(String(80), default='')
    started_by: Mapped[str] = mapped_column(String(64))
    document_ids: Mapped[list] = mapped_column(JSON, default=list)
    backend: Mapped[str] = mapped_column(String(40), default='')
    ontology_version: Mapped[str] = mapped_column(String(20), default='')
    counts: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    finished_at: Mapped[str] = mapped_column(String(40), default='')


class GraphNode(Base):
    """An entity in a folder's graph, typed against the ontology. Visible only to that folder."""
    __tablename__ = 'graph_nodes'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    folder_id: Mapped[str] = mapped_column(ForeignKey('folders.id'), index=True)
    key: Mapped[str] = mapped_column(String(200), index=True)  # normalised label, stable across builds
    label: Mapped[str] = mapped_column(Text)
    node_type: Mapped[str] = mapped_column(String(40))
    type_source: Mapped[str] = mapped_column(String(10), default='model')  # model, rule, human
    document_ids: Mapped[list] = mapped_column(JSON, default=list)
    build_id: Mapped[str] = mapped_column(String(32))


class GraphLink(Base):
    """A typed link between two nodes. Proposed until a checker approves it; only approved links are used in answers."""
    __tablename__ = 'graph_links'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    folder_id: Mapped[str] = mapped_column(ForeignKey('folders.id'), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey('graph_nodes.id'), index=True)
    target_id: Mapped[str] = mapped_column(ForeignKey('graph_nodes.id'), index=True)
    predicate: Mapped[str] = mapped_column(String(40))
    relation: Mapped[str] = mapped_column(String(60), default='')  # graphify's own relation name
    confidence: Mapped[str] = mapped_column(String(12), default='')  # EXTRACTED, INFERRED, AMBIGUOUS
    score: Mapped[float] = mapped_column(default=0.0)
    status: Mapped[str] = mapped_column(String(12), default='proposed')  # proposed, approved, rejected
    reviewed_by: Mapped[str] = mapped_column(String(64), default='')
    reviewed_at: Mapped[str] = mapped_column(String(40), default='')
    review_note: Mapped[str] = mapped_column(Text, default='')
    build_id: Mapped[str] = mapped_column(String(32))


class GraphEvidence(Base):
    """Where a link was found: the indexed chunk (and so the page) and the sentence that supports it."""
    __tablename__ = 'graph_evidence'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    link_id: Mapped[str] = mapped_column(ForeignKey('graph_links.id'), index=True)
    document_id: Mapped[str] = mapped_column(String(64))
    chunk_id: Mapped[str] = mapped_column(String(80), default='')
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quote: Mapped[str] = mapped_column(Text, default='')


class EntityScan(Base):
    """One run of the entity scan over a folder's indexed documents; see kb.entities."""
    __tablename__ = 'entity_scans'
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    folder_id: Mapped[str] = mapped_column(ForeignKey('folders.id'), index=True)
    status: Mapped[str] = mapped_column(String(20), default='queued')  # queued, running, done, failed
    stage: Mapped[str] = mapped_column(String(120), default='')
    started_by: Mapped[str] = mapped_column(String(64))
    document_ids: Mapped[list] = mapped_column(JSON, default=list)
    model: Mapped[str] = mapped_column(String(80), default='')
    ontology_version: Mapped[str] = mapped_column(String(20), default='')
    counts: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    finished_at: Mapped[str] = mapped_column(String(40), default='')


class EntityMention(Base):
    """An entity found in one chunk (a chunk tag), with the classifier's check of its type."""
    __tablename__ = 'entity_mentions'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    folder_id: Mapped[str] = mapped_column(ForeignKey('folders.id'), index=True)
    document_id: Mapped[str] = mapped_column(String(64), index=True)
    chunk_id: Mapped[str] = mapped_column(String(80), index=True)
    key: Mapped[str] = mapped_column(String(200), index=True)  # normalised label, as graph nodes use
    label: Mapped[str] = mapped_column(Text)
    entity_type: Mapped[str] = mapped_column(String(40))
    confidence: Mapped[float] = mapped_column(default=0.0)       # the extractor's span confidence
    type_check: Mapped[str] = mapped_column(String(12), default='')  # confirmed, disputed
    agreement: Mapped[float] = mapped_column(default=0.0)        # classifier probability of entity_type
    hint: Mapped[str] = mapped_column(String(40), default='')     # the classifier's own pick
    scan_id: Mapped[str] = mapped_column(String(32))


class DocumentEntity(Base):
    """One of the few entities a document is mainly about, ranked, with its document-fit flag."""
    __tablename__ = 'document_entities'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    folder_id: Mapped[str] = mapped_column(ForeignKey('folders.id'), index=True)
    document_id: Mapped[str] = mapped_column(String(64), index=True)
    key: Mapped[str] = mapped_column(String(200), index=True)
    label: Mapped[str] = mapped_column(Text)
    entity_type: Mapped[str] = mapped_column(String(40))
    rank: Mapped[int] = mapped_column(Integer)
    chunks: Mapped[int] = mapped_column(Integer)   # chunks of the document that mention it
    score: Mapped[float] = mapped_column(default=0.0)
    agreement: Mapped[float] = mapped_column(default=0.0)
    fit: Mapped[str] = mapped_column(String(12), default='unchecked')  # expected, unexpected, unchecked
    scan_id: Mapped[str] = mapped_column(String(32))


class DocumentProfile(Base):
    """The document kind the classifier chose, and the scan's counts for one document."""
    __tablename__ = 'document_profiles'
    document_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    folder_id: Mapped[str] = mapped_column(ForeignKey('folders.id'), index=True)
    kind: Mapped[str] = mapped_column(String(40), default='')
    kind_confidence: Mapped[float] = mapped_column(default=0.0)
    kind_scores: Mapped[dict] = mapped_column(JSON, default=dict)
    entities: Mapped[int] = mapped_column(Integer, default=0)
    confirmed: Mapped[int] = mapped_column(Integer, default=0)
    disputed: Mapped[int] = mapped_column(Integer, default=0)
    unexpected: Mapped[int] = mapped_column(Integer, default=0)
    scan_id: Mapped[str] = mapped_column(String(32))
    scanned_at: Mapped[str] = mapped_column(String(40), default=now)


class Audit(Base):
    __tablename__ = 'audit_events'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    actor_id: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(100))
    target_id: Mapped[str] = mapped_column(String(100))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    previous_hash: Mapped[str] = mapped_column(String(64), default='')
    event_hash: Mapped[str] = mapped_column(String(64), default='')


cfg = settings()
cfg.data_dir.mkdir(parents=True, exist_ok=True)
engine = create_engine(cfg.database_url, connect_args={'check_same_thread': False, 'timeout': 30} if cfg.database_url.startswith('sqlite') else {}, pool_pre_ping=True)
if cfg.database_url.startswith('sqlite'):
    @event.listens_for(engine, 'connect')
    def sqlite_settings(connection, _):
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute(f'PRAGMA journal_mode={cfg.sqlite_journal_mode.upper()}')  # SQLITE_JOURNAL_MODE

Session = sessionmaker(engine, expire_on_commit=False)


def init_db():
    Base.metadata.create_all(engine)


@contextmanager
def db_session():
    with Session() as db:
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise


def audit(db, actor_id: str, action: str, target_id: str, details: dict | None = None):
    # Local append-only evidence. Production must forward to immutable/WORM storage.
    import json
    from sqlalchemy import select
    if engine.dialect.name == 'postgresql':
        from sqlalchemy import text
        db.execute(text('SELECT pg_advisory_xact_lock(4819201)'))
    else:
        # Acquire SQLite's transaction-wide writer lock before reading the chain head.
        db.connection().exec_driver_sql('UPDATE audit_events SET event_hash=event_hash WHERE id=-1')
    previous = db.scalar(select(Audit).order_by(Audit.id.desc()).limit(1))
    item = Audit(actor_id=actor_id, action=action, target_id=target_id, details=details or {}, created_at=now(), previous_hash=previous.event_hash if previous else '')
    data = [item.created_at, item.actor_id, item.action, item.target_id, item.details, item.previous_hash]
    item.event_hash = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    db.add(item)
    db.flush()
