# Settings for the offline Windows demo. Dot-sourced by setup.ps1 and run.ps1.
# Nothing here needs OpenSearch, Garage or an external model: search is SQLite, files are stored
# on disk, answers are extractive and embeddings are local hashes. These variables apply only to
# the current PowerShell session and override nothing in a private .env for other profiles.
$KbRoot = (Resolve-Path "$PSScriptRoot\..\..").Path

$env:PYTHONUTF8 = '1'                      # read and write files as UTF-8, as on macOS and Linux
$env:KB_RUNTIME_PROFILE = ''               # no named hybrid profile
$env:APP_MODE = 'demo'
$env:SEARCH_BACKEND = 'sqlite'
$env:STORAGE_BACKEND = 'local'
$env:LLM_PROVIDER = 'extractive'
$env:EMBEDDING_BACKEND = 'hash'
$env:EXTERNAL_MODELS_ENABLED = 'false'
$env:PDF_VISION_ENABLED = 'false'
$env:ALLOW_EXTERNAL_VISION_IMAGES = 'false'
$env:IMAGE_EMBEDDINGS_ENABLED = 'false'
$env:OPENSEARCH_VECTOR_SEARCH = 'false'
$env:OPENSEARCH_BM25_FALLBACK_INDEX = ''
$env:OPENSEARCH_HYBRID_FOLDERS = '[]'
$env:LOCAL_INGESTION_ENABLED = 'true'
$env:DATA_DIR = 'var/windows-local'
$env:DATABASE_URL = 'sqlite:///var/windows-local/kbase.db'
if (-not $env:DEMO_PASSWORD) { $env:DEMO_PASSWORD = 'local-demo-change-me' }
# Employees who may change processing time limits on the Processing settings page (comma-separated).
# Demo: E011 (Technology) stands in for the developer team; set real developer IDs here.
if (-not $env:KB_DEVELOPERS) { $env:KB_DEVELOPERS = 'E011' }
# Knowledge graph builder (Knowledge graph page). Demo: graphify and the ontology alignment run through the
# local Claude CLI, so document text goes to Claude. Production: KBASE_GRAPH_BACKEND=openai with
# OPENAI_BASE_URL / OPENAI_MODEL pointing at the internal model server.
if (-not $env:KBASE_GRAPH_BACKEND) { $env:KBASE_GRAPH_BACKEND = 'claude-cli' }
if (-not $env:KBASE_GRAPH_MODEL) { $env:KBASE_GRAPH_MODEL = 'sonnet' }
if (-not $env:KBASE_GRAPH_CONCURRENCY) { $env:KBASE_GRAPH_CONCURRENCY = '4' }   # files sent to the model at once
# Graph extraction: 'gliner' runs GLiNER2.5 on this machine (no LLM, nothing sent out); 'graphify' uses
# graphify and the Claude CLI above. GLiNER results are cached per chunk under var\...\graph-work.
if (-not $env:KBASE_GRAPH_EXTRACTOR) { $env:KBASE_GRAPH_EXTRACTOR = 'gliner' }
# Documents parsed at the same time. Each local Docling + Granite parse uses about 4 GB of GPU memory,
# so 2 is the most an 8 GB laptop GPU holds.
if (-not $env:INGEST_WORKERS) { $env:INGEST_WORKERS = '2' }

# PDFs and images are read by the local Docling install (layout, tables, OCR) and Granite Vision
# (chart data), run as a separate process by scripts/docling/worker.py. Models load from the local
# Hugging Face cache with downloads off. Without that install, PDFs fall back to pypdf text only.
$DoclingPython = (Resolve-Path "$KbRoot\..\docling\.venv\Scripts\python.exe" -ErrorAction SilentlyContinue)
if ($DoclingPython) {
    $env:DOCLING_PYTHON = $DoclingPython.Path
    $env:PDF_PARSER = 'docling_local'
    $env:IMAGE_PARSER = 'docling_local'
    if (-not $env:GRANITE_CHARTS) { $env:GRANITE_CHARTS = 'true' }     # chart pictures to data tables
    if (-not $env:GRANITE_MAX_CHARTS) { $env:GRANITE_MAX_CHARTS = '25' }
    if (-not $env:DOCLING_OCR) { $env:DOCLING_OCR = 'true' }           # scanned pages and image regions
} else {
    $env:PDF_PARSER = 'pypdf'
    $env:IMAGE_PARSER = 'pytesseract'
}

# Opt-in: hybrid search (BM25 + vectors, merged by rank) on a local OpenSearch, with a local
# embedding model run in the Docling environment. Nothing leaves the machine. Without the switch
# the demo stays on SQLite keyword search and needs neither Docker nor a model.
#   1. .\scripts\windows\opensearch.ps1          start OpenSearch (Docker); elasticsearch.ps1 for Elasticsearch
#   2. $env:KB_LOCAL_HYBRID = '1'                  in the same PowerShell window
#   3. python -m kb.cli reindex                    once, to copy existing documents into the index
#   4. .\scripts\windows\run.ps1
if ($env:KB_LOCAL_HYBRID -eq '1') {
    $env:SEARCH_BACKEND = 'opensearch'         # means a search server; SEARCH_ENGINE picks which one
    if (-not $env:SEARCH_ENGINE) { $env:SEARCH_ENGINE = 'opensearch' }    # 'elasticsearch' (8.x) also works
    $env:OPENSEARCH_VECTOR_SEARCH = 'true'
    $env:EMBEDDING_BACKEND = 'local'
    $env:OPENSEARCH_USERNAME = ''              # the local node has no accounts; send none
    $env:OPENSEARCH_PASSWORD = ''
    if (-not $env:OPENSEARCH_URL) { $env:OPENSEARCH_URL = 'http://127.0.0.1:9200' }
    if (-not $env:OPENSEARCH_INDEX) { $env:OPENSEARCH_INDEX = 'kbase-local-hybrid-v1' }
    if (-not $env:LOCAL_EMBEDDING_MODEL) { $env:LOCAL_EMBEDDING_MODEL = 'BAAI/bge-small-en-v1.5' }
    if (-not $env:EMBEDDING_DIMENSIONS) { $env:EMBEDDING_DIMENSIONS = '384' }
}

New-Item -ItemType Directory -Force (Join-Path $KbRoot 'var\windows-local') | Out-Null
$KbPython = Join-Path $KbRoot '.venv\Scripts\python.exe'
