"""Build KBase's knowledge graph with graphify, one graph per access folder.

Why per folder: graphify has no access control, so each folder's approved
documents get their own graph. Every node and link then inherits that folder's
permissions when imported (see import_graph.py).

Backend (which model reads the documents) comes from the environment:

  demo          KBASE_GRAPH_BACKEND=claude-cli   (default; uses the local `claude` CLI)
  real product  KBASE_GRAPH_BACKEND=openai
                OPENAI_BASE_URL=http://<internal-model-server>/v1
                OPENAI_MODEL=<model the server serves>

Only synthetic demo documents may go to an outside API.

Usage:
  python scripts/graph/build_graph.py                 # all folders
  python scripts/graph/build_graph.py --folder finance
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GRAPHIFY = ROOT / '.venv-graphify' / 'Scripts' / 'graphify.exe'
if not GRAPHIFY.exists():
    GRAPHIFY = ROOT / '.venv-graphify' / 'bin' / 'graphify'
SOURCE = ROOT / 'mockdata' / 'policies'
ACCESS = ROOT / 'mockdata' / 'access-control.json'
STAGE = ROOT / 'output' / 'graph-stage'
OUT = ROOT / 'output' / 'graph'

BACKEND = os.environ.get('KBASE_GRAPH_BACKEND', 'claude-cli')
DEMO_MODEL = os.environ.get('KBASE_GRAPH_MODEL', 'sonnet')


def folders() -> list[str]:
    return sorted({row['folder_id'] for row in json.loads(ACCESS.read_text(encoding='utf-8'))})


def folder_of(path: Path, known: list[str]) -> str | None:
    """Demo rule: a policy file belongs to the folder its name starts with."""
    name = path.stem
    matches = [f for f in known if name == f or name.startswith(f + '-')]
    return max(matches, key=len) if matches else None


def stage(folder: str, known: list[str]) -> Path:
    """Copy this folder's approved documents into a clean staging directory."""
    target = STAGE / folder
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for doc in sorted(SOURCE.glob('*.md')):
        if folder_of(doc, known) == folder:
            shutil.copy2(doc, target / doc.name)
    return target


def run(folder: str, staged: Path) -> Path:
    out = OUT / folder
    out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    cmd = [str(GRAPHIFY), 'extract', str(staged), '--out', str(out), '--no-gitignore', '--force']
    if BACKEND == 'claude-cli':
        env['GRAPHIFY_CLAUDE_CLI_MODEL'] = DEMO_MODEL
        cmd += ['--backend', 'claude-cli', '--max-concurrency', '1']
    else:
        cmd += ['--backend', BACKEND]
    env['GRAPHIFY_QUERY_LOG_DISABLE'] = '1'
    print(f'[{folder}] graphify extract with backend={BACKEND}', flush=True)
    subprocess.run(cmd, check=True, env=env, cwd=str(staged))
    graph = out / 'graphify-out' / 'graph.json'
    if not graph.exists():
        raise SystemExit(f'[{folder}] graphify finished but wrote no graph.json under {out}')
    return graph


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--folder', help='build only this folder')
    args = parser.parse_args()
    if not GRAPHIFY.exists():
        raise SystemExit('graphify not found: create .venv-graphify and pip install "graphifyy[pdf,office]"')
    known = folders()
    selected = [args.folder] if args.folder else known
    for folder in selected:
        if folder not in known:
            raise SystemExit(f'unknown folder {folder!r}; known: {known}')
        staged = stage(folder, known)
        if not any(staged.iterdir()):
            print(f'[{folder}] no approved documents, skipped')
            continue
        print(f'[{folder}] wrote {run(folder, staged).relative_to(ROOT)}')


if __name__ == '__main__':
    sys.exit(main())
