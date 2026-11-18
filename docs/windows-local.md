# Running KBase on Windows (offline demo)

Branch: `windows-local`. Everything runs on one Windows machine with no extra servers and no model calls.

| Part | Mac setup (`develop`) | Windows offline demo |
|---|---|---|
| Search | OpenSearch, BM25 + vectors | SQLite word search |
| Files | Garage S3 | local folder `var/windows-local/objects` |
| Controls | SQLite | SQLite `var/windows-local/kbase.db` |
| Embeddings | OpenRouter | local hashes (no model) |
| Answers | OpenRouter LLM | extractive evidence excerpts |
| PDF images | vision model | not described (text and tables only) |

Nothing leaves the machine. The Mac profiles, `.env` and their approvals are untouched: the Windows settings live only in `scripts/windows/env.ps1` and apply to the PowerShell session that runs them.

## First-time setup

Needs Python 3.11 (`py -3.11`). From the repository root in PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\setup.ps1
```

This creates `.venv`, installs `requirements.txt` plus `pdfplumber`, creates the database, seeds the 50 synthetic employees and stages the synthetic retail policy documents.

## Start

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\run.ps1
```

Open <http://127.0.0.1:8000>. Demo password: `local-demo-change-me` (set `$env:DEMO_PASSWORD` before starting to change it). The app also runs the ingestion worker.

## First walk-through

1. Sign in as **E002** (retail checker), open **Approvals** and approve the three staged documents. The worker indexes them within seconds.
2. Sign in as **E003** (retail maker) to upload more files (PDF, DOCX, DOC, XLSX, Markdown and the other supported formats). A checker approves each upload before it is indexed.
3. Ask a question from the **Home** banner or the **Ask knowledge** view. Answers are evidence excerpts with citations.

Other synthetic roles are listed in the main README (owner E001, delegate E004, and the same pattern for the other business roots).

## What changed for Windows

- `LOCAL_INGESTION_ENABLED` (default `false`): allows uploads and ingestion with SQLite search, local storage, hash embeddings and no external models. The settings validator refuses it in any other combination, so it cannot weaken the hybrid profiles.
- Uploads are written to a closed file inside a temporary folder. The previous `NamedTemporaryFile` was reopened while still open, which Windows does not allow (it failed with `PermissionError`).
- `PYTHONUTF8=1` makes every file read and write UTF-8, as on macOS and Linux.
- New UI: dark icon rail and navigation frame around a white workspace, red accents, and a **Home** dashboard (folder cards, ask banner, latest documents, approvals waiting). All existing views and controls are unchanged.

Note: `init` (run by `setup.ps1`) rewrites `mockdata/employees.json` from code, which drops its `pii_unmasked` field. That is existing behaviour of `kb.cli init`; restore the file afterwards with `git checkout -- mockdata/employees.json` if you do not want the change.

## Reset

Stop the app, then delete `var\windows-local` and run `setup.ps1` again.

## Limits

SQLite word search has no ranking model, so related questions can return the same top passage. Charts and images inside PDFs are not described without a vision model. For real hybrid search on Windows, OpenSearch for Windows and a local OpenAI-compatible model server can be added later.
