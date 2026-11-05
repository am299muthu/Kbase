"""Local PDF text, ruled-table extraction and Docling object identification."""
import io
import json
import re
from pathlib import Path

from kb.config import settings

from kb.parsers import ParseError, ParserLimitError, _add_text, _dependency, _docling_document, _limit


def _box(values, width, height):
    left, top, right, bottom = map(float, values)
    if not (width > 0 and height > 0 and 0 <= left < right <= width + 1 and 0 <= top < bottom <= height + 1):
        raise ParseError('PDF object has invalid page coordinates')
    return {'left': left, 'top': top, 'right': right, 'bottom': bottom,
            'page_width': float(width), 'page_height': float(height), 'origin': 'top-left'}


def _overlaps(first, second):
    def normalized(box):
        return (box['left'] / box['page_width'], box['top'] / box['page_height'],
                box['right'] / box['page_width'], box['bottom'] / box['page_height'])
    a, b = normalized(first), normalized(second)
    intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    first_area = (a[2] - a[0]) * (a[3] - a[1])
    second_area = (b[2] - b[0]) * (b[3] - b[1])
    return first_area > 0 and second_area > 0 and intersection / first_area >= 0.8 and intersection / second_area >= 0.6


def _png(image):
    if image.width * image.height > _limit('PARSER_MAX_IMAGE_PIXELS', 40000000):
        raise ParserLimitError('PDF object image exceeds PARSER_MAX_IMAGE_PIXELS')
    output = io.BytesIO()
    image.convert('RGB').save(output, format='PNG')
    return output.getvalue()


def _native_or_blank_page(page, text):
    # Positively account for simple pages; low character count alone is not a scan.
    # Unknown drawing/image/appearance content remains on the VL review path.
    if page.get('/Annots'):
        return False
    contents = page.get_contents()
    if contents is None:
        return True
    state_only = {b'q', b'Q', b'cm', b'BT', b'ET', b'Tf', b'Td', b'TD', b'Tm',
                  b'T*', b'Tc', b'Tw', b'Tz', b'TL', b'Tr', b'Ts', b'g', b'G',
                  b'rg', b'RG', b'k', b'K', b'BMC', b'BDC', b'EMC', b'MP', b'DP'}
    allowed = state_only | ({b'Tj', b'TJ', b"'", b'"'} if text.strip() else set())
    return all(operator in allowed for _, operator in contents.operations)


def _add_positioned_native_text(path, builder, page_text, pdfplumber):
    # Retain the original page-level extractor output as evidence, while using
    # positioned native text lines to interleave text with tables and images.
    builder.extraction_evidence = {'native_page_text': [
        {'page': number, 'text': text, 'extractor': 'pypdf'} for number, text in page_text.items()],
        'reading_order': 'page-y-x', 'sequence_base': 0}
    with pdfplumber.open(path) as pdf:
        for number, page in enumerate(pdf.pages, 1):
            lines = page.crop(page.bbox).extract_text_lines(return_chars=False)
            for index, line in enumerate(lines):
                if not line['text'].strip():
                    continue
                x0, y0 = page.bbox[:2]
                box = _box((line['x0']-x0, line['top']-y0, line['x1']-x0, line['bottom']-y0), page.width, page.height)
                builder.add(line['text'], f'Page {number} native text', number, 'text', metadata={
                    'object_id': f'text:{number}:{index}', 'object_kind': 'text', 'bbox': box,
                    'extractor': 'pdfplumber-native', 'generated': False})
            if not lines and page_text[number].strip():
                # Preserve otherwise unpositioned content; do not invent coordinates.
                _add_text(builder, page_text[number], f'Page {number} unpositioned text', number)


def parse_hybrid_pdf(path: Path, builder) -> None:
    pypdf = _dependency('pypdf')
    pdfplumber = _dependency('pdfplumber')
    page_text = {}
    native_pages = set()
    with path.open('rb') as source:
        reader = pypdf.PdfReader(source)
        if reader.is_encrypted and not reader.decrypt(''):
            raise ParseError('Password-protected PDF requires authorized decryption')
        if len(reader.pages) > _limit('PARSER_MAX_PAGES', 10000):
            raise ParserLimitError('PDF exceeds PARSER_MAX_PAGES')
        section = 'Content'
        for number, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ''
            page_text[number] = text
            if _native_or_blank_page(page, text):
                native_pages.add(number)


    _add_positioned_native_text(path, builder, page_text, pdfplumber)

    scanned_pages = set()
    if settings().pdf_scanned_vl_enabled:
        with pdfplumber.open(path) as pdf:
            for number, page in enumerate(pdf.pages, 1):
                sparse_text = len(re.sub(r'\s+', '', page_text[number])) < 24
                raster_page = any(
                    max(0, min(page.width, image['x1']) - max(0, image['x0']))
                    * max(0, min(page.height, image['bottom']) - max(0, image['top']))
                    > page.width * page.height * 0.6 for image in page.images)
                if (sparse_text or raster_page) and number not in native_pages:
                    scanned_pages.add(number)
    maximum = _limit('PARSER_MAX_PDF_OBJECTS', 0, allow_zero=True)
    # Refuse an impossible tile budget before rendering a potentially huge PDF.
    if maximum and len(scanned_pages) * 4 > maximum:
        raise ParserLimitError('Scanned PDF exceeds PARSER_MAX_PDF_OBJECTS')
    document = _docling_document(path, page_images=True)
    # Geometry only: no extracted text or page pixels in the debug manifest.
    builder.layout_objects = []
    for item, _ in document.iterate_items():
        for index, prov in enumerate(getattr(item, 'prov', []) or []):
            page = document.pages.get(prov.page_no)
            if page is None:
                continue
            box = prov.bbox.to_top_left_origin(page_height=page.size.height)
            builder.layout_objects.append({
                'object_id': f'layout:{getattr(item, "self_ref", "item")}:{prov.page_no}:{index}',
                'object_kind': str(getattr(item, 'label', 'unknown')).lower().split('.')[-1],
                'page': prov.page_no, 'extractor': 'docling-layout',
                'bbox': _box((box.l, box.t, box.r, box.b), page.size.width, page.size.height)})
    grids = {}
    object_count = 0
    cell_count = 0
    for number in sorted(scanned_pages):
        page = document.pages.get(number)
        if page is None or page.image is None:
            raise ParseError('Scanned PDF requires a locally rendered page for VL inference')
        image = page.image.pil_image
        if image is None:
            raise ParseError('Scanned PDF rendered image is unavailable')
        overlap = settings().pdf_scan_tile_overlap / 2
        intervals = ((0, 0.5 + overlap), (0.5 - overlap, 1))
        for row, (top, bottom) in enumerate(intervals):
            for column, (left, right) in enumerate(intervals):
                object_count += 1
                if maximum and object_count > maximum:
                    raise ParserLimitError('Scanned PDF exceeds PARSER_MAX_PDF_OBJECTS')
                region = image.crop((int(left * image.width), int(top * image.height),
                                     int(right * image.width), int(bottom * image.height)))
                # Bound upload pixels locally; never silently upload an oversized page.
                max_side = max(1, int(settings().vision_max_image_pixels ** 0.5))
                region.thumbnail((max_side, max_side))
                box = _box((left * page.size.width, top * page.size.height,
                            right * page.size.width, bottom * page.size.height), page.size.width, page.size.height)
                object_id = f'scan:{number}:{row}:{column}'
                section = f'Page {number} scanned region {row + 1}.{column + 1}'
                metadata = {'object_id': object_id, 'object_kind': 'scanned_text_region', 'bbox': box,
                            'extractor': 'local-scan-render', 'generated': False,
                            'requires_vl': True, 'scan_tile': [row, column]}
                asset = builder.asset(_png(region), '.png', section, number, **metadata,
                                      extraction='Overlapping scanned-page region; VL reading required, no OCR transcription')
                builder.add('[Scanned PDF region captured; VL reading required]', section, number,
                            'image', asset, metadata)
    with pdfplumber.open(path) as pdf:
        for number, page in enumerate(pdf.pages, 1):
            if not page_text[number].strip():
                continue  # Grid extraction is for electronic/searchable pages, never OCR guesses.
            # PDF drawing commands can extend far outside the visible page.
            # Clip before table detection, not after extracting invalid cells.
            page = page.crop(page.bbox)
            tables = page.find_tables({'vertical_strategy': 'lines', 'horizontal_strategy': 'lines'})
            for index, table in enumerate(tables, 1):
                rows = table.extract()
                if not rows or not any(cell not in (None, '') for row in rows for cell in row):
                    continue
                object_count += 1
                cell_count += sum(len(row) for row in rows)
                if (maximum and object_count > maximum) or cell_count > _limit('PARSER_MAX_CELLS', 1000000):
                    raise ParserLimitError('PDF tables exceed the configured object/cell limits')
                x0, top, x1, bottom = table.bbox
                offset_x, offset_y = page.bbox[:2]
                box = _box((x0 - offset_x, top - offset_y, x1 - offset_x, bottom - offset_y), page.width, page.height)
                grids.setdefault(number, []).append(box)
                if page.width * page.height * 1.5 ** 2 > _limit('PARSER_MAX_IMAGE_PIXELS', 40000000):
                    raise ParserLimitError('PDF table rendering exceeds PARSER_MAX_IMAGE_PIXELS')
                image = page.crop(table.bbox).to_image(resolution=108).original
                object_id = f'grid:{number}:{index}'
                section = f'Page {number} grid table {index}'
                metadata = {'object_id': object_id, 'object_kind': 'table', 'bbox': box,
                            'extractor': 'pdfplumber-lines', 'generated': False}
                asset = builder.asset(_png(image), '.png', section, number, **metadata,
                                      extraction='Electronic ruled-table crop; cell text extracted by pdfplumber')
                text = 'Electronic grid table (pdfplumber; exact cell rows):\n' + '\n'.join(
                    f'Row {row_number}: {json.dumps(row, ensure_ascii=False)}' for row_number, row in enumerate(rows, 1))
                builder.add(text, section, number, 'table', asset, metadata)

    doc_types = _dependency('docling_core.types.doc', 'docling-core')
    for item, _level in document.iterate_items():
        label = str(getattr(item, 'label', '')).lower().split('.')[-1]
        if isinstance(item, doc_types.TableItem):
            label = 'table'
        elif isinstance(item, doc_types.PictureItem) and label != 'chart':
            label = 'picture'
        provenance = getattr(item, 'prov', []) or []
        if label not in {'table', 'picture', 'chart'}:
            # Supplement only pages where the electronic text extractor found nothing.
            for prov in provenance:
                if prov.page_no not in scanned_pages and not page_text.get(prov.page_no, '').strip():
                    text = getattr(item, 'text', '')
                    page = document.pages[prov.page_no]
                    bounds = prov.bbox.to_top_left_origin(page_height=page.size.height)
                    builder.add(text, f'Page {prov.page_no} Docling supplemental text', prov.page_no, 'text',
                                metadata={'object_id': f'text-docling:{item.self_ref}:{prov.page_no}',
                                          'bbox': _box((bounds.l, bounds.t, bounds.r, bounds.b), page.size.width, page.size.height),
                                          'extractor': 'docling', 'generated': False})
            continue
        if not provenance:
            raise ParseError('Docling visual object has no page provenance')
        for prov_index, prov in enumerate(provenance):
            if prov.page_no in scanned_pages:
                continue  # VL reads overlapping scan regions; do not also upload a whole-page picture.
            page = document.pages.get(prov.page_no)
            if page is None or prov.page_no not in page_text:
                raise ParseError('Docling visual object refers to an unknown page')
            box = prov.bbox.to_top_left_origin(page_height=page.size.height)
            box = _box((box.l, box.t, box.r, box.b), page.size.width, page.size.height)
            if label == 'table' and any(_overlaps(box, grid) for grid in grids.get(prov.page_no, [])):
                continue  # The searchable ruled table already has deterministic cell extraction.
            object_count += 1
            if maximum and object_count > maximum:
                raise ParserLimitError('PDF exceeds PARSER_MAX_PDF_OBJECTS')
            image = item.get_image(document, prov_index=prov_index)
            if image is None:
                raise ParseError('Docling object has no crop; local page images are required')
            object_id = f'docling:{getattr(item, "self_ref", label)}:{prov.page_no}:{prov_index}'
            section = f'Page {prov.page_no} {label} {object_count}'
            metadata = {'object_id': object_id, 'object_kind': label, 'bbox': box,
                        'extractor': 'docling', 'generated': False}
            asset = builder.asset(_png(image), '.png', section, prov.page_no, **metadata,
                                  extraction='Docling object crop; not a whole-page upload')
            text = item.export_to_markdown(doc=document) if label == 'table' else '[PDF picture captured; visual interpretation pending]'
            builder.add(text, section, prov.page_no, 'table' if label == 'table' else 'image', asset, metadata)
