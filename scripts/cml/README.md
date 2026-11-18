# KBase on CML

How to run the KBase proof of concept as a CML Application: OpenSearch for keyword and vector search,
Garage (S3-compatible) for files, SQLite for the controls database, Docling and GLiNER running in the
Application, and every other model on the bank's own endpoints. Background and open questions:
[docs/cml-readiness.md](../../docs/cml-readiness.md).

| File | What it is |
|---|---|
| `cml.env` | The settings profile. The project's environment variables win over it; keys go there, never in the file. |
| `app.py` | The Application's script: starts Garage and OpenSearch inside the Application if configured, creates the database on the first start, then serves KBase on `CDSW_APP_PORT`. `--check` reports without starting anything. |
| `install.py` | One-time setup in a session: installs the libraries, places the OCR models, checks everything. |
| `requirements-cml.txt` | Docling, GLiNER and torch, at the versions tested locally. |
| `garage.toml.example` | Garage config for running it inside the Application. |

## 1. Bring the code and files into the project

Create a CML project from the KBase repository (or upload the folder). Then upload into the project,
under the KBase folder:

| Path in the project | What | Size |
|---|---|---|
| `models/huggingface/hub/models--fastino--gliner2.5-base-v1` | GLiNER2.5 | 748 MB |
| `models/huggingface/hub/models--docling-project--docling-layout-heron` | Docling layout | 164 MB |
| `models/huggingface/hub/models--docling-project--docling-models` | Docling tables | 342 MB |
| `models/huggingface/hub/models--docling-project--DocumentFigureClassifier-v2.5` | Chart or picture | 33 MB |
| `models/rapidocr/*.onnx` | OCR models (from the rapidocr package's `models` folder) | 62 MB |
| `tools/opensearch/` | OpenSearch 3.x Linux tarball, unpacked (the full distribution: it has the k-NN plugin) | about 900 MB |
| `tools/garage` | Garage 2.x Linux binary, executable | about 30 MB |

The model folders are copied as they are from a machine's Hugging Face cache
(`%USERPROFILE%\.cache\huggingface\hub` on Windows). Skip `tools/opensearch` if the bank's OpenSearch
cluster is used, and `tools/garage` if a running Garage server is used (see the comments in `cml.env`).

## 2. Set the project's environment variables

In the project's settings, set at least:

| Variable | Value |
|---|---|
| `PIP_INDEX_URL` | The internal package mirror (if the platform team has not set it for everyone) |
| `ANSWER_MODEL`, `ANSWER_MODEL_URL`, `ANSWER_MODEL_KEY` | The answer model's name, link and key |
| `VISION_MODEL`, `VISION_MODEL_URL`, `VISION_MODEL_KEY` | The vision model (charts and scans) |
| `EMBEDDING_MODEL`, `EMBEDDING_MODEL_URL`, `EMBEDDING_MODEL_KEY`, `EMBEDDING_DIMENSIONS` | The embedding model and its vector size |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | The Garage access key for KBase (step 4) |
| `DEMO_PASSWORD` | A password for the sample accounts, instead of the default |

Optional: `PLANNER_MODEL*` and `GUARDRAIL_MODEL*` (blank uses the answer model), and for the bank's
OpenSearch: `OPENSEARCH_URL`, `OPENSEARCH_USERNAME`, `OPENSEARCH_PASSWORD`, `OPENSEARCH_CA_CERTS`,
with `KB_CML_OPENSEARCH_HOME=off`. Likewise `KB_CML_GARAGE_CONFIG=off` for a Garage server that is already running.

## 3. Install, in a session

Start a session on a Python 3.11 or 3.12 runtime, open its terminal, and in the KBase folder run:

```bash
python scripts/cml/install.py
```

It installs the libraries, copies the OCR models into place and ends with a checklist. Fix anything
it marks TO FIX, then check again with `python scripts/cml/install.py --check`.

## 4. Garage, once

Copy `scripts/cml/garage.toml.example` to `var/garage/config.toml`, set a new `rpc_secret`
(`openssl rand -hex 32`) and check the two paths. Then, in the session terminal:

```bash
tools/garage --config var/garage/config.toml server --single-node &
tools/garage --config var/garage/config.toml bucket create abc-knowledge
tools/garage --config var/garage/config.toml key create kbase-app
tools/garage --config var/garage/config.toml bucket allow --read --write abc-knowledge --key kbase-app
```

`key create` prints a key ID and a secret: put them in `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`
(step 2). KBase's key can read and write but does not own the bucket. Stop that Garage afterwards
(`kill %1`); the Application starts its own from the same config and data.

## 5. Create the Application

| Field | Value |
|---|---|
| Script | `scripts/cml/app.py` (with the KBase folder's path if it is not the project root) |
| Runtime | The same Python 3.11 or 3.12 runtime as step 3 |
| Resources | At least 4 vCPU and 12 GB memory (OpenSearch 1 GB heap, Docling and GLiNER on CPU) |

The first start creates the database, the sample employees and the search index. The log shows each
step (`[kbase-cml] ...`); Garage and OpenSearch logs are in `var/cml/`.

## 6. Load documents and show the integration

1. Sign in as a sample maker, upload documents, and approve them as a checker. Each document is parsed
   by Docling, its charts read by the vision model, its chunks embedded by the embedding model, and
   indexed in OpenSearch with keyword and vector fields. Files land in Garage.
2. Ask questions: answers come from OpenSearch's keyword and vector results, merged by rank.
3. Knowledge graph page: run the entity scan (GLiNER, in the Application). Its entity tags are written
   to the same OpenSearch index and raise matching passages in answers.

From a session, `python -m kb.cli reindex` rebuilds the index from KBase's own records, and
`python -m kb.cli entities --folder <id>` runs the entity scan without the page.
