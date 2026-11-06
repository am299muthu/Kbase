"""Render the end-to-end swimlane: UI upload -> maker/checker -> indexing -> retrieval.

Static text only: no application imports, no document or model calls.
Outputs docs/end-to-end-flow/end-to-end-flow.png and .svg
"""
from html import escape
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'docs/end-to-end-flow'
OUT.mkdir(exist_ok=True)

FONT = '/System/Library/Fonts/Supplemental/Arial.ttf'
BOLD = '/System/Library/Fonts/Supplemental/Arial Bold.ttf'

LANES = [
    ('Maker (browser UI)', '#e8f1fb'),
    ('Checker (browser UI)', '#eaf6ee'),
    ('FastAPI + SQLite', '#f3eefb'),
    ('Private MCP tools', '#f6f0e4'),
    ('Worker (this Mac)', '#fdf3e4'),
    ('Garage S3 files', '#eef2f4'),
    ('OpenRouter models', '#fbeaea'),
    ('OpenSearch', '#e9f5f7'),
    ('Employee (chat UI)', '#e8f1fb'),
]
MAKER, CHECKER, API, MCP, WORKER, GARAGE, MODELS, SEARCH, EMPLOYEE = range(9)

# (phase title or None, lane index, title, up to three body lines)
STEPS = [
    ('A | MAKER SUBMITS', MAKER, 'Upload file', ['Choose folder + PDF/DOCX', 'Maker role needed on the folder', 'Replace / new version if duplicate']),
    (None, API, 'Create request', ['Metadata taken from the folder', 'Document = pending, version set', 'Request binds hash + scope digest']),
    (None, GARAGE, 'Stage original', ['staging/ object, SHA-256 checked', 'Streamed and read back', 'Nothing is searchable yet']),
    ('B | CHECKER REVIEWS (a different person)', CHECKER, 'Review request', ['Approvals screen: open the source', 'See models, index, dimensions,', 'price ceilings = processing scope']),
    (None, API, 'Record decision', ['Checker is not maker or owner', 'Must confirm exact scope digest', 'Approve, or reject with a note']),
    ('C | WORKER PROCESSES (only if approved scope = worker scope)', WORKER, 'Claim request', ['Approved and scope matches', 'Otherwise it stays Queued', 'One request at a time']),
    (None, GARAGE, 'Keep artifacts', ['raw/ copy, hash re-checked', 'Parsed, assets, curated saved', 'Original is never changed']),
    (None, WORKER, 'Parse locally', ['pypdf text, pdfplumber tables', 'Docling layout + object crops', 'Page / box / hash recorded']),
    (None, MODELS, 'Read visuals (VL)', ['Crops go out: free models first,', 'paid Qwen only if they fail', 'Output = generated, unverified']),
    (None, WORKER, 'Chunk', ['Text + visual readings merged', 'Each chunk keeps source refs', 'Chunks saved as curated/ files']),
    (None, MODELS, 'Embed chunks', ['qwen3-embedding-8b (paid)', '4096 dimensions, validated', 'A bad vector fails the run']),
    ('D | INDEX AND PUBLISH', SEARCH, 'Write to index', ['abc-kb-curated-multimodal-v3', 'Text for BM25 + vector for k-NN', 'Tagged with the generation ID']),
    (None, API, 'Publish in SQLite', ['Chunk hashes + manifest saved', 'Generation pointer switched', 'Document = indexed; audit logged']),
    (None, MAKER, 'Monitor / retry', ['Ingestion monitor shows each stage', 'Failure: error + model response', 'Retry by maker or checker']),
    ('E | EMPLOYEE ASKS (planner + MCP tools + retrieval)', EMPLOYEE, 'Ask a question', ['Signed-in employee in Ask', 'One or several questions', 'Session cookie identifies them']),
    (None, API, 'Start chat turn', ['Session + active employee', 'Starts the private MCP process', 'Session passed outside tool args']),
    (None, MCP, 'Check access', ['lookupemployeeaccesscontrol', 'listbusinessfolders', 'Folders come from SQLite']),
    (None, MODELS, 'LLM question planner', ['Simple question: no model call', 'Compound: split into up to 6 parts', 'Plans only; no tools, no answer']),
    (None, MCP, 'searchdocuments', ['Called once per question part', 'Entitled folders, in-date,', 'published generation only']),
    (None, MODELS, 'Embed the question', ['Same qwen3-embedding-8b', '4096-dim query vector', 'Needed for k-NN matching']),
    (None, SEARCH, 'Hybrid search', ['BM25 + k-NN in one request', 'Reciprocal rank fusion', 'Top chunks go back to the tool']),
    (None, MCP, 'getdocsection', ['Per hit: recheck folder access', 'Chunk hash + status vs SQLite', 'Stale or unauthorised hits dropped']),
    (None, MODELS, 'Generate + ground', ['Claims with citation IDs', 'Separate grounding verdict', 'Free models first, paid last']),
    (None, API, 'Release check', ['Recheck session + folder access', 'Only verified claims released', 'Any failure withholds the answer']),
    (None, EMPLOYEE, 'Cited answer', ['Answer + source citations', 'Tool trace: planner + MCP calls', 'Performance breakdown shown']),
]

NOTES_TITLE = 'Rules that hold throughout'
NOTES = [
    'Maker and checker must be different people, owners included. Approval covers one exact request and scope digest.',
    'Changing any setting in the digest (models, index, dimensions, price ceilings, page-coverage cap) needs a fresh approval.',
    'Retry re-queues the same request only if its approval still matches the worker scope; otherwise a new request needs approval.',
    'A failed run never changes what is published: the previous generation stays searchable and the error is shown in the monitor.',
    'Only approved text and image crops leave the machine, and only to OpenRouter. If every model in a chain fails, the step fails closed.',
    'Search may run while ingestion continues. With --partial-corpus it serves whatever is already indexed in the new index.',
    'Each question part runs search, section read, generation and grounding on its own: up to 6 parts per message, 2 at a time.',
    'MCP tools take no employee ID. They act for the signed-in session only, and requestaccess records a request but never grants access.',
]

LANE_W, GUTTER, MARGIN = 238, 10, 28
BOX_W, BOX_H, PITCH, PHASE_GAP = 222, 92, 112, 52
HEADER_H = 150
W = MARGIN * 2 + len(LANES) * LANE_W
top = HEADER_H + 40 + 44


def lane_x(i):
    return MARGIN + i * LANE_W


# Pre-compute box positions (phase titles add extra vertical gap before the first box of the phase).
positions = []
y = top
for index, (phase, *_rest) in enumerate(STEPS):
    if phase and index:
        y += PHASE_GAP - (PITCH - BOX_H)
    positions.append(y)
    y += PITCH
steps_end = y
notes_h = 60 + 28 * len(NOTES)
H = steps_end + 30 + notes_h + 40

im = Image.new('RGB', (W, H), '#f7f9fc')
draw = ImageDraw.Draw(im)
svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}"><rect width="{W}" height="{H}" fill="#f7f9fc"/>']


def font(size, strong=False):
    return ImageFont.truetype(BOLD if strong else FONT, size)


def text(x, y, value, size=15, color='#14283c', strong=False, anchor='l'):
    f = font(size, strong)
    width = draw.textlength(value, font=f)
    px = x - width / 2 if anchor == 'm' else x
    draw.text((px, y), value, font=f, fill=color)
    sx, extra = (x, ' text-anchor="middle"') if anchor == 'm' else (x, '')
    svg.append(f'<text x="{sx}" y="{y + size}" font-family="Arial,sans-serif" font-size="{size}" fill="{color}" font-weight="{700 if strong else 400}"{extra}>{escape(value)}</text>')


def rect(x, y, w, h, fill, stroke=None, width=1, radius=0):
    draw.rounded_rectangle((x, y, x + w, y + h), radius=radius, fill=fill, outline=stroke, width=width)
    svg.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{fill}" stroke="{stroke or fill}" stroke-width="{width}"/>')


def line(points, color='#52718c', width=2, arrow=False):
    draw.line(points, fill=color, width=width, joint='curve')
    d = 'M' + ' L'.join(f'{x},{y}' for x, y in points)
    svg.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width}"/>')
    if arrow:
        x, y = points[-1]
        tri = [(x, y), (x - 6, y - 11), (x + 6, y - 11)]
        draw.polygon(tri, fill=color)
        svg.append('<polygon points="' + ' '.join(f'{a},{b}' for a, b in tri) + f'" fill="{color}"/>')


# Header
text(MARGIN, 24, 'Knowledge solution | End-to-end flow', 36, strong=True)
text(MARGIN, 76, 'From a file in the UI, through maker-checker approval and indexing, to a cited answer. 2026-11-06 | Read top to bottom; numbers are the order of events.', 17, '#526578')
text(MARGIN, 102, 'Solid arrows: handoff to the next step. Boxes sit in the lane of whoever or whatever performs the step.', 17, '#526578')

# Lane backgrounds + headers
for i, (name, colour) in enumerate(LANES):
    rect(lane_x(i), HEADER_H, LANE_W - GUTTER, steps_end - HEADER_H - 10, colour, '#d4dbe3')
    rect(lane_x(i), HEADER_H, LANE_W - GUTTER, 34, '#142333', radius=0)
    text(lane_x(i) + (LANE_W - GUTTER) / 2, HEADER_H + 8, name, 16, '#ffffff', True, 'm')

# Phase bands (drawn first so arrows and boxes sit on top)
for index, (phase, *_r) in enumerate(STEPS):
    if not phase:
        continue
    band_top = positions[index] - 46 if index else HEADER_H + 40
    band_h = 30
    rect(MARGIN, band_top, W - MARGIN * 2, band_h, '#dfe7ef', '#c3cfdb')

# Boxes
for index, (phase, lane, title, body) in enumerate(STEPS):
    x = lane_x(lane) + (LANE_W - GUTTER - BOX_W) // 2
    y0 = positions[index]
    rect(x, y0, BOX_W, BOX_H, '#ffffff', '#52718c', 1, 5)
    rect(x, y0, BOX_W, 5, '#2b6a9a')
    text(x + 12, y0 + 11, f'{index + 1}. {title}', 16, strong=True)
    for n, row in enumerate(body):
        text(x + 12, y0 + 36 + n * 17, row, 13, '#2b3b4b')

# Arrows between consecutive steps
for index in range(len(STEPS) - 1):
    la, lb = STEPS[index][1], STEPS[index + 1][1]
    ax = lane_x(la) + (LANE_W - GUTTER) // 2
    bx = lane_x(lb) + (LANE_W - GUTTER) // 2
    a_bottom = positions[index] + BOX_H
    b_top = positions[index + 1]
    if la == lb:
        line([(ax, a_bottom), (bx, b_top)], arrow=True)
    else:
        mid = b_top - 5
        line([(ax, a_bottom), (ax, mid), (bx, mid), (bx, b_top)], arrow=True)

# Phase titles last, on a solid pill, so crossing arrows never obscure them
for index, (phase, *_r) in enumerate(STEPS):
    if not phase:
        continue
    band_top = positions[index] - 46 if index else HEADER_H + 40
    pill_w = int(draw.textlength(phase, font=font(16, True))) + 24
    rect(MARGIN + 6, band_top + 3, pill_w, 24, '#dfe7ef')
    text(MARGIN + 18, band_top + 7, phase, 16, '#142333', True)

# Notes
ny = steps_end + 30
rect(MARGIN, ny, W - MARGIN * 2, notes_h, '#e9eff6', '#c9d6e3', radius=6)
text(MARGIN + 18, ny + 14, NOTES_TITLE, 20, strong=True)
for n, note in enumerate(NOTES):
    text(MARGIN + 18, ny + 50 + n * 28, f'-  {note}', 15, '#2b3b4b')

svg.append('</svg>')
im.save(OUT / 'end-to-end-flow.png')
(OUT / 'end-to-end-flow.svg').write_text('\n'.join(svg))
print(f'Rendered end-to-end-flow.png and .svg in {OUT} ({W}x{H})')
