import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kb.db import Base, Document, ProcessingRequest, ReplacementCleanup
from kb.replacement import replacement_candidates, schedule_replacement, cleanup_replacement
from kb.storage import ObjectStore


class ReplacementTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.old = self.document('old', 'Report.PDF', 'folder')
        self.new = self.document('new', 'report.pdf', 'folder', 'pending')
        self.db.add(self.old)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def document(self, id, title, folder, status='indexed'):
        return Document(id=id, title=title, folder_id=folder, status=status, version=1,
                        latest_date='2026-11-04', maker_id='maker', metadata_json={},
                        staging_key=f'staging/{folder}/{id}/original.pdf', sha256='a'*64)

    def request(self, status):
        row = ProcessingRequest(id='req', document_id='old', folder_id='folder', kind='initial',
            idempotency_key='test', maker_id='maker', status=status, source_sha256='a'*64,
            source_version=1, binding_sha256='b'*64, scope_json={}, scope_sha256='c'*64)
        self.db.add(row)
        self.db.commit()
        return row

    def test_duplicate_requires_confirmation_without_mutation(self):
        with self.assertRaises(HTTPException) as caught:
            replacement_candidates(self.db, 'folder', 'report.pdf', [])
        self.assertEqual(caught.exception.detail['code'], 'duplicate_filename')
        self.assertEqual(caught.exception.detail['documents'][0]['id'], 'old')
        self.assertEqual(self.old.status, 'indexed')

    def test_unindexed_documents_do_not_trigger_duplicate_prompt(self):
        for status in ['pending', 'approved', 'processing', 'error', 'rejected', 'cancelled', 'archived', 'superseded']:
            with self.subTest(status=status):
                self.old.status = status
                self.db.commit()
                self.assertEqual(replacement_candidates(self.db, 'folder', 'report.pdf', []), [])

    def test_cancelled_initial_request_does_not_trigger_duplicate_prompt(self):
        self.old.status = 'pending'
        self.request('cancelled')
        self.assertEqual(replacement_candidates(self.db, 'folder', 'Report.PDF', []), [])

    def test_other_folder_is_not_a_duplicate(self):
        self.assertEqual(replacement_candidates(self.db, 'other', 'Report.PDF', []), [])

    def test_stale_confirmation_rejected(self):
        self.db.add(self.document('another', 'report.pdf', 'folder', 'indexed'))
        self.db.commit()
        with self.assertRaises(HTTPException):
            replacement_candidates(self.db, 'folder', 'Report.PDF', ['old'])

    def test_processing_request_blocks_replacement(self):
        self.request('processing')
        with self.assertRaises(HTTPException) as caught:
            replacement_candidates(self.db, 'folder', 'Report.PDF', ['old'])
        self.assertIn('processing', str(caught.exception.detail))

    def schedule(self):
        self.db.add(self.new)
        with patch('kb.replacement.audit'):
            schedule_replacement(self.db, [self.old], self.new, 'maker')
        self.db.commit()

    def test_confirmation_cancels_queue_and_persists_cleanup(self):
        row = self.request('approved')
        self.schedule()
        self.assertEqual(row.status, 'cancelled')
        self.assertEqual(self.old.status, 'superseded')
        self.assertEqual(self.db.get(ReplacementCleanup, 'old').replacement_id, 'new')

    def test_keep_new_version_preserves_processing_previous_file(self):
        row = self.request('processing')
        self.assertEqual(replacement_candidates(self.db, 'folder', 'report.pdf', ['old'], 'version'), [])
        self.assertEqual(row.status, 'processing')
        self.assertEqual(self.old.status, 'indexed')
        self.assertIsNone(self.db.get(ReplacementCleanup, 'old'))

    def test_upload_keep_version_increments_and_does_not_schedule_cleanup(self):
        from io import BytesIO
        from fastapi import UploadFile
        from kb.app import upload_document
        from kb.db import Folder
        self.db.add(Folder(id='folder', name='Folder', path='folder', owner_id='maker',
            business_unit='test', division='test', department='test'))
        self.db.commit()
        upload = UploadFile(filename='report.pdf', file=BytesIO(b'synthetic test source'))
        with patch('kb.app.entitled'), patch('kb.governance.require_hybrid_ingestion'), patch('kb.app.request_processing'), patch('kb.app.audit'), patch('kb.app.document_json', side_effect=lambda db, doc, user: {'version': doc.version}), patch('kb.app.ObjectStore') as store:
            result = upload_document(file=upload, folder_id='folder', latest_date=None,
                metadata='{}', replace_ids='["old"]', duplicate_action='version',
                user=SimpleNamespace(id='maker'), db=self.db)
        self.assertEqual(result['document']['version'], 2)
        self.assertFalse(result['cleanup_pending'])
        self.assertEqual(self.old.status, 'indexed')
        self.assertIsNone(self.db.get(ReplacementCleanup, 'old'))
        store.return_value.put_file.assert_called_once()

    def test_maker_can_cancel_pending_but_not_another_makers_request(self):
        from kb.reprocess import cancel_request
        from kb.governance import GovernanceError
        row = self.request('pending')
        with patch('kb.reprocess.entitled'), patch('kb.reprocess.audit'):
            with self.assertRaises(GovernanceError):
                cancel_request(self.db, row.id, 'different-maker')
            self.assertEqual(row.status, 'pending')
            cancel_request(self.db, row.id, 'maker')
        self.assertEqual(row.status, 'cancelled')

    def test_maker_cannot_cancel_processing_request(self):
        from kb.reprocess import cancel_request
        from kb.governance import GovernanceError
        row = self.request('processing')
        with patch('kb.reprocess.entitled'), self.assertRaises(GovernanceError):
            cancel_request(self.db, row.id, 'maker')
        self.assertEqual(row.status, 'processing')

    @contextmanager
    def session(self):
        yield self.db
        self.db.commit()

    def test_partial_cleanup_retries_without_marking_complete(self):
        self.schedule()
        search, store = Mock(), Mock()
        search.delete_by_query.return_value = {'failures': []}
        store.delete_prefix.side_effect = RuntimeError('storage down')
        cfg = SimpleNamespace(search_backend='opensearch', opensearch_index='current', opensearch_bm25_fallback_index='legacy')
        with patch('kb.replacement.db_session', self.session), patch('kb.replacement.settings', return_value=cfg), patch('kb.replacement.client', return_value=search), patch('kb.replacement.ObjectStore', return_value=store), patch('kb.replacement.audit'):
            self.assertFalse(cleanup_replacement())
            self.assertEqual(self.db.get(ReplacementCleanup, 'old').status, 'pending')
            store.delete_prefix.side_effect = None
            self.assertTrue(cleanup_replacement())
        self.assertEqual(self.db.get(ReplacementCleanup, 'old').status, 'completed')
        self.assertEqual({call.kwargs['index'] for call in search.delete_by_query.call_args_list}, {'current', 'legacy'})
        self.assertEqual(store.delete_prefix.call_args_list[-1].args[0], 'curated/old')

    def test_local_cleanup_preserves_neighbor_namespace(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ObjectStore(SimpleNamespace(data_dir=temp, storage_backend='local'))
            for key in ['assets/old/a.txt', 'assets/old-other/a.txt']:
                path = store.local_path(key); path.parent.mkdir(parents=True); path.write_text('test')
            store.delete_prefix('assets/old')
            store.delete_prefix('assets/old')
            self.assertTrue(store.local_path('assets/old-other/a.txt').exists())
            with self.assertRaises(ValueError): store.delete_prefix('../outside')

    def test_s3_partial_delete_is_failure(self):
        store = ObjectStore.__new__(ObjectStore)
        store.backend = 's3'; store.cfg = SimpleNamespace(s3_bucket='test-bucket'); store.client = Mock()
        store.client.get_bucket_versioning.return_value = {}
        store.client.get_paginator.return_value.paginate.return_value = [{'Contents': [{'Key': 'assets/old/a'}]}]
        store.client.delete_objects.return_value = {'Errors': [{'Code': 'AccessDenied'}]}
        with self.assertRaises(RuntimeError): store.delete_prefix('assets/old')
        self.assertEqual(store.client.get_paginator.return_value.paginate.call_args.kwargs['Prefix'], 'assets/old/')


if __name__ == '__main__':
    unittest.main()
