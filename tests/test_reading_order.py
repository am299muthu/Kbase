import tempfile
import unittest
from pathlib import Path

from kb.parsers import ParsedBlock, ParsedDocument, _Builder, chunk_document
from kb.reading_order import order_pdf_blocks, evidence_snapshot


def block(text, page, top, left, object_id, generated=False, asset=None):
    return ParsedBlock(text, f'Page {page}', page, 'vision_description' if generated else 'text', asset,
        {'object_id': object_id, 'bbox': {'top': top, 'left': left, 'bottom': top+10, 'right': left+20,
         'page_width': 600, 'page_height': 800, 'origin': 'top-left'}, 'generated': generated})


class ReadingOrderTests(unittest.TestCase):
    def test_pages_then_y_then_x_with_description_beside_source(self):
        parsed = ParsedDocument([
            block('page two',2,0,0,'p2'), block('right',1,10,200,'right'),
            block('description',1,20,0,'table',True,'crop.png'),
            block('lower',1,40,0,'lower'), block('left',1,10,10,'left'),
            block('table',1,20,0,'table',False,'crop.png'),
        ], [], 'test', [{'object_id':'layout'}], {'native_page_text':['source evidence']})
        ordered=order_pdf_blocks(parsed)
        self.assertEqual([b.text for b in ordered.blocks],['left','right','table','description','lower','page two'])
        self.assertEqual([b.metadata['sequence'] for b in ordered.blocks],list(range(6)))
        self.assertEqual(order_pdf_blocks(ordered),ordered)
        self.assertEqual(ordered.layout_objects,parsed.layout_objects)
        self.assertEqual(ordered.extraction_evidence,parsed.extraction_evidence)
        self.assertNotIn('sequence',parsed.blocks[0].metadata)

    def test_unpositioned_text_is_preserved_and_explicitly_labelled(self):
        parsed=ParsedDocument([ParsedBlock('fallback',page=1),block('positioned',1,20,30,'a')],[],'test')
        ordered=order_pdf_blocks(parsed)
        self.assertEqual([b.text for b in ordered.blocks],['positioned','fallback'])
        self.assertEqual(ordered.blocks[1].metadata['ordering_method'],'page-only-fallback')

    def test_chunk_order_and_evidence_indices_match_stored_blocks(self):
        asset={'path':'/tmp/crop.png','page':1,'section':'Page 1','object_id':'table','mime_type':'image/png'}
        parsed=order_pdf_blocks(ParsedDocument([
            block('last page',2,0,0,'last'),block('after table',1,40,0,'after'),
            block('table source',1,20,0,'table',False,asset['path']),
            block('table interpretation',1,20,0,'table',True,asset['path']),
            block('first line',1,0,0,'first'),block('second line',1,10,0,'second'),
        ],[asset],'test'))
        stored={**asset,'path':'s3://bucket/assets/doc/crop.png','object_key':'assets/doc/crop.png'}
        snapshot=evidence_snapshot(parsed,[stored])
        chunks=chunk_document(parsed,max_chars=1000,overlap=0)
        self.assertEqual([c['sequence'] for c in chunks],list(range(len(chunks))))
        self.assertEqual(chunks[0]['source_block_indices'],[0,1])
        self.assertEqual(chunks[1]['source_block_indices'],[2,3])
        self.assertEqual(chunks[-1]['page'],2)
        for chunk in chunks:
            for index in chunk['source_block_indices']:
                self.assertIn(snapshot['blocks'][index]['text'],chunk['text'])
        self.assertEqual(snapshot['blocks'][2]['asset_path'],stored['path'])
        self.assertEqual(parsed.blocks[2].asset_path,'/tmp/crop.png')

    def test_positioned_native_text_from_real_pdf(self):
        import pdfplumber
        from pypdf import PdfWriter
        from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
        from kb.pdf_hybrid import _add_positioned_native_text
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'sample.pdf';writer=PdfWriter();page=writer.add_blank_page(width=600,height=800)
            font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
            page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
            content=DecodedStreamObject();content.set_data(b'BT /F1 12 Tf 40 600 Td (Bottom line) Tj ET BT /F1 12 Tf 40 750 Td (Top line) Tj ET')
            page[NameObject('/Contents')]=writer._add_object(content);writer.write(path)
            builder=_Builder(Path(temp)/'assets','docling+pypdf+pdfplumber')
            _add_positioned_native_text(path,builder,{1:'Bottom line\nTop line'},pdfplumber)
            parsed=order_pdf_blocks(builder.finish())
            self.assertEqual([b.text for b in parsed.blocks],['Top line','Bottom line'])
            self.assertLess(parsed.blocks[0].metadata['bbox']['top'],parsed.blocks[1].metadata['bbox']['top'])
            self.assertEqual(parsed.extraction_evidence['native_page_text'][0]['text'],'Bottom line\nTop line')


if __name__=='__main__':unittest.main()
