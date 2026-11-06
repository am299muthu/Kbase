#!/usr/bin/env python3
"""Render architecture PNG and editable diagrams.net XML from one neutral layout.

Requires Pillow. No application imports, document reads, secrets or model calls.
Run from any working directory: python3 /path/to/scripts/render_architecture.py
"""
from pathlib import Path
import argparse
import math
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SHIFT = 420  # vertical room for the chat orchestration region
WIDTH, HEIGHT = 2000, 2750 + SHIFT
LEGEND_Y = 2695 + SHIFT
INK, MUTED, LINE = '#18232D', '#4C5864', '#667581'
SURFACE, BORDER, REGION = '#FFFFFF', '#BBC5CD', '#F3F5F7'
TITLE = 'ABC KNOWLEDGE | ARCHITECTURE + ACCESS CONTROL'
SUBTITLE = '2026-11-06 | Chat: OpenSearch hybrid retrieval | Garage: source files | SQLite: access + publication authority'
LEGEND = 'Solid: current live path. Dashed: disabled in this profile / future controls. Arrows show representative flows, not every call.'

# Identity and state use labels/line style, not categorical colors.
BOXES = [
    ('ui', 80, 245, 385, 190, 'Employee browser UI', 'Synthetic employee sign-in\nChat / uploads / approvals\nIngestion monitor: stage, error, retry\nOwner/delegate access management\nNo direct storage/search credentials'),
    ('api', 565, 245, 385, 190, 'FastAPI authentication', 'HttpOnly, SameSite session cookie\nHashed token + expiry in SQLite\nActive employee required\nDemo identity; not enterprise SSO'),
    ('acl', 1050, 245, 385, 190, 'Application folder ACL', 'Owner/delegate inherit to children\nMaker/checker/reader: exact folder\nFolder grants authorize visibility\nNo role bypasses maker separation'),
    ('db', 1535, 245, 385, 190, 'SQLite control authority', 'Employees / sessions / folder grants\nDocuments / processing approvals\nChunk hashes + generation pointers\nAudit hash chain; no chunk text'),
    ('review', 80, 585, 385, 230, 'Maker / checker governance', 'Maker stages source + target scope\nAuthenticated independent checker\nChecker differs from both makers\nHash / version / metadata binding\nParser / models / destination binding\nNo CLI auto-approval or impersonation'),
    ('worker', 565, 585, 385, 230, 'Approved-request worker', 'Only exact runtime-scope matches\nRecheck current roles + source hash\nStage new generation; index first\nThen commit SQL publication pointer\nFailed replacement keeps old data\nFailure keeps model-response excerpts\nRetry re-queues under valid approval'),
    ('retrieval', 1050, 585, 385, 230, 'Chat orchestrator (FastAPI)', 'Access / folder questions: fixed routing\nCompound questions: LLM planner\nCalls 5 private MCP tools (stdio)\nSession-bound; no caller employee ID\nSQL rechecks current source + hashes\nAccess request never grants access'),
    ('output', 1535, 585, 385, 230, 'Response release controls', 'LIVE: cited generated answers\nIndependent grounding verdicts\nValidate cited source evidence\nRecheck session + folders at release\nGuard/model failure withholds answer\nFree models can be rate-limited'),
    ('objects', 80, 980, 385, 220, 'Garage S3 file layers', 'staging / raw / raw-parsed\ncurated / assets; original content\nSHA-256 + streamed readback\nLoopback HTTP; private bucket key\nSerialized-local application writes\nIngestion/reprocessing source files'),
    ('parser', 565, 980, 385, 220, 'Local document processing', 'pypdf: electronic text\npdfplumber: searchable grid cells\nDocling: object detection + crops\nScans: overlapping rendered tiles\nPage / bbox / hash provenance\nDOCX: local text/table extraction'),
    ('search', 1050, 980, 385, 220, 'OpenSearch hybrid retrieval', 'v3: BM25 + k-NN, 4096 dims (filling)\nv1: legacy lexical candidates\nTLS / local CA / service login\nApplication filters + SQL rechecks\nSQLite is not the content index\nNew ingestion requires real vectors'),
    ('service', 1535, 980, 385, 220, 'Service access != employee ACL', 'OpenSearch: exact index scopes\nS3: named-bucket read/write only\nCredentials remain server-side\nNeither service enforces folder roles\nNo direct end-user object/index access\nOriginal source content retained'),
    ('gates', 80, 1410, 385, 215, 'External transfer gates', 'Checker-approved transfer scope\nCurrent approved ingestion scope\nRoles rechecked before each retry\nInteractive calls: current session ACL\nApproved text sent to model\nApproved crops sent to vision model'),
    ('vision', 565, 1410, 385, 215, 'VL interpretation', 'Free VL chain, then paid Qwen\n429/5xx/bad JSON -> next model\nObjects incl. full-slide images\nGenerated/unverified readings\nPreserve page / bbox / source hash\nInvalid output fails; none invented'),
    ('text', 1050, 1410, 385, 215, 'Text embedding space', 'OPENROUTER_EMBEDDING_MODEL\nEMBEDDING_DIMENSIONS = 4096\nqwen3-embedding-8b (paid, 1 model)\nDedicated vector field / model ID\nLucene HNSW / cosine\nNever mix with image-model vectors'),
    ('image', 1535, 1410, 385, 215, 'Image embedding space', 'Disabled in pdf-hybrid profile\nImages searchable via VL text only\nSeparate image model when enabled\nDedicated image_vector / model ID\nNever mixed with text vectors\nFull hybrid profile: not active'),
    ('guardrails', 80, 1715, 385, 205, 'Inference safety checks', 'Structured cited claims\nCheck source IDs and integrity\nValidate each factual claim\nSeparate supported/relevant verdict\nFail closed on malformed judgements\nModel verdicts are not guarantees'),
    ('generation', 565, 1715, 385, 205, 'Cited OpenRouter inference', 'Free models first, then paid Qwen\nPaid capped by max-price ceilings\nStructured claims + valid citation IDs\nGrounding checks before release\nFinal session / source / ACL recheck\nAll models fail: answer withheld'),
    ('hybrid', 1050, 1715, 385, 205, 'Hybrid ranking: LIVE (PARTIAL)', 'abc-kb-curated-multimodal-v3\nEvery entitled folder is eligible\nBM25 + text k-NN\nReciprocal rank fusion\nPublished generations only\nCurrent SQL integrity checks retained'),
    ('ready', 1535, 1715, 385, 205, 'Fail-closed hybrid startup', 'profiles/pdf-hybrid.env profile\nChecks current published corpus\nApprovals / hashes / model vectors\nserve --partial-corpus overrides\nInvalid target blocks normal startup\nASGI gate; profile-check exit 2'),
    ('telemetry', 80, 2110, 590, 220, 'Application telemetry: OPT-IN', 'HTTP / chat / MCP / ingestion spans\nBM25, vector and model-call timings\nCorrelated trace IDs and error types\nNo prompt / document / secret payloads\nKB_TELEMETRY_ENABLED: off by default'),
    ('prepper', 705, 2110, 590, 220, 'Data Prepper 2.16: INSTALLED', 'OpenTelemetry log + trace sources\nLog indexing + trace processing\nService-map processing\nDedicated observability indexes\nNot running until started'),
    ('dashboards', 1330, 2110, 590, 220, 'OpenSearch Dashboards', 'Login required; loopback only\nQuery Insights + slow logs\nLog / trace analytics screens\nSearch telemetry by trace / request ID\nCorpus index remains separate\nTelemetry is not RAG evidence'),
    ('prod1', 80, 2480, 590, 155, 'Identity + document protection', 'Enterprise SSO / MFA / managed secrets / KMS\nMalware scanning / OS-level parser isolation\nClaim accuracy + visual-reading evaluation'),
    ('prod2', 705, 2480, 590, 155, 'Resilience + operations', 'HA / durable queue / backups and restore drills\nRemote TLS / distributed write coordination\nScale, quotas, model availability and monitoring'),
    ('prod3', 1330, 2480, 590, 155, 'Governance assurance', 'Independent legal / risk / privacy approval identities\nImmutable audit / retention / WORM controls\nFull citation-entailment and security evaluation'),
]
EDGES = [
    ('obs1', 'telemetry', 'prepper', [(670, 2220), (705, 2220)], '', True),
    ('obs2', 'prepper', 'dashboards', [(1295, 2220), (1330, 2220)], '', True),
    ('e1', 'ui', 'api', [(465, 340), (565, 340)], 'session', False),
    ('e2', 'api', 'acl', [(950, 340), (1050, 340)], 'roles', False),
    ('e3', 'acl', 'db', [(1435, 340), (1535, 340)], 'SQL', False),
    ('e4', 'api', 'review', [(700, 435), (700, 495), (272, 495), (272, 585)], 'request + authenticated review', False),
    ('e5', 'acl', 'retrieval', [(1242, 435), (1242, 585)], 'permitted folders', False),
    ('e6', 'review', 'worker', [(465, 700), (565, 700)], 'approved', False),
    ('e7', 'retrieval', 'output', [(1435, 700), (1535, 700)], 'evidence', False),
    ('e8', 'worker', 'parser', [(757, 815), (757, 980)], 'parse', False),
    ('e9', 'parser', 'objects', [(565, 1090), (465, 1090)], 'objects', False),
    ('e10', 'parser', 'search', [(950, 1090), (1050, 1090)], 'index', False),
    ('e11', 'retrieval', 'search', [(1242, 815), (1242, 980)], 'BM25 + k-NN', False),
    ('e12', 'service', 'search', [(1535, 1090), (1435, 1090)], 'service', False),
    ('e13', 'gates', 'vision', [(465, 1515), (565, 1515)], 'pixels', False),
    ('e14', 'gates', 'guardrails', [(272, 1625), (272, 1715)], 'approved text', False),
    ('e15', 'text', 'hybrid', [(1242, 1625), (1242, 1715)], 'text vectors', False),
    ('e16', 'image', 'hybrid', [(1727, 1625), (1727, 1650), (1350, 1650), (1350, 1715)], 'image vectors', True),
    ('e17', 'guardrails', 'generation', [(465, 1818), (565, 1818)], 'checks', False),
    ('e18', 'hybrid', 'ready', [(1435, 1818), (1535, 1818)], 'verify', False),
]
REGIONS = [
    ('local', 40, 145, 1920, 1115, 'LIVE SERVING PATH | APPLICATION ACCESS CONTROL + GOVERNED STORAGE', 'Chat evidence comes from OpenSearch; SQLite verifies access and published hashes; Garage holds ingestion files', False),
    ('external', 40, 1310, 1920, 665, 'EXTERNAL MODEL BOUNDARY | APPROVED VL + TEXT EMBEDDINGS + CITED GENERATION', 'All new ingestion requires hybrid processing and matching approval; separate image embeddings remain disabled', False),
    ('observability', 40, 2025, 1920, 345, 'OBSERVABILITY | QUERY INSIGHTS LIVE; TELEMETRY PIPELINE IMPLEMENTED, OFF BY DEFAULT', 'Operational telemetry flows separately from corpus ingestion and question answering', True),
    ('production', 40, 2415, 1920, 250, 'FUTURE PRODUCTION ASSURANCES | NOT PROVIDED BY THIS LOCAL PROTOTYPE', '', True),
]


def _shift_y(value):
    return value + SHIFT if value >= 1300 else value


BOXES = [(k, x, _shift_y(y), w, h, t, b) for k, x, y, w, h, t, b in BOXES]
EDGES = [(k, a, b, [(px, _shift_y(py)) for px, py in pts], label, dashed) for k, a, b, pts, label, dashed in EDGES]
REGIONS = [(k, x, _shift_y(y), w, h, t, sub, d) for k, x, y, w, h, t, sub, d in REGIONS]

# Chat orchestration region (inserted between the local region and the external model boundary)
REGIONS.insert(1, ('chat', 40, 1310, 1920, 370, 'CHAT ORCHESTRATION | LLM QUESTION PLANNER + PRIVATE MCP TOOLS',
                   '', False))
BOXES += [
    ('planner', 80, 1415, 385, 230, 'LLM question planner', 'Simple question: direct search, no model\nCompound: model splits into up to 6 parts\nParts must reproduce the whole message\nPlanner fails: local split fallback\nPlans only: no tools, no answers\nRuns on the answer-model chain'),
    ('mcp', 565, 1415, 385, 230, 'Private MCP tools (stdio)', 'searchdocuments: entitled evidence\ngetdocsection: re-checks access, reads\nlookupemployeeaccesscontrol: own roles\nlistbusinessfolders: permitted only\nrequestaccess: records, never grants\nSession via environment, not arguments'),
    ('parts', 1050, 1415, 385, 230, 'Per-question evidence loop', 'Up to 6 parts, 2 running at a time\nsearchdocuments -> getdocsection\nCited claims generated from sections\nGrounding check + one correction try\nA failed part is reported, not hidden\nMerged answer + source citations'),
    ('chatout', 1535, 1415, 385, 230, 'Release + trace', 'Recheck session + folder access\nAnswer + citation list to the browser\nTool trace: planner + MCP calls\nAudit event: chat.completed\nTimings: planning, retrieval, generation\nNo access change by any tool'),
]
EDGES += [
    ('e19', 'planner', 'mcp', [(465, 1530), (565, 1530)], 'plan', False),
    ('e20', 'mcp', 'parts', [(950, 1530), (1050, 1530)], 'evidence', False),
    ('e21', 'parts', 'chatout', [(1435, 1530), (1535, 1530)], 'answer', False),
    ('e22', 'mcp', 'search', [(870, 1415), (870, 1240), (1340, 1240), (1340, 1200)], 'searchdocuments', False),
    ('e23', 'retrieval', 'planner', [(1050, 700), (1000, 700), (1000, 1285), (272, 1285), (272, 1415)], 'questions', True),
]


def render_xml(destination):
    mxfile = ET.Element('mxfile', host='app.diagrams.net', version='24.7.17')
    diagram = ET.SubElement(mxfile, 'diagram', id='abc-kb', name='Architecture + access control')
    model = ET.SubElement(diagram, 'mxGraphModel', dx=str(WIDTH), dy=str(HEIGHT), grid='1', gridSize='10', page='1', pageWidth=str(WIDTH), pageHeight=str(HEIGHT))
    root = ET.SubElement(model, 'root')
    ET.SubElement(root, 'mxCell', id='0')
    ET.SubElement(root, 'mxCell', id='1', parent='0')

    def vertex(key, x, y, w, h, value, style):
        cell = ET.SubElement(root, 'mxCell', id=key, value=value, style=style, vertex='1', parent='1')
        ET.SubElement(cell, 'mxGeometry', x=str(x), y=str(y), width=str(w), height=str(h), attrib={'as': 'geometry'})

    vertex('title', 40, 25, 1920, 60, TITLE, 'text;html=0;align=left;fontSize=30;fontStyle=1;fontColor=' + INK)
    vertex('subtitle', 40, 85, 1920, 35, SUBTITLE, 'text;html=0;align=left;fontSize=18;fontColor=' + MUTED)
    for key, x, y, w, h, title, subtitle, dashed in REGIONS:
        vertex(key, x, y, w, h, '', f'rounded=1;html=0;fillColor={REGION};strokeColor={BORDER};dashed={int(dashed)};')
        vertex(key + '-header', x+20, y+12, w-40, 65 if subtitle else 35, title + ('\n' + subtitle if subtitle else ''), f'text;whiteSpace=wrap;html=0;align=left;verticalAlign=top;fontSize=18;fontColor={INK};')
    by_id = {box[0]: box for box in BOXES}
    for key, source, target, points, label, dashed in EDGES:
        _, sx, sy, sw, sh, *_ = by_id[source]
        _, tx, ty, tw, th, *_ = by_id[target]
        start, end = points[0], points[-1]
        anchors = f'exitX={(start[0]-sx)/sw};exitY={(start[1]-sy)/sh};entryX={(end[0]-tx)/tw};entryY={(end[1]-ty)/th};exitPerimeter=0;entryPerimeter=0;'
        cell = ET.SubElement(root, 'mxCell', id=key, source=source, target=target, value=label, edge='1', parent='1', style=f'edgeStyle=orthogonalEdgeStyle;rounded=0;html=0;endArrow=block;strokeColor={LINE};strokeWidth=2;dashed={int(dashed)};fontSize=14;labelBackgroundColor={REGION};' + anchors)
        geometry = ET.SubElement(cell, 'mxGeometry', relative='1', attrib={'as': 'geometry'})
        if len(points) > 2:
            array = ET.SubElement(geometry, 'Array', attrib={'as': 'points'})
            for x, y in points[1:-1]:
                ET.SubElement(array, 'mxPoint', x=str(x), y=str(y))
    for key, x, y, w, h, title, body in BOXES:
        vertex(key, x, y, w, h, title + '\n\n' + body, f'rounded=1;whiteSpace=wrap;html=0;fontSize=17;fontColor={INK};fillColor={SURFACE};strokeColor={BORDER};spacing=12;')
    vertex('legend', 40, LEGEND_Y, 1920, 30, LEGEND, 'text;html=0;align=left;fontSize=16;fontColor=' + MUTED)
    ET.indent(mxfile)
    ET.ElementTree(mxfile).write(destination, encoding='utf-8', xml_declaration=True)


def render_png(destination):
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new('RGB', (WIDTH, HEIGHT), SURFACE)
    draw = ImageDraw.Draw(image)

    def font(size, bold=False):
        choices = [
            '/System/Library/Fonts/Supplemental/Arial Bold.ttf' if bold else '/System/Library/Fonts/Supplemental/Arial.ttf',
            '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        ]
        for path in choices:
            if Path(path).exists():
                return ImageFont.truetype(path, size)
        return ImageFont.load_default(size=size)

    def text_at(position, text, size, *, bold=False, fill=INK, max_width=None):
        face = font(size, bold)
        if max_width is not None and draw.textlength(text, font=face) > max_width:
            raise ValueError(f'Architecture label exceeds available width: {text}')
        draw.text(position, text, fill=fill, font=face)

    text_at((40, 30), TITLE, 34, bold=True, max_width=1920)
    text_at((40, 85), SUBTITLE, 20, fill=MUTED, max_width=1920)
    for _, x, y, w, h, title, subtitle, dashed in REGIONS:
        draw.rounded_rectangle((x, y, x+w, y+h), radius=14, fill=REGION, outline=BORDER, width=2)
        text_at((x+20, y+17), title, 20, bold=True, max_width=w-40)
        if subtitle:
            text_at((x+20, y+45), subtitle, 17, fill=MUTED, max_width=w-40)
        if dashed:
            for sx in range(x+15, x+w-15, 18):
                draw.line((sx, y, min(sx+9, x+w-15), y), fill=LINE, width=2)
    for _, _, _, points, label, dashed in EDGES:
        for (x1, y1), (x2, y2) in zip(points, points[1:]):
            length = math.hypot(x2-x1, y2-y1)
            if dashed:
                for start in range(0, int(length), 16):
                    end = min(start+8, length)
                    draw.line((x1+(x2-x1)*start/length, y1+(y2-y1)*start/length, x1+(x2-x1)*end/length, y1+(y2-y1)*end/length), fill=LINE, width=2)
            else:
                draw.line((x1, y1, x2, y2), fill=LINE, width=2)
        x, y = points[-1]
        px, py = points[-2]
        angle = math.atan2(y-py, x-px)
        draw.polygon([(x, y), (x-11*math.cos(angle-.45), y-11*math.sin(angle-.45)), (x-11*math.cos(angle+.45), y-11*math.sin(angle+.45))], fill=LINE)
        a, b = max(zip(points, points[1:]), key=lambda pair: math.dist(*pair))
        cx, cy = (a[0]+b[0])/2, (a[1]+b[1])/2
        if a[0] == b[0]:
            cx += 75
        else:
            cy -= 17
        face = font(14)
        bounds = draw.textbbox((0, 0), label, font=face)
        tw, th = bounds[2]-bounds[0], bounds[3]-bounds[1]
        draw.rectangle((cx-tw/2-4, cy-th/2-4, cx+tw/2+4, cy+th/2+4), fill=REGION)
        draw.text((cx-tw/2, cy-th/2-2), label, fill=MUTED, font=face)
    for _, x, y, w, h, title, body in BOXES:
        draw.rounded_rectangle((x, y, x+w, y+h), radius=10, fill=SURFACE, outline=BORDER, width=2)
        title_font, body_font = font(21, True), font(17)
        title_width = draw.textlength(title, font=title_font)
        if title_width > w-24:
            raise ValueError(f'Architecture title exceeds box: {title}')
        draw.text((x+(w-title_width)/2, y+17), title, fill=INK, font=title_font)
        for index, text in enumerate(body.splitlines()):
            text_width = draw.textlength(text, font=body_font)
            if text_width > w-24 or 52+index*24+22 > h-10:
                raise ValueError(f'Architecture body exceeds box: {text}')
            draw.text((x+(w-text_width)/2, y+52+index*24), text, fill=MUTED, font=body_font)
    text_at((40, LEGEND_Y + 5), LEGEND, 17, fill=MUTED, max_width=1920)
    image.save(destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'docs' / 'architecture-access-control')
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    render_png(args.output_dir / 'architecture.png')
    render_xml(args.output_dir / 'architecture.xml')
    print('Rendered architecture.xml and architecture.png in', args.output_dir)
    if args.output_dir.resolve() == (ROOT / 'docs').resolve():
        for name in ('architecture.xml', 'architecture.png'):
            try:
                with (ROOT / name).open('xb') as output:
                    output.write((args.output_dir / name).read_bytes())
                print('Created root-level submission copy:', name)
            except FileExistsError:
                print('Preserved existing root-level submission copy:', name)


if __name__ == '__main__':
    main()
