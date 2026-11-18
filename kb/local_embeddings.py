"""Local text embeddings through one shared, long-lived worker process.

KBase's own environment has no torch, so the model runs in another Python (LOCAL_EMBEDDING_PYTHON,
else DOCLING_PYTHON). The worker listens on 127.0.0.1 only (LOCAL_EMBEDDING_PORT, default 8765), so
the web server, the helper it starts for each question and the command line all use the same loaded
model; loading it takes about 20 seconds and must not happen per question. The web server starts
the worker at startup and stops it at shutdown. A worker started by anything else stops by itself
after an hour without requests. Nothing leaves the machine: the worker reads the model from the
local Hugging Face cache with downloads switched off.
"""
import json
import os
import socket
import subprocess
import threading
from pathlib import Path

from kb.config import ROOT, settings

WORKER = ROOT / 'scripts' / 'embeddings' / 'worker.py'
# BGE English models are trained with this instruction on the question side only.
_BGE_QUERY_PREFIX = 'Represent this sentence for searching relevant passages: '
_START_SECONDS, _CALL_SECONDS, _IDLE_SECONDS = 240, 180, 3600
_lock = threading.Lock()
_started: subprocess.Popen | None = None  # the worker this process started, if any


class LocalEmbeddingError(RuntimeError):
    pass


def _port() -> int:
    return int(os.environ.get('LOCAL_EMBEDDING_PORT', '8765'))


def _python() -> str:
    path = os.environ.get('LOCAL_EMBEDDING_PYTHON') or os.environ.get('DOCLING_PYTHON') or ''
    if not path or not Path(path).exists():
        raise LocalEmbeddingError('Local embeddings need LOCAL_EMBEDDING_PYTHON or DOCLING_PYTHON: a Python with torch and transformers.')
    return path


def _ask(request: dict) -> dict | None:
    """One request to the worker. None means no worker is listening."""
    try:
        connection = socket.create_connection(('127.0.0.1', _port()), timeout=2)
    except OSError:
        return None
    with connection:
        connection.settimeout(_CALL_SECONDS)
        connection.sendall((json.dumps(request) + '\n').encode('utf-8'))
        with connection.makefile('r', encoding='utf-8') as reply:
            line = reply.readline()
    if not line:
        raise LocalEmbeddingError('The local embedding worker closed the connection; see embedding-worker.log in the data folder.')
    return json.loads(line)


def _start(idle_seconds: int) -> None:
    global _started
    cfg = settings()
    env = {**os.environ, 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'PYTHONIOENCODING': 'utf-8',
           'TOKENIZERS_PARALLELISM': 'false'}
    log = open(cfg.data_dir / 'embedding-worker.log', 'ab')
    process = subprocess.Popen([_python(), str(WORKER), cfg.local_embedding_model, str(_port()), str(idle_seconds)],
                               # With no idle limit the worker lives as long as this process: it stops
                               # when the pipe closes, even if this process is killed.
                               stdin=subprocess.PIPE if idle_seconds == 0 else subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log, text=True, encoding='utf-8', env=env)
    timer = threading.Timer(_START_SECONDS, process.kill)
    timer.start()
    try:
        line = process.stdout.readline()
    finally:
        timer.cancel()
    ready = json.loads(line) if line else {}
    if not ready.get('ready'):
        process.kill()
        if 'is not free' in ready.get('error', ''):
            return  # another process started a worker at the same moment; use that one
        raise LocalEmbeddingError(f"The local embedding model did not load: {ready.get('error', 'the worker stopped; see embedding-worker.log')}")
    if ready.get('dimensions') != cfg.embedding_dimensions:
        process.kill()
        raise LocalEmbeddingError(f"{cfg.local_embedding_model} gives {ready.get('dimensions')} dimensions; EMBEDDING_DIMENSIONS is {cfg.embedding_dimensions}.")
    _started = process


def embed(texts: list[str], kind: str = 'passage', idle_seconds: int = _IDLE_SECONDS) -> list[list[float]]:
    """Unit-length vectors for `texts`. `kind` is 'query' for a search question, else 'passage'."""
    if not texts:
        return []
    cfg = settings()
    name = cfg.local_embedding_model.lower()
    prefix = _BGE_QUERY_PREFIX if kind == 'query' and 'bge' in name and '-en' in name else ''
    request = {'texts': texts, 'prefix': prefix, 'model': cfg.local_embedding_model}
    try:
        answer = _ask(request)
        if answer is None:
            with _lock:
                answer = _ask(request)  # another thread may have started it while this one waited
                if answer is None:
                    _start(idle_seconds)
                    answer = _ask(request)
    except (OSError, ValueError) as exc:
        raise LocalEmbeddingError(f'The local embedding worker failed ({type(exc).__name__}).') from exc
    if answer is None:
        raise LocalEmbeddingError('The local embedding worker did not start; see embedding-worker.log in the data folder.')
    vectors = answer.get('vectors')
    if (not isinstance(vectors, list) or len(vectors) != len(texts)
            or any(not isinstance(v, list) or len(v) != cfg.embedding_dimensions for v in vectors)):
        raise LocalEmbeddingError(f"The local embedding worker returned an invalid result: {str(answer.get('error', ''))[:200]}")
    return vectors


def warm() -> None:
    """Called by the web server at startup: load the model now, and keep it until the server stops."""
    embed(['warm up'], idle_seconds=0)


def stop() -> None:
    """Stop the worker if this process started it."""
    global _started
    if _started is not None and _started.poll() is None:
        _started.kill()
    _started = None
