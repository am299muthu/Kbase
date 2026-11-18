"""Docling and Granite worker for KBase.

KBase runs this file with the Python of the local Docling install (App/docling/.venv, Python 3.14
with a CUDA build of torch), so KBase itself needs no Docling, torch or transformers packages. It is
started once per document by kb.parsers._parse_docling_local:

    python -I worker.py --input FILE --out DIR [--ocr on|off] [--granite on|off]
                        [--granite-max N] [--granite-seconds S]

It reads FILE (a PDF or an image), writes DIR/result.json and the picture crops under DIR/pictures,
and prints one JSON progress line per step on stdout. On failure it writes DIR/error.json with the
failing step and the exception type, and exits with code 2. Everything stays on this machine: the
models load from the local Hugging Face cache with downloads switched off.

Finding charts, in order (each later step is optional and only adds to the result):
  1. Docling's layout model finds picture regions; its picture classifier names each one.
  2. A picture region that holds several charts (a row of small panels, two stacked charts) is cut
     along its white gutters and divider lines, and each piece is classified on its own. The cut is
     kept only when at least two pieces are charts.
  3. Vector drawings the layout model did not mark as a picture (a donut drawn from a few arcs) are
     found from the PDF's own path objects, cropped and classified.
  4. Granite reads every chart, and every picture the classifier calls a table, into a data table,
     one at a time with a time limit, on the GPU in 4-bit when there is one. A chart it cannot
     finish as a table (a line of daily prices) gets a short written summary instead.
  5. Each Granite table is compared with the numbers printed in the PDF's text layer inside the
     chart's region. This catches invented numbers; it cannot catch a right number in the wrong cell.
"""
from __future__ import annotations

import argparse
import csv
import glob
import io
import json
import os
import re
import sys
import time
import traceback
from collections import defaultdict
from pathlib import Path

os.environ.setdefault("TORCHDYNAMO_DISABLE", "1")  # no MSVC compiler on this machine
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("DO_NOT_TRACK", "1")

GRANITE_DIR = "models--ibm-granite--granite-vision-4.1-4b"
GRANITE_NAME = "granite-vision-4.1-4b"
CLASSIFIER_DIR = "models--docling-project--DocumentFigureClassifier-v2.5"
# Picture classes Granite's chart-to-table reading handles.
CHART_CLASSES = {"bar_chart", "line_chart", "pie_chart", "stacked_bar_chart"}
# A picture the classifier calls a table is often a panel of small charts, or a table drawn as an
# image; Granite reads both into rows, so it gets a reading too.
GRANITE_CLASSES = CHART_CLASSES | {"table"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}


def say(step: str, **details) -> None:
    print(json.dumps({"progress": step, **details}), flush=True)


def hf_hub() -> Path:
    home = os.environ.get("HF_HOME")
    return Path(home) / "hub" if home else Path.home() / ".cache" / "huggingface" / "hub"


# ----------------------------------------------------------------------------------- Docling

def convert(src: Path, ocr: bool):
    from docling.datamodel.accelerator_options import AcceleratorOptions
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions, TableFormerMode
    from docling.document_converter import DocumentConverter, ImageFormatOption, PdfFormatOption

    options = PdfPipelineOptions()
    options.do_ocr = ocr
    if ocr:
        # onnxruntime is not installed in this venv; RapidOCR's torch backend and models are.
        options.ocr_options = RapidOcrOptions(backend="torch")
    options.do_table_structure = True
    options.table_structure_options.mode = TableFormerMode.ACCURATE
    options.generate_picture_images = True
    options.images_scale = 2.0
    options.do_picture_classification = True
    options.do_picture_description = False
    options.enable_remote_services = False
    options.accelerator_options = AcceleratorOptions(device="auto")
    converter = DocumentConverter(
        allowed_formats=[InputFormat.PDF, InputFormat.IMAGE],
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options),
                        InputFormat.IMAGE: ImageFormatOption(pipeline_options=options)},
    )
    result = converter.convert(str(src), raises_on_error=False)
    status = str(getattr(result.status, "value", result.status)).lower()
    if status not in {"success", "partial_success"}:
        modules = sorted({e.module_name for e in (result.errors or [])})
        raise RuntimeError(f"Docling reported {status} in {', '.join(modules) or 'the pipeline'}")
    notes = []
    if status == "partial_success":
        notes.append("Docling converted only part of the file; some pages may be missing")
    return result.document, notes


def classification(item) -> tuple[str | None, float | None]:
    meta = getattr(item, "meta", None)
    cls = getattr(meta, "classification", None) if meta is not None else None
    if cls is not None and getattr(cls, "predictions", None):
        main = cls.get_main_prediction()
        return main.class_name, (float(main.confidence) if main.confidence is not None else None)
    for annotation in getattr(item, "annotations", None) or []:  # older Docling releases
        predicted = getattr(annotation, "predicted_classes", None)
        if predicted:
            best = max(predicted, key=lambda p: p.confidence or 0)
            return best.class_name, float(best.confidence or 0)
    return None, None


def collect(document, out: Path) -> tuple[dict, list[dict]]:
    from docling_core.types.doc import PictureItem, TableItem

    pages = {str(n): {"width": float(p.size.width), "height": float(p.size.height)}
             for n, p in sorted(document.pages.items())}
    items: list[dict] = []
    section = "Content"
    pictures = 0
    for item, _level in document.iterate_items(with_groups=False):
        label = item.label.value if hasattr(item.label, "value") else str(item.label)
        boxes = []
        for prov in getattr(item, "prov", None) or []:
            page = document.pages.get(prov.page_no)
            if page is None:
                continue
            box = prov.bbox.to_top_left_origin(page_height=page.size.height)
            boxes.append({"page": prov.page_no, "l": round(box.l, 2), "t": round(box.t, 2),
                          "r": round(box.r, 2), "b": round(box.b, 2)})
        if label in {"section_header", "title"}:
            section = (getattr(item, "text", "") or section).strip() or section
        entry = {"label": label, "ref": getattr(item, "self_ref", ""), "section": section,
                 "text": (getattr(item, "text", "") or "").strip(), "boxes": boxes}
        if isinstance(item, TableItem):
            try:
                entry["text"] = item.export_to_markdown(doc=document)
            except Exception:  # noqa: BLE001  a table that will not render as Markdown keeps its cells' text
                entry["text"] = "\n".join(" | ".join(c.text for c in row) for row in item.data.grid)
        elif isinstance(item, PictureItem):
            image = item.get_image(document)
            picture = {"file": None, "class": None, "confidence": None, "chart": None,
                       "caption": (item.caption_text(document) or "").strip() if hasattr(item, "caption_text") else ""}
            if image is not None:
                name = f"pictures/{pictures}.png"
                image.convert("RGB").save(out / name)
                picture["file"] = name
                pictures += 1
            picture["class"], picture["confidence"] = classification(item)
            entry["picture"] = picture
        items.append(entry)
    return pages, items


# ----------------------------------------------------------------------------------- classifier

class Classifier:
    """Docling's own picture classifier (DocumentFigureClassifier), loaded once, for pictures this
    worker makes itself: the pieces of a split region, vector charts, a whole uploaded image."""

    def __init__(self) -> None:
        self._model = None

    def _load(self) -> None:
        snapshots = sorted(glob.glob(str(hf_hub() / CLASSIFIER_DIR / "snapshots" / "*")))
        if not snapshots:
            raise FileNotFoundError("The picture classifier is not in the local Hugging Face cache")
        from transformers import AutoImageProcessor, AutoModelForImageClassification

        self._processor = AutoImageProcessor.from_pretrained(snapshots[-1])
        self._model = AutoModelForImageClassification.from_pretrained(snapshots[-1]).eval()

    def __call__(self, image) -> tuple[str, float]:
        import torch

        if self._model is None:
            self._load()
        with torch.no_grad():
            scores = self._model(**self._processor(images=image.convert("RGB"), return_tensors="pt")).logits.softmax(-1)[0]
        best = int(scores.argmax())
        return self._model.config.id2label[best], float(scores[best])


def picture_item(ref: str, section: str, page: int, box: tuple, file: str, name: str, confidence: float, **extra) -> dict:
    l, t, r, b = box
    return {"label": "picture", "ref": ref, "section": section, "text": "",
            "boxes": [{"page": page, "l": round(l, 2), "t": round(t, 2), "r": round(r, 2), "b": round(b, 2)}],
            "picture": {"file": file, "class": name, "confidence": round(confidence, 4), "chart": None, "caption": "", **extra}}


def whole_image_chart(src: Path, out: Path, pages: dict, items: list[dict], classify: Classifier) -> None:
    """An uploaded image that is itself a chart has no picture region inside it, so Docling never
    classifies it. Classify the whole image and, when it is a chart, add it as one picture."""
    if any((e.get("picture") or {}).get("class") in CHART_CLASSES for e in items):
        return
    from PIL import Image

    image = Image.open(src).convert("RGB")
    name, confidence = classify(image)
    if name not in CHART_CLASSES or confidence < 0.6:
        return
    image.save(out / "pictures" / "whole.png")
    page = pages.get("1") or {"width": float(image.width), "height": float(image.height)}
    items.append(picture_item("#/whole-image", items[0]["section"] if items else "Content", 1,
                              (0.0, 0.0, page["width"], page["height"]), "pictures/whole.png", name, confidence))


# ----------------------------------------------------------------------------------- 2. split merged regions

def ink_mask(image):
    """True where a pixel differs clearly from the background colour (taken from the border)."""
    import numpy as np

    pixels = np.asarray(image.convert("RGB")).astype(np.int16)
    border = np.concatenate([pixels[0], pixels[-1], pixels[:, 0], pixels[:, -1]])
    background = np.median(border, axis=0)
    return np.abs(pixels - background).sum(axis=-1) > 48


def gutter_runs(fraction, min_gap: int, rule_max: int) -> list[tuple[int, int]]:
    """Runs of empty lines at least min_gap long, not touching either edge. A thin full-length rule
    (a divider line) with empty lines on both sides counts as empty, so it separates like white space;
    an axis line does not, because the bars or labels touch it."""
    n = len(fraction)
    empty = [f < 0.004 for f in fraction]
    i = 0
    while i < n:
        if fraction[i] > 0.6:
            j = i
            while j < n and fraction[j] > 0.6:
                j += 1
            if j - i <= rule_max and i > 0 and j < n and empty[i - 1] and empty[j]:
                for k in range(i, j):
                    empty[k] = True
            i = j
        else:
            i += 1
    runs, start = [], None
    for k, is_empty in enumerate(empty + [False]):
        if is_empty and start is None:
            start = k
        elif not is_empty and start is not None:
            if k - start >= min_gap and start > 0 and k < n:
                runs.append((start, k))
            start = None
    return runs


def between(length: int, runs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    cuts, pos = [], 0
    for start, end in runs:
        cuts.append((pos, start))
        pos = end
    cuts.append((pos, length))
    return [(a, b) for a, b in cuts if b > a]


def divider_cuts(mask, rule_max: int) -> list[tuple[int, int]]:
    """Rows drawn almost all the way across (a divider line between panels), thin, and not at the
    very top or bottom. An axis line is shorter than the panel, so it does not count."""
    fraction = mask.mean(axis=1)
    height = len(fraction)
    runs, start = [], None
    for k, value in enumerate(list(fraction) + [0.0]):
        if value >= 0.9 and start is None:
            start = k
        elif value < 0.9 and start is not None:
            if k - start <= rule_max and start > height * 0.03 and k < height * 0.97:
                runs.append((start, k))
            start = None
    return runs


def split_regions(image, scale: float) -> list[tuple[int, int, int, int]]:
    """Pieces of a picture that holds several charts. Divider lines across the whole picture win;
    without them it is cut at white gutters: rows first, then columns inside each row, then rows
    again inside each column (an XY cut, three levels deep)."""
    import numpy as np

    mask = ink_mask(image)
    rule_max = max(2, int(2 * scale))
    gaps = {0: max(6, int(4 * scale)), 1: max(8, int(7 * scale))}  # rows, columns
    min_w, min_h = 50 * scale, 40 * scale

    def cut(x0, y0, x1, y1, axis, depth):
        sub = mask[y0:y1, x0:x1]
        if not sub.any():
            return []
        fraction = sub.mean(axis=1 - axis) if axis == 0 else sub.mean(axis=0)
        length = y1 - y0 if axis == 0 else x1 - x0
        pieces = between(length, gutter_runs(list(fraction), gaps[axis], rule_max))
        if len(pieces) <= 1 and depth > 0:
            pieces_out = cut(x0, y0, x1, y1, 1 - axis, depth - 1) if depth > 1 else []
            return pieces_out or [(x0, y0, x1, y1)]
        result = []
        for a, b in pieces:
            box = (x0, y0 + a, x1, y0 + b) if axis == 0 else (x0 + a, y0, x0 + b, y1)
            if depth > 1:
                result.extend(cut(*box, 1 - axis, depth - 1))
            else:
                result.append(box)
        return result

    dividers = divider_cuts(mask, rule_max)
    if dividers:
        pieces = [(0, a, mask.shape[1], b) for a, b in between(mask.shape[0], dividers)]
    else:
        pieces = cut(0, 0, mask.shape[1], mask.shape[0], 0, 3)
    boxes = []
    for x0, y0, x1, y1 in pieces:
        sub = mask[y0:y1, x0:x1]
        ys, xs = np.nonzero(sub)
        if not len(xs):
            continue
        bx0, by0, bx1, by1 = x0 + xs.min(), y0 + ys.min(), x0 + xs.max() + 1, y0 + ys.max() + 1
        if bx1 - bx0 >= 4 * scale and by1 - by0 >= 4 * scale:  # drop specks only; small label pieces are kept
            boxes.append((int(bx0), int(by0), int(bx1), int(by1)))
    return boxes


def attach_labels(charts: list[list], others: list[tuple], reach: float) -> None:
    """Fold each non-chart piece (a legend column, a row of year labels under the bars, a title) into
    the chart piece it sits right beside, so Granite sees the chart's labels. charts holds
    [box, ...] lists whose box is widened in place."""
    def overlap(a0, a1, b0, b1):
        return max(0, min(a1, b1) - max(a0, b0)) / max(1, min(a1 - a0, b1 - b0))

    for o in others:
        best, best_gap = None, reach
        for chart in charts:
            c = chart[0]
            if overlap(o[0], o[2], c[0], c[2]) >= 0.3:  # above or below the chart
                gap = max(0, o[1] - c[3], c[1] - o[3])
            elif overlap(o[1], o[3], c[1], c[3]) >= 0.3:  # left or right of the chart
                gap = max(0, o[0] - c[2], c[0] - o[2])
            else:
                continue
            if gap <= best_gap:
                best, best_gap = chart, gap
        if best is not None:
            c = best[0]
            best[0] = (min(c[0], o[0]), min(c[1], o[1]), max(c[2], o[2]), max(c[3], o[3]))


def split_merged_pictures(items: list[dict], out: Path, classify: Classifier, notes: list[str]) -> int:
    """Cut picture regions that hold several charts into one picture per chart."""
    from PIL import Image

    added = 0
    for entry in list(items):
        picture = entry.get("picture")
        if not picture or not picture.get("file") or not entry.get("boxes") or picture.get("part_of"):
            continue
        box = entry["boxes"][0]
        image = Image.open(out / picture["file"]).convert("RGB")
        scale = image.width / max(1e-6, box["r"] - box["l"])
        pieces = split_regions(image, scale)
        large = [p for p in pieces if p[2] - p[0] >= 50 * scale and p[3] - p[1] >= 40 * scale]
        if len(large) < 2:
            continue
        pad = int(3 * scale)

        def crop_of(box):
            x0, y0, x1, y1 = box
            return image.crop((max(0, x0 - pad), max(0, y0 - pad), min(image.width, x1 + pad), min(image.height, y1 + pad)))

        charts = []
        for piece in large:
            name, confidence = classify(crop_of(piece))
            if name in CHART_CLASSES and confidence >= 0.5:
                charts.append([piece, name, confidence])
        if len(charts) < 2:
            continue
        chart_boxes = {tuple(c[0]) for c in charts}
        attach_labels(charts, [p for p in pieces if p not in chart_boxes], reach=32 * scale)
        picture["split_into"] = len(charts)
        stem = Path(picture["file"]).stem
        for k, ((x0, y0, x1, y1), name, confidence) in enumerate(charts, 1):
            crop = crop_of((x0, y0, x1, y1))
            file = f"pictures/{stem}-part{k}.png"
            crop.save(out / file)
            items.append(picture_item(f"{entry['ref']}/part{k}", entry["section"], box["page"],
                                      (box["l"] + x0 / scale, box["t"] + y0 / scale, box["l"] + x1 / scale, box["t"] + y1 / scale),
                                      file, name, confidence, part_of=entry["ref"]))
            added += 1
    if added:
        notes.append(f"{added} charts were cut out of picture regions that held more than one chart")
    return added


# ----------------------------------------------------------------------------------- 3. vector charts

def cluster_boxes(boxes: list[tuple], gap: float) -> list[tuple]:
    """Union of boxes that touch or lie within gap of each other; returns (l, t, r, b, count)."""
    parent = list(range(len(boxes)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    cell = 50.0
    grid = defaultdict(list)
    for i, (l, t, r, b) in enumerate(boxes):
        for gx in range(int((l - gap) // cell), int((r + gap) // cell) + 1):
            for gy in range(int((t - gap) // cell), int((b + gap) // cell) + 1):
                grid[(gx, gy)].append(i)
    for members in grid.values():
        for a_index, a in enumerate(members):
            la, ta, ra, ba = boxes[a]
            for b in members[a_index + 1:]:
                lb, tb, rb, bb = boxes[b]
                if la - gap <= rb and lb - gap <= ra and ta - gap <= bb and tb - gap <= ba:
                    root_a, root_b = find(a), find(b)
                    if root_a != root_b:
                        parent[root_b] = root_a
    groups = defaultdict(list)
    for i in range(len(boxes)):
        groups[find(i)].append(boxes[i])
    return [(min(g[0] for g in members), min(g[1] for g in members), max(g[2] for g in members),
             max(g[3] for g in members), len(members)) for members in groups.values()]


def covered_share(box: tuple, others: list[tuple]) -> float:
    l, t, r, b = box[:4]
    area = max(1e-6, (r - l) * (b - t))
    inside = sum(max(0, min(r, o[2]) - max(l, o[0])) * max(0, min(b, o[3]) - max(t, o[1])) for o in others)
    return min(1.0, inside / area)


def find_vector_charts(src: Path, pages: dict, items: list[dict], out: Path, classify: Classifier, notes: list[str]) -> int:
    """Charts drawn as vector paths that Docling's layout model did not mark as a picture."""
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c

    covered = defaultdict(list)
    for entry in items:
        if entry["label"] in {"picture", "table", "chart"}:
            for box in entry.get("boxes") or []:
                covered[box["page"]].append((box["l"], box["t"], box["r"], box["b"]))
    pdf = pdfium.PdfDocument(str(src))
    added = 0
    try:
        for key in sorted(pages, key=int):
            number = int(key)
            page = pdf[number - 1]
            width, height = page.get_size()
            boxes = []
            for obj in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_PATH], max_depth=4):
                left, bottom, right, top = obj.get_bounds()
                if (right - left) * (top - bottom) > 0.6 * width * height:
                    continue  # page backgrounds and full-page panels
                boxes.append((left, height - top, right, height - bottom))
            if not boxes or len(boxes) > 4000:
                page.close()
                continue
            candidates = []
            for l, t, r, b, _count in cluster_boxes(boxes, gap=6):
                w, h = r - l, b - t
                if w < 60 or h < 40 or w * h < 0.02 * width * height or w * h > 0.6 * width * height:
                    continue
                if covered_share((l, t, r, b), covered[number]) > 0.3:
                    continue
                candidates.append((l, t, r, b))
            if candidates:
                scale = 2.0
                rendered = page.render(scale=scale).to_pil().convert("RGB")
                for k, (l, t, r, b) in enumerate(candidates, 1):
                    margin = 10
                    l, t = max(0, l - margin), max(0, t - margin)
                    r, b = min(width, r + margin), min(height, b + margin)
                    crop = rendered.crop((int(l * scale), int(t * scale), int(r * scale), int(b * scale)))
                    name, confidence = classify(crop)
                    if name not in CHART_CLASSES or confidence < 0.6:
                        continue
                    file = f"pictures/vector-p{number}-{k}.png"
                    crop.save(out / file)
                    above = [e for e in items if e.get("boxes") and e["boxes"][0]["page"] == number and e["boxes"][0]["t"] <= t]
                    section = above[-1]["section"] if above else "Content"
                    items.append(picture_item(f"#/vector/{number}/{k}", section, number, (l, t, r, b), file, name, confidence,
                                              vector=True))
                    added += 1
            page.close()
    finally:
        pdf.close()
    if added:
        notes.append(f"{added} vector charts were found that the layout model had not marked as pictures")
    return added


# ----------------------------------------------------------------------------------- 4. Granite

def patch_transformers() -> None:
    """Granite's remote code passes cache_position to create_causal_mask; transformers 5 dropped it."""
    import inspect

    from transformers import masking_utils

    original = masking_utils.create_causal_mask
    params = inspect.signature(original).parameters
    if "cache_position" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return

    def create_causal_mask(*args, cache_position=None, **kwargs):  # noqa: ARG001
        return original(*args, **kwargs)

    masking_utils.create_causal_mask = create_causal_mask


def load_granite():
    import torch

    snapshots = sorted(glob.glob(str(hf_hub() / GRANITE_DIR / "snapshots" / "*")))
    if not snapshots:
        raise FileNotFoundError("Granite Vision weights are not in the local Hugging Face cache")
    patch_transformers()
    from transformers import AutoModelForImageTextToText, AutoProcessor

    path = snapshots[-1]
    processor = AutoProcessor.from_pretrained(path, trust_remote_code=True)
    kwargs: dict = {"trust_remote_code": True}
    device = "cpu"
    if torch.cuda.is_available() and os.environ.get("GRANITE_DEVICE", "auto") != "cpu":
        try:
            from transformers import BitsAndBytesConfig

            kwargs.update(device_map="cuda", quantization_config=BitsAndBytesConfig(
                load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=torch.bfloat16))
            device = "cuda (4-bit)"
        except ImportError:
            kwargs.update(device_map="cuda", dtype=torch.bfloat16)
            device = "cuda"
    else:
        kwargs.update(device_map="cpu", dtype=torch.bfloat16)
    model = AutoModelForImageTextToText.from_pretrained(path, **kwargs).eval()
    return processor, model, device


def table_from_csv(text: str) -> list[list[str]] | None:
    body = text
    if "```" in text:
        parts = text.split("```")
        if len(parts) >= 3:
            body = parts[1]
            if body.lower().startswith("csv"):
                body = body[3:]
    rows = [[cell.strip() for cell in row] for row in csv.reader(io.StringIO(body.strip())) if any(c.strip() for c in row)]
    if len(rows) < 2 or max(len(r) for r in rows) < 2:
        return None
    width = max(len(r) for r in rows)
    return [r + [""] * (width - len(r)) for r in rows]


def generate(processor, model, image, prompt_tag: str, seconds: int, max_tokens: int) -> tuple[str, float, int, bool]:
    conversation = [{"role": "user", "content": [{"type": "image"}, {"type": "text", "text": prompt_tag}]}]
    prompt = processor.apply_chat_template(conversation, tokenize=False, add_generation_prompt=True)
    inputs = processor(text=[prompt], images=[image], return_tensors="pt", padding=True, do_pad=True).to(model.device)
    started = time.time()
    output = model.generate(**inputs, max_new_tokens=max_tokens, use_cache=True, max_time=seconds)
    elapsed = round(time.time() - started, 1)
    new = output[0, inputs["input_ids"].shape[1]:]
    tokens = int(new.shape[0])
    cut_off = tokens >= max_tokens or elapsed >= seconds - 1
    return processor.decode(new, skip_special_tokens=True), elapsed, tokens, cut_off


def read_chart(processor, model, image, seconds: int, max_tokens: int, summary_seconds: int = 60) -> dict:
    """A data table if Granite finishes one; otherwise a short written summary of the chart."""
    text, elapsed, tokens, cut_off = generate(processor, model, image, "<chart2csv>", seconds, max_tokens)
    rows = None if cut_off else table_from_csv(text)
    if rows:
        return {"rows": rows, "summary": None, "seconds": elapsed, "tokens": tokens, "error": None, "mode": "table"}
    reason = f"the table was not finished in {round(elapsed)} s" if cut_off else "the answer was not a table"
    summary, s_elapsed, s_tokens, s_cut = generate(processor, model, image, "<chart2summary>", summary_seconds, 400)
    summary = summary.strip()
    if summary and not s_cut:
        return {"rows": None, "summary": summary, "seconds": round(elapsed + s_elapsed, 1), "tokens": tokens + s_tokens,
                "error": None, "mode": "summary", "note": f"Summary instead of a table: {reason}"}
    return {"rows": None, "summary": None, "seconds": round(elapsed + s_elapsed, 1), "tokens": tokens + s_tokens,
            "error": f"{reason}, and no summary was finished either", "mode": None}


def granite_pass(items: list[dict], out: Path, maximum: int, seconds: int, notes: list[str], summary_seconds: int = 60) -> dict:
    from PIL import Image

    charts = [e for e in items
              if (e.get("picture") or {}).get("class") in GRANITE_CLASSES and e["picture"].get("file")
              and not e["picture"].get("split_into")]
    # Charts first; pictures classed as tables only after them.
    charts.sort(key=lambda e: e["picture"]["class"] == "table")
    info = {"charts": len(charts), "read": 0, "summaries": 0, "device": None, "seconds": 0.0}
    if not charts:
        return info
    if len(charts) > maximum:
        notes.append(f"Granite read the first {maximum} of {len(charts)} charts (GRANITE_MAX_CHARTS)")
        charts = charts[:maximum]
    started = time.time()
    say("loading Granite", model=GRANITE_NAME)
    processor, model, info["device"] = load_granite()
    max_tokens = int(os.environ.get("GRANITE_MAX_TOKENS", "1536"))
    for index, entry in enumerate(charts):
        say("reading charts with Granite", completed=index, total=len(charts), model=GRANITE_NAME)
        image = Image.open(out / entry["picture"]["file"]).convert("RGB")
        longest = 1600
        if max(image.size) > longest:
            scale = longest / max(image.size)
            image = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))))
        try:
            entry["picture"]["chart"] = read_chart(processor, model, image, seconds, max_tokens, summary_seconds)
        except Exception as exc:  # noqa: BLE001  one bad chart must not lose the document
            entry["picture"]["chart"] = {"rows": None, "summary": None, "seconds": 0, "tokens": 0,
                                         "error": type(exc).__name__, "mode": None}
        chart = entry["picture"]["chart"]
        info["read"] += bool(chart.get("rows"))
        info["summaries"] += bool(chart.get("summary"))
    say("reading charts with Granite", completed=len(charts), total=len(charts), model=GRANITE_NAME)
    info["seconds"] = round(time.time() - started, 1)
    return info


# ----------------------------------------------------------------------------------- 5. check numbers

NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def numbers(text: str) -> list[float]:
    found = []
    for match in NUMBER.findall(text or ""):
        try:
            found.append(round(float(match.replace(",", "")), 4))
        except ValueError:
            continue
    return found


def totals_off(rows: list[list[str]]) -> int:
    """Rows whose Total column is not the sum of the row's other values (a right number copied into
    the wrong cell shows up here even though the number itself is printed on the page)."""
    header = [cell.lower() for cell in rows[0]]
    total_columns = [i for i, cell in enumerate(header) if i > 0 and "total" in cell]
    if len(total_columns) != 1:
        return 0
    t = total_columns[0]
    wrong = 0
    for row in rows[1:]:
        cells = [numbers(cell) for cell in row]
        if t >= len(cells) or not cells[t]:
            continue
        parts = [found[0] for i, found in enumerate(cells) if i not in (0, t) and found]
        if len(parts) >= 2 and abs(sum(parts) - cells[t][0]) > max(0.6, 0.01 * cells[t][0]):
            wrong += 1
    return wrong


def compare_with_text(rows: list[list[str]], text: str) -> dict:
    """How many of Granite's values (body cells, not the header row or the label column) are
    printed in the PDF text inside the chart's region, and whether any Total column adds up.
    Zeros are left out: a chart rarely prints a 0 for an empty segment."""
    printed = set(numbers(text))
    values = [found[0] for row in rows[1:] for cell in row[1:] for found in [numbers(cell)] if found and found[0] != 0]
    matched = sum(value in printed for value in values)
    off = totals_off(rows)
    if not values:
        status = "no-numbers"
    elif len(printed) < 3:
        status = "no-text"  # the chart is a picture without a text layer, or prints no values
    elif matched / len(values) >= 0.8 and not off:
        status = "matches"
    elif matched / len(values) >= 0.5:
        status = "partial"
    else:
        status = "not-found"
    return {"status": status, "matched": matched, "total": len(values), "totals_off": off}


def check_charts(src: Path, items: list[dict]) -> dict:
    import pypdfium2 as pdfium

    counts = defaultdict(int)
    pdf = pdfium.PdfDocument(str(src))
    try:
        for entry in items:
            chart = (entry.get("picture") or {}).get("chart") or {}
            if not chart.get("rows") or not entry.get("boxes"):
                continue
            box = entry["boxes"][0]
            page = pdf[box["page"] - 1]
            width, height = page.get_size()
            textpage = page.get_textpage()
            margin = 6
            text = textpage.get_text_bounded(left=max(0, box["l"] - margin), bottom=max(0, height - box["b"] - margin),
                                             right=min(width, box["r"] + margin), top=min(height, height - box["t"] + margin))
            textpage.close()
            page.close()
            chart["check"] = compare_with_text(chart["rows"], text)
            counts[chart["check"]["status"]] += 1
    finally:
        pdf.close()
    return dict(counts)


# ----------------------------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--ocr", choices=["on", "off"], default="on")
    parser.add_argument("--granite", choices=["on", "off"], default="on")
    parser.add_argument("--granite-max", type=int, default=25)
    parser.add_argument("--granite-seconds", type=int, default=90)
    parser.add_argument("--summary-seconds", type=int, default=60)
    args = parser.parse_args()
    src, out = Path(args.input), Path(args.out)
    (out / "pictures").mkdir(parents=True, exist_ok=True)
    is_image = src.suffix.lower() in IMAGE_SUFFIXES
    step = "loading Docling"
    try:
        import torch

        device = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu"
        say("converting with Docling")
        step = "converting with Docling"
        started = time.time()
        ocr = args.ocr == "on" or is_image
        document, notes = convert(src, ocr)
        convert_seconds = round(time.time() - started, 1)
        step = "reading the Docling layout"
        pages, items = collect(document, out)
        del document
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        granite = {"charts": 0, "read": 0, "summaries": 0, "device": None, "seconds": 0.0, "skipped": args.granite == "off",
                   "split": 0, "vector": 0, "checks": {}}
        if args.granite == "on":
            classify = Classifier()
            finders = [("classifying the image", lambda: whole_image_chart(src, out, pages, items, classify))] if is_image else [
                ("splitting merged chart regions", lambda: granite.__setitem__("split", split_merged_pictures(items, out, classify, notes))),
                ("looking for vector charts", lambda: granite.__setitem__("vector", find_vector_charts(src, pages, items, out, classify, notes))),
            ]
            for step, finder in finders:
                say(step)
                try:
                    finder()
                except Exception as exc:  # noqa: BLE001  each finder only adds charts; the rest still runs
                    notes.append(f"Step '{step}' could not run ({type(exc).__name__}); its charts may be missing")
                    traceback.print_exc(file=sys.stderr)
            step = "reading charts with Granite"
            try:
                granite.update(granite_pass(items, out, args.granite_max, args.granite_seconds, notes, args.summary_seconds))
            except Exception as exc:  # noqa: BLE001  the Docling result is still good without chart tables
                notes.append(f"Granite could not run ({type(exc).__name__}); charts are kept as pictures")
                traceback.print_exc(file=sys.stderr)
            if not is_image:
                step = "checking chart numbers against the PDF text"
                say(step)
                try:
                    granite["checks"] = check_charts(src, items)
                except Exception as exc:  # noqa: BLE001
                    notes.append(f"Chart numbers could not be checked ({type(exc).__name__})")
                    traceback.print_exc(file=sys.stderr)
        result = {"version": 2, "device": device, "ocr": ocr, "pages": pages, "items": items, "notes": notes,
                  "timings": {"docling_seconds": convert_seconds, "granite_seconds": granite.get("seconds", 0.0)},
                  "granite": granite}
        (out / "result.json").write_text(json.dumps(result), encoding="utf-8")
        say("done", completed=len(pages), total=len(pages))
        return 0
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        (out / "error.json").write_text(json.dumps({"step": step, "error_type": type(exc).__name__}), encoding="utf-8")
        return 2


if __name__ == "__main__":
    sys.exit(main())
