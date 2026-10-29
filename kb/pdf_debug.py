"""Annotated source copies for local layout validation; no model calls."""
import io
from collections import defaultdict
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Rectangle, FreeText
from pypdf.generic import ArrayObject, FloatObject, NameObject, NumberObject, TextStringObject


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
