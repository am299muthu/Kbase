import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from fastapi import HTTPException
from kb.app import _checker_pdf_request
from kb.db import Document, ProcessingRequest
from kb.governance import GovernanceError, document_binding, digest_json

class CheckerPdfTests(unittest.TestCase):
    def setUp(self):
        self.doc=SimpleNamespace(id='doc',folder_id='folder',sha256='a'*64,version=1,
            title='review.pdf',staging_key='staged/review.pdf',latest_date='2026-10-30',metadata_json={},maker_id='E003')
        self.request=SimpleNamespace(id='req',document_id='doc',folder_id='folder',status='pending',
            maker_id='E003',source_sha256=self.doc.sha256,source_version=1,
            binding_sha256=document_binding(self.doc),scope_json={},scope_sha256=digest_json({}))
        self.user=SimpleNamespace(id='E002',pii_unmasked=False)
        self.db=Mock()
        self.db.get.side_effect=lambda cls,key:self.request if cls is ProcessingRequest else self.doc
    def test_checker_without_unmasked_permission_can_review(self):
        with patch('kb.app.entitled') as allowed:
            req,doc=_checker_pdf_request(self.db,self.user,'req')
            self.assertIs(req,self.request)
            self.assertIn('checker',allowed.call_args.args[-1])
    def test_maker_cannot_use_checker_preview(self):
        self.user.id='E003'
        with patch('kb.app.entitled'),self.assertRaises(HTTPException) as error:
            _checker_pdf_request(self.db,self.user,'req')
        self.assertEqual(error.exception.status_code,403)
    def test_no_folder_role_denied(self):
        with patch('kb.app.entitled',side_effect=GovernanceError('denied',403)),self.assertRaises(GovernanceError):
            _checker_pdf_request(self.db,self.user,'req')
    def test_review_ends_when_request_is_approved(self):
        self.request.status='approved'
        with patch('kb.app.entitled'),self.assertRaises(HTTPException) as error:
            _checker_pdf_request(self.db,self.user,'req')
        self.assertEqual(error.exception.status_code,409)
    def test_changed_source_rejected(self):
        self.doc.sha256='b'*64
        with patch('kb.app.entitled'),self.assertRaises(GovernanceError):
            _checker_pdf_request(self.db,self.user,'req')

    def test_staged_source_renders_and_checks_digest(self):
        from kb.app import _checker_pdf_preview
        from pypdf import PdfWriter
        def fetch(key, destination, *, expected_sha256):
            self.assertEqual(key, self.doc.staging_key)
            self.assertEqual(expected_sha256, self.request.source_sha256)
            writer=PdfWriter();writer.add_blank_page(width=612,height=792);writer.add_blank_page(width=792,height=612)
            writer.write(destination)
        with patch('kb.app.entitled'), patch('kb.app.audit') as audit, patch('kb.app.ObjectStore') as store:
            store.return_value.fetch.side_effect=fetch
            info=_checker_pdf_preview('req',self.user,self.db)
            self.assertEqual(info['page_count'],2)
            self.assertEqual(info['page_sizes'], [[612.0,792.0],[792.0,612.0]])
            png=_checker_pdf_preview('req',self.user,self.db,1)
            self.assertTrue(png.startswith(b'\x89PNG'))
            self.assertEqual(audit.call_count,2)
            with self.assertRaises(HTTPException) as error:
                _checker_pdf_preview('req',self.user,self.db,3)
            self.assertEqual(error.exception.status_code,404)

    def test_concurrent_page_requests_render_without_crashing(self):
        from concurrent.futures import ThreadPoolExecutor
        from io import BytesIO
        from kb.app import _checker_pdf_preview
        from pypdf import PdfWriter
        writer=PdfWriter()
        for width,height in [(612,792),(792,612),(612,792)]:
            writer.add_blank_page(width=width,height=height)
        source=BytesIO();writer.write(source)
        def fetch(key,destination,**kwargs):
            destination.write_bytes(source.getvalue())
        with patch('kb.app.entitled'),patch('kb.app.audit'),patch('kb.app.ObjectStore') as store:
            store.return_value.fetch.side_effect=fetch
            with ThreadPoolExecutor(max_workers=6) as pool:
                images=list(pool.map(lambda page: _checker_pdf_preview('req',self.user,self.db,page), [1,2,3]*3))
        self.assertEqual(len(images),9)
        self.assertTrue(all(image.startswith(b'\x89PNG') for image in images))

if __name__=='__main__': unittest.main()
