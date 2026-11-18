"""Annotated source copies for local layout validation; no model calls."""
import io
from collections import defaultdict
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Rectangle, FreeText
from pypdf.generic import ArrayObject, FloatObject, NameObject, NumberObject, TextStringObject


def _markdown_table(text):
    """Split Markdown into table rows and the lines around them (a caption, a note)."""
    rows, other = [], []
    for line in (text or '').splitlines():
        stripped = line.strip()
        if stripped.startswith('|'):
            row = [cell.strip() for cell in stripped.strip('|').split('|')]
            if not all(cell and set(cell) <= set('-: ') for cell in row):  # the |---|---| separator
                rows.append(row)
        elif stripped and not stripped.startswith(_READING_MARKERS):
            other.append(stripped)
    return rows, ' '.join(other)


# The first line of a stored model reading, e.g. "[Chart data read by Granite Vision; ...]".
_READING_MARKERS = ('[Chart data read by', '[Chart summary written by', '[Generated visual interpretation')


def _without_marker(text):
    lines = (text or '').splitlines()
    if lines and lines[0].strip().startswith(_READING_MARKERS):
        lines = lines[1:]
    return '\n'.join(lines).strip()


def extracted_data(parsed):
    """The tables and chart data stored with a run, keyed by the object they came from, for the
    object inspector. Tables are Docling's (structure from the layout model, cell text from the PDF);
    charts are model readings (Granite). Reads the parsed record only; no model calls."""
    captions = {}
    for block in parsed.get('blocks', []):
        meta = block.get('metadata') or {}
        if block.get('kind') == 'image' and meta.get('object_id') and not meta.get('generated'):
            captions[meta['object_id']] = (block.get('text') or '').split('] ', 1)[-1].strip(' []')
    items = []
    for block in parsed.get('blocks', []):
        meta = block.get('metadata') or {}
        object_id = meta.get('object_id')
        if not object_id:
            continue
        if meta.get('generated') and meta.get('model'):
            rows, _ = _markdown_table(block.get('text'))
            caption = captions.get(object_id, '')
            items.append({'object_id': object_id, 'page': block.get('page'), 'kind': 'chart', 'source': meta['model'],
                          'generated': True, 'seconds': meta.get('model_seconds'), 'rows': rows,
                          'caption': '' if caption.startswith('Picture:') else caption,
                          'mode': meta.get('reading_mode') or ('table' if rows else 'text'),
                          'check': meta.get('check'), 'note': meta.get('note'),
                          'text': '' if rows else _without_marker(block.get('text'))})
        elif block.get('kind') == 'table':
            rows, caption = _markdown_table(block.get('text'))
            if rows:
                items.append({'object_id': object_id, 'page': block.get('page'), 'kind': 'table',
                              'source': meta.get('extractor') or 'parser', 'generated': False, 'seconds': None,
                              'rows': rows, 'caption': caption, 'mode': 'table', 'check': None, 'note': None,
                              'text': ''})
    return items


def annotate_pdf(source, objects, layer='all'):
    if layer not in {'all', 'layout', 'crops'}:
        raise ValueError('Unknown geometry layer')
    writer = PdfWriter()
    for page in PdfReader(source).pages:
        if page.rotation:
            page.transfer_rotation_to_content()
        writer.add_page(page)
    counts = defaultdict(int)
    for obj in objects:
        number = obj['page']
        counts[number] += 1  # Same per-page numbering as the geometry viewer.
        layout = obj.get('extractor') == 'docling-layout'
        if (layer == 'layout' and not layout) or (layer == 'crops' and layout):
            continue
        if not 1 <= number <= len(writer.pages):
            raise ValueError('Object page outside source PDF')
        page = writer.pages[number-1]
        crop = page.cropbox
        b = obj['bbox']
        if b.get('origin') != 'top-left' or b['page_width'] <= 0 or b['page_height'] <= 0:
            raise ValueError('Invalid coordinate system')
        sx, sy = float(crop.width)/b['page_width'], float(crop.height)/b['page_height']
        x0 = float(crop.left)+b['left']*sx
        x1 = float(crop.left)+b['right']*sx
        y0 = float(crop.top)-b['bottom']*sy
        y1 = float(crop.top)-b['top']*sy
        rect = Rectangle((x0,y0,x1,y1))
        color = '2563EB' if layout else 'D97706'
        rect[NameObject('/C')] = ArrayObject([FloatObject(int(color[i:i+2],16)/255) for i in (0,2,4)])
        rect[NameObject('/F')] = NumberObject(4)
        rect[NameObject('/Contents')] = TextStringObject(f"{counts[number]}. {obj.get('object_kind')} | {obj.get('object_id')} | {obj.get('extractor')}")
        writer.add_annotation(number-1,rect)
        label = FreeText(text=str(counts[number]), rect=(x0,max(float(crop.bottom),y1-12),min(x0+30,float(crop.right)),y1), font_size='9pt',font_color=color,border_color=None)
        label[NameObject('/F')] = NumberObject(4)
        writer.add_annotation(number-1,label)
    out=io.BytesIO()
    writer.write(out)
    return out.getvalue()
