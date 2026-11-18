# Running KBase on CML: readiness check

Checked 2026-11-18 against the local Windows build and the current Cloudera AI (CML) documentation.
Goal: a proof of concept on CML that shows KBase working with OpenSearch (keyword plus vector
search, entity tags, the knowledge graph).

## Verdict

Every Python library KBase uses is a normal PyPI package with Linux wheels, so nothing blocks the
app itself. Three things need a decision or a change before it runs on CML:

1. **Where OpenSearch runs.** CML has no Cloudera-provided OpenSearch and no Docker inside sessions,
   so our Docker setup does not carry over. Either use an OpenSearch cluster the bank already runs,
   or start OpenSearch from its Linux tarball inside the KBase application.
2. **Model endpoints.** Every model but GLiNER is reached over the bank's own API endpoints, one per
   model, now set with `*_MODEL_URL` and `*_MODEL_KEY` (see "Models over the bank's own API
   endpoints"). Only GLiNER and Docling's layout and table models are uploaded (about 1.3 GB).
3. **Files and database.** Files go to Garage (S3-compatible), as in the project design; the controls
   database stays SQLite, with WAL switched off on the project's network disk
   (`SQLITE_JOURNAL_MODE=delete`).

The setup is ready in [scripts/cml](../scripts/cml/README.md): a settings profile, a one-time install
step, and the Application's launcher, which also starts Garage and OpenSearch inside the Application.

## What CML gives us

| Need | CML | Source |
|---|---|---|
| Serve a web app | An Application binds `127.0.0.1` on `CDSW_APP_PORT` (or `CDSW_READONLY_PORT`) and is reached at its own subdomain | [Engine environment variables](https://docs.cloudera.com/machine-learning/cloud/runtimes/topics/ml-engine-environment-variables-runtimes.html), [Applications](https://docs.cloudera.com/machine-learning/1.5.5/applications/ml-applications.pdf) |
| Python | ML Runtimes ship Python 3.10 to 3.13; the NVIDIA GPU edition uses CUDA 12.5 | [ML Runtimes](https://docs.cloudera.com/machine-learning/cloud/runtimes/ml-runtimes.pdf), [NVIDIA GPU edition](https://docs.cloudera.com/machine-learning/cloud/runtimes/topics/ml-runtimes-nvidia-gpu.html) |
| Packages offline | An internal PyPI mirror set with `PIP_INDEX_URL` (admin or project environment variable) | [AMPs in air-gapped environments](https://docs.cloudera.com/machine-learning/cloud/manage-amp/topics/ml-amps-airgapped-without-network.html), [Community answer](https://community.cloudera.com/t5/Support-Questions/CML-Python-Package-Installation-Security/m-p/381690) |
| Custom images | Allowed if built on a Cloudera runtime image | [Managing ML Runtimes](https://docs.cloudera.com/machine-learning/cloud/managing-runtimes/ml-managing-runtimes.pdf) |
| OpenSearch | Not offered by Cloudera (its search service is Solr based) | none found in the Cloudera documentation |
| Docker | Not available inside sessions or applications | confirm with the platform team |

## Our dependencies against CML

### KBase app (Python 3.11 here; 3.11 or 3.12 on CML)

| Package (installed here) | On CML |
|---|---|
| fastapi 0.162, uvicorn 0.54, pydantic 2.14, pydantic-settings, python-multipart | PyPI, works |
| SQLAlchemy 2.1, psycopg 3.3 | PyPI, works (Postgres optional) |
| opensearch-py 3.2 | PyPI, works. Supports URL, user, password and a CA bundle (`OPENSEARCH_URL`, `OPENSEARCH_USERNAME`, `OPENSEARCH_PASSWORD`, `OPENSEARCH_CA_CERTS`) |
| elasticsearch 8.19 | PyPI. Only needed if the bank's Elasticsearch is used instead (`SEARCH_ENGINE=elasticsearch`) |
| mcp 1.30 | PyPI, works. The MCP server starts as a child process with the same Python |
| pypdf, python-docx, openpyxl, xlrd, Pillow, defusedxml, PyYAML, httpx | PyPI, works |
| openai 2.54 | PyPI. Only for an internal model endpoint later; the demo answers extractively |
| boto3 | PyPI. Not needed: files are stored on the project disk (`STORAGE_BACKEND=local`) |
| opentelemetry-sdk, OTLP exporter | PyPI. Optional; leave telemetry off |

### Parsing, embeddings and GLiNER (a separate Python 3.14 environment here)

| Package (installed here) | On CML |
|---|---|
| docling 2.148.0 (installed from source here), docling-core 2.106, docling-ibm-models 4.0.4 | PyPI. Install `docling==2.148.0` |
| torch 2.15 + torchvision 0.29 (CUDA 13.2 build here) | PyPI Linux wheels. On a GPU runtime the wheel must match its CUDA 12.5 driver; on CPU use the CPU wheel |
| transformers 5.22, tokenizers, safetensors, huggingface_hub, accelerate | PyPI, works |
| gliner2 2.1.0 | PyPI, works (Apache 2.0) |
| rapidocr 3.10, opencv-python-headless, pypdfium2, numpy | PyPI, works (headless OpenCV needs no display) |
| bitsandbytes 0.50 | PyPI, needs a CUDA GPU. Only for Granite chart reading in 4-bit |

Python 3.14 is newer than any CML runtime. On CML this environment is rebuilt on the runtime's
Python (3.11 or 3.12). Simplest is one environment for everything: install both lists into the
runtime and point `DOCLING_PYTHON` and `LOCAL_GLINER_PYTHON` at the same interpreter.

Not needed on CML: graphify and the Claude CLI (the graph uses GLiNER, `KBASE_GRAPH_EXTRACTOR=gliner`).

### Models over the bank's own API endpoints

On CML every model except GLiNER is reached over an API, each with its own link. KBase takes one
OpenAI-compatible endpoint per model (all settings are in `.env.example`):

| Model | Endpoint | Key | Name |
|---|---|---|---|
| Answers | `ANSWER_MODEL_URL` | `ANSWER_MODEL_KEY` | `ANSWER_MODEL` |
| Question planner | `PLANNER_MODEL_URL` (blank: the answer model's) | `PLANNER_MODEL_KEY` | `PLANNER_MODEL` (blank: the answer model) |
| Grounding guardrail | `GUARDRAIL_MODEL_URL` (blank: the answer model's) | `GUARDRAIL_MODEL_KEY` | `GUARDRAIL_MODEL` |
| Charts and scans (Granite Vision) | `VISION_MODEL_URL` | `VISION_MODEL_KEY` | `VISION_MODEL` |
| Search vectors (bge or other) | `EMBEDDING_MODEL_URL` | `EMBEDDING_MODEL_KEY` | `EMBEDDING_MODEL` |
| Image vectors (optional) | `IMAGE_EMBEDDING_MODEL_URL` | `IMAGE_EMBEDDING_MODEL_KEY` | `IMAGE_EMBEDDING_MODEL` |

Set `MODEL_API=openai` so requests are plain OpenAI-compatible calls (no OpenRouter routing or price
fields). URLs must use HTTPS. A URL may be the base (`https://host/model/v1`) or the full
`/chat/completions` or `/embeddings` address. The endpoint URLs become part of what a checker
approves; keys never do.

With the models on endpoints, only GLiNER and Docling's own layout and table models still run on
CML itself, so the upload shrinks to about 1.3 GB and no GPU is needed for chart reading. Docling's
layout and table models are part of the Docling library and stay local by decision (2026-11-18), so
PDF parsing runs inside the KBase application on CML.

**Startup rule to know about.** With API models KBase runs in its hybrid mode, which refuses to start
unless documents are stored in S3-compatible storage (`STORAGE_BACKEND=s3`) and every indexed chunk
checks out. This is an existing governance rule, not new. Garage meets the storage part; until the
index is fully verified, `KB_CML_PARTIAL_CORPUS=1` starts it with a warning.

### Models in the local Hugging Face cache (for running models on CML instead)

| Model | Size | Used for | Needed for the demo |
|---|---|---|---|
| BAAI/bge-small-en-v1.5 | 129 MB | Embeddings for vector search | Yes |
| docling-project/docling-layout-heron | 164 MB | PDF layout | Yes, to parse PDFs on CML |
| docling-project/docling-models | 342 MB | Tables (TableFormer) | Yes, to parse PDFs on CML |
| docling-project/DocumentFigureClassifier-v2.5 | 33 MB | Telling charts from pictures | Yes, to parse PDFs on CML |
| fastino/gliner2.5-base-v1 | 748 MB | Entity scan and GLiNER graph | Yes, for main entities and the graph |
| ibm-granite/granite-vision-4.1-4b | 7.5 GB | Reading charts into tables | Optional; needs a GPU to be usable |
| ds4sd/docling-models, ds4sd/docling-layout-old | 670 MB | Older Docling models | Only if the new ones are missing |
| google/pix2struct-base | 1.1 GB | Not used by KBase | No |

Upload the needed folders into the project (for example `models/huggingface`), set `HF_HOME` to
it, and keep `HF_HUB_OFFLINE=1`. Without a GPU, turn chart reading off (`GRANITE_CHARTS=false`).
A 276-page annual report parsed in about 22 minutes on CPU and 10 on a GPU here, so index the demo
documents before the showcase.

## OpenSearch: two ways

| | A. The bank's OpenSearch cluster | B. OpenSearch inside the KBase application |
|---|---|---|
| What it shows | A real integration with a managed cluster | Everything running on CML alone |
| Needs | Network route from CML to the cluster, an account, the CA certificate, an index name | The OpenSearch 3.x Linux tarball (bundled Java) in the project, about 2 to 3 GB of memory in the application |
| KBase changes | None: settings only | A start script that runs OpenSearch on `127.0.0.1:9200` before KBase |
| Watch out for | Vector search needs the k-NN plugin (KBase uses the Lucene engine, which is part of the standard distribution) | Data on the project disk is slow; turn off memory-mapped storage (`node.store.allow_mmap: false`) since the container cannot raise `vm.max_map_count`; restart means a short warm-up |

Either way the index is rebuilt from KBase's own records with `python -m kb.cli reindex`.
OpenSearch Dashboards is optional; in option B it would need the application's second port.

## Changes KBase needs for CML

1. **A Linux start script** (the PowerShell scripts in `scripts/windows` hold all the settings today):
   set the environment, start OpenSearch if option B, then
   `python -m kb.cli serve --port $CDSW_APP_PORT`. The host already defaults to `127.0.0.1`.
2. **Database location.** SQLite runs in WAL mode (`kb/db.py`), which is not safe on a network file
   system like the project disk. For the demo either keep the database on the project disk with WAL
   switched off (a small setting to add), or use a Postgres database if the platform has one
   (`DATABASE_URL=postgresql+psycopg://...`).
3. **Settings for the CML profile** (models on the bank's endpoints): `SEARCH_BACKEND=opensearch`,
   `OPENSEARCH_VECTOR_SEARCH=true`, `MODEL_API=openai`, `EMBEDDING_BACKEND=api`, `LLM_PROVIDER=api`,
   `EXTERNAL_MODELS_ENABLED=true`, `GUARDRAILS_ENABLED=true`, `PDF_VISION_ENABLED=true`,
   `ALLOW_EXTERNAL_VISION_IMAGES=true`, the six `*_MODEL_URL` / `*_MODEL_KEY` / model names above,
   `EMBEDDING_DIMENSIONS` matching the embedding model, `KBASE_GRAPH_EXTRACTOR=gliner`, `DATA_DIR` on
   the project disk. Keys go in the CML project's environment variables, not in files.
4. **Ingestion workers.** On CPU keep `INGEST_WORKERS=1`. The new one-hour limit per document
   already runs on Linux (it uses process groups there).

## Questions for the platform team

1. Which CML version and runtimes do we have, and is there a GPU runtime for our project?
2. Is there an internal PyPI mirror, and does it carry torch, docling and gliner2?
3. Is there an OpenSearch cluster we can use (option A)? If not, may an application run OpenSearch
   from a tarball (option B), and how much memory can it have?
4. How do we bring in model files and the OpenSearch tarball: upload into the project, or an
   internal artifact store?
5. Is a Postgres database available to CML projects, or should the demo stay on SQLite?
6. May an Application run Garage from its Linux binary (the default in `scripts/cml`), or is there a
   Garage server we should use instead?
7. For each model endpoint: the base URL, the model name it expects, how keys are issued, and whether
   the embedding model's vector size is fixed (KBase needs it in `EMBEDDING_DIMENSIONS`).
8. Is the application reachable by everyone at the showcase, and does CML sign-in sit in front of it?
