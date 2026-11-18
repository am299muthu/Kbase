import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kb.db import Audit, Base, Document, ProcessingRequest, audit
from kb.processing_timeout import expire_overdue


class ProcessingTimeoutTests(unittest.TestCase):
    def test_latest_claim_controls_timeout_and_retry(self):
        engine = create_engine('sqlite://')
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)

        @contextmanager
        def session():
            with Session(engine) as db, db.begin():
                yield db

        with session() as db:
            for key, kind, status, age in [
                ('old', 'initial', 'processing', 61), ('indexed', 'reprocess', 'processing', 61),
                ('retry', 'initial', 'processing', 1), ('queued', 'initial', 'approved', 61)
            ]:
                db.add(Document(id=key, title=key + '.pdf', folder_id='folder', status=(
                    'indexed' if kind == 'reprocess' else status), version=1, latest_date='2026-11-01',
                    maker_id='maker', staging_key=key, sha256=key.ljust(64, 'a'), metadata_json={}))
                db.add(ProcessingRequest(id=key, document_id=key, folder_id='folder', kind=kind,
                    idempotency_key=key, maker_id='maker', status=status, source_sha256='a'*64,
                    source_version=1, binding_sha256='b'*64, scope_json={}, scope_sha256='c'*64))
                for minutes in ([180, age] if key == 'retry' else [age]):
                    timestamp = datetime.now(timezone.utc) - timedelta(minutes=minutes)
                    with patch('kb.db.now', return_value=timestamp.isoformat()):
                        audit(db, 'pipeline', 'ingestion.claimed', key, {'request_id': key})
        with patch('kb.processing_timeout.db_session', session):
            self.assertEqual(expire_overdue(), 2)
            self.assertEqual(expire_overdue(), 0)
        with session() as db:
            self.assertEqual(db.get(Document, 'old').status, 'error')
            self.assertEqual(db.get(Document, 'indexed').status, 'indexed')
            self.assertEqual(db.get(ProcessingRequest, 'retry').status, 'processing')
            self.assertEqual(db.get(ProcessingRequest, 'queued').status, 'approved')
            failures = list(db.scalars(select(Audit).where(Audit.action == 'ingestion.failed')))
            self.assertEqual({f.target_id for f in failures}, {'old', 'indexed'})
            self.assertTrue(all(f.details['error_code'] == 'processing_timeout' for f in failures))
