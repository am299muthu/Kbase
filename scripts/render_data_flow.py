"""Generate readable PNG/SVG data flow from the current implementation."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from html import escape
import json
ROOT=Path(__file__).resolve().parent.parent
OUT=ROOT/'docs/data-flow'; OUT.mkdir(exist_ok=True)
flows=[
('01  INGESTION  |  file to searchable evidence',[
('Upload + approval','Maker uploads file + metadata\nGarage: staged original\nSQLite: document + request\nIndependent checker approves'),
('Local PDF extraction','pypdf: electronic text\npdfplumber: ruled tables\nDocling: layout + object crops\nScan regions: overlapping tiles'),
('Visual interpretation','Approved crops → OpenRouter\nVision primary → fallback*\nValidate structured response\nKeep source IDs + bounding boxes'),
('Chunks + embeddings','Store parsed content + chunks\nLocal PII masking → model check\nValidated text → embedding model\nGuardrail failure stops this run'),
('Index + publish','OpenSearch: text + vectors\nSQLite publishes generation\nOnly published chunks retrieved\nOld generation survives failure')]),
('02  QUESTION ANSWERING  |  question to cited answer',[
('Question + identity','Employee signs in\nFastAPI receives question\nMCP tools resolve folder grants\nSQLite is access authority'),
('Retrieve evidence','Question → text embedding\nOpenSearch BM25 + text k-NN\nRank fusion combines results\nFilter folders + live generations'),
('Validate source','SQL checks chunk manifests\nCheck retention + source lineage\nResolve cited evidence sections\nReject stale / unauthorized hits'),
('Generate + verify','Evidence → OpenRouter answer\nValidate citation IDs\nModel checks claim grounding\nApply output PII rules by access'),
('Release answer','Recheck session + permissions\nShow answer + source citations\nExpose permitted tool trace\nWithhold if required checks fail')]),
('03  DEBUGGING  |  parallel views of pipeline and search data',[
('Capture geometry','After parsing, before model calls\nSQLite audit: PDF layout snapshot\nObject ID + type + page + bbox\nNo PDF text in geometry snapshot'),
('Retain artifacts','Garage: original per request\nParsed JSON + visual crops\nCurated chunk JSON\nLegacy runs: crop geometry only'),
('PDF object inspector','Select PDF run + page\nLocal PDFium renders source\nSVG boxes overlay the page\nSelect box, layer, zoom; export'),
('Ingestion monitor','SQLite: request + live progress\nAudit: error + model excerpts\nMaker/checker retries unchanged\napproval without new review'),
('Search diagnostics','OpenSearch → Query Insights\nTop-query latency → Dashboards\nSlowlog → local query viewer\nSearch latency ≠ total chat time')])]
W,H=1800,1460
im=Image.new('RGB',(W,H),'#f7f9fc');d=ImageDraw.Draw(im);svg=[]
font='/System/Library/Fonts/Supplemental/Arial.ttf';bold='/System/Library/Fonts/Supplemental/Arial Bold.ttf'
def text(x,y,t,size=21,color='#14283c',strong=False):
 f=ImageFont.truetype(bold if strong else font,size);d.text((x,y),t,font=f,fill=color)
 svg.append(f'<text x="{x}" y="{y+size}" font-family="Arial,sans-serif" font-size="{size}" fill="{color}" font-weight="{700 if strong else 400}">{escape(t)}</text>')
def rect(x,y,w,h,fill,stroke=None):
 d.rectangle((x,y,x+w,y+h),fill=fill,outline=stroke,width=1)
 svg.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{fill}" stroke="{stroke or fill}"/>')
def arrow(x,y):
 d.line((x,y,x+22,y),fill='#52718c',width=2);d.polygon([(x+22,y),(x+15,y-5),(x+15,y+5)],fill='#52718c')
 svg.append(f'<path d="M{x},{y} h22 m-7,-5 l7,5 -7,5" fill="none" stroke="#52718c" stroke-width="2"/>')
text(45,30,'Knowledge solution · Data flow',38,strong=True)
text(45,86,'Implementation view · 30 October 2026 · Top lanes: left to right · Debug views: parallel',21,'#526578')
for row,(title,steps) in enumerate(flows):
 y=145+row*290
 text(45,y,title,24,strong=True)
 for i,(name,body) in enumerate(steps):
  x=45+i*348
  rect(x,y+49,320,201,'#ffffff','#d4dce5')
  rect(x,y+49,320,5,'#226a8d')
  text(x+15,y+69,name,23,strong=True)
  for j,line in enumerate(body.splitlines()):text(x+15,y+112+j*29,line,18)
  if row<2 and i<4:arrow(x+324,y+150)
rect(45,1032,1710,159,'#eaf0f5')
text(65,1050,'Where data lives',24,strong=True)
text(65,1095,'SQLite  →  identities, grants, approvals, progress, audit, chunk manifests and published-generation pointer',21)
text(65,1132,'Garage S3  →  files and artifacts          OpenSearch  →  searchable chunks, vectors and query insights',21)
text(45,1220,'External boundary & failure behavior',24,strong=True)
notes=[
'Only approved text/crops go to OpenRouter. Local rendering and the PDF inspector do not call models.',
'* Vision fallback is configured for HTTP 429 / 502 / 503 / 504. Text PII guardrails currently have no fallback.',
'Current pdf-hybrid profile: new writes require real text vectors; retained legacy generations remain readable.',
'Image embeddings are disabled in this profile. Original PDF inspection requires folder role + unmasked permission.'
]
for i,line in enumerate(notes):text(45,1262+i*34,line,20,'#526578')
im.save(OUT/'data-flow.png')
(OUT/'data-flow.svg').write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}"><rect width="100%" height="100%" fill="#f7f9fc"/>'+''.join(svg)+'</svg>')
(OUT/'flows.json').write_text(json.dumps(flows,indent=2))
