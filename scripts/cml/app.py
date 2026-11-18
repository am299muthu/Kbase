"""KBase as a CML Application.

Set this file as the Application's script (Python 3.11 or 3.12 runtime). It reads scripts/cml/cml.env
(the project's environment variables win), starts Garage and OpenSearch inside the Application when
they are configured, creates the database on the first start, then serves KBase on CDSW_APP_PORT.

    python scripts/cml/app.py            # what the Application runs
    python scripts/cml/app.py --check    # report the settings and what would start; starts nothing
"""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
PROFILE = Path(__file__).with_name("cml.env")
PATH_KEYS = ("HF_HOME", "KB_CML_GARAGE_CONFIG", "KB_CML_GARAGE_BIN", "KB_CML_OPENSEARCH_HOME",
             "S3_CA_BUNDLE", "OPENSEARCH_CA_CERTS", "DATA_DIR")
# Model role -> the Settings field that names its model.
MODEL_NAMES = {"answer": "openrouter_model", "planner": "planner_model", "guardrail": "openrouter_guardrail_model",
               "vision": "openrouter_vision_model", "embedding": "openrouter_embedding_model"}


def say(message: str) -> None:
    print(f"[kbase-cml] {message}", flush=True)


def load_profile() -> None:
    """cml.env into the environment, without overriding what the project already sets."""
    from dotenv import dotenv_values

    for key, value in dotenv_values(PROFILE).items():
        if value is not None and key not in os.environ:  # a project setting wins, even a blank one
            os.environ[key] = value
    for key in PATH_KEYS:
        value = os.environ.get(key, "").strip()  # blank or spaces: not set
        if value and value.lower() not in {"off", "none", "false", "0"} and not Path(value).is_absolute():
            value = str((ROOT / value).resolve())
        os.environ[key] = value
    # One runtime Python runs KBase, the Docling worker and the GLiNER worker.
    os.environ.setdefault("DOCLING_PYTHON", sys.executable)
    os.environ.setdefault("LOCAL_GLINER_PYTHON", sys.executable)
    os.environ.setdefault("PYTHONUTF8", "1")


def data_dir() -> Path:
    path = Path(os.environ.get("DATA_DIR") or ROOT / "var" / "cml")
    path.mkdir(parents=True, exist_ok=True)
    return path


def port_open(host: str, port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex((host, port)) == 0


def wait_until(check, seconds: int, what: str) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            say(f"{what} is up")
            return
        time.sleep(2)
    raise SystemExit(f"{what} did not start within {seconds} s; see its log in {data_dir()}")


def switched_off(key: str) -> bool:
    """Blank, spaces, or 'off': app.py does not start this service."""
    return os.environ.get(key, "").strip().lower() in {"", "off", "none", "false", "0"}


def garage_plan() -> dict | None:
    if switched_off("KB_CML_GARAGE_CONFIG"):
        return None
    config = os.environ["KB_CML_GARAGE_CONFIG"]
    endpoint = urlsplit(os.environ.get("S3_ENDPOINT_URL", ""))
    binary = os.environ.get("KB_CML_GARAGE_BIN") or shutil.which("garage") or ""
    return {"config": config, "binary": binary, "host": endpoint.hostname or "127.0.0.1", "port": endpoint.port or 3900}


def opensearch_plan() -> dict | None:
    if switched_off("KB_CML_OPENSEARCH_HOME"):
        return None
    home = os.environ["KB_CML_OPENSEARCH_HOME"]
    url = urlsplit(os.environ.get("OPENSEARCH_URL", "http://127.0.0.1:9200"))
    return {"home": home, "binary": str(Path(home) / "bin" / "opensearch"), "url": url.geturl(),
            "host": url.hostname or "127.0.0.1", "port": url.port or 9200,
            "heap": os.environ.get("KB_CML_OPENSEARCH_HEAP", "1g")}


def start_garage(plan: dict, children: list) -> None:
    if port_open(plan["host"], plan["port"]):
        say(f"Garage already answers on {plan['host']}:{plan['port']}")
        return
    if not Path(plan["config"]).is_file() or not plan["binary"] or not Path(plan["binary"]).is_file():
        raise SystemExit("Garage is configured but its config or binary is missing; run: python scripts/cml/app.py --check")
    log = open(data_dir() / "garage.log", "ab")
    say("starting Garage")
    children.append(subprocess.Popen([plan["binary"], "--config", plan["config"], "server", "--single-node"],
                                     stdout=log, stderr=log, start_new_session=True))
    wait_until(lambda: port_open(plan["host"], plan["port"]), 60, "Garage")


def start_opensearch(plan: dict, children: list) -> None:
    def healthy() -> bool:
        try:
            with urllib.request.urlopen(plan["url"], timeout=3) as response:
                return response.status == 200
        except OSError:
            return False

    if healthy():
        say(f"OpenSearch already answers at {plan['url']}")
        return
    if not Path(plan["binary"]).is_file():
        raise SystemExit("OpenSearch is configured but bin/opensearch is missing; run: python scripts/cml/app.py --check")
    folder = data_dir() / "opensearch"
    env = {**os.environ, "OPENSEARCH_JAVA_OPTS": f"-Xms{plan['heap']} -Xmx{plan['heap']}"}
    command = [plan["binary"], "-Ecluster.name=kbase-cml", "-Ediscovery.type=single-node",
               f"-Enetwork.host={plan['host']}", f"-Ehttp.port={plan['port']}",
               "-Eplugins.security.disabled=true",      # loopback only, inside the Application
               "-Enode.store.allow_mmap=false",         # a container cannot raise vm.max_map_count
               f"-Epath.data={folder / 'data'}", f"-Epath.logs={folder / 'logs'}"]
    log = open(data_dir() / "opensearch.log", "ab")
    say("starting OpenSearch")
    children.append(subprocess.Popen(command, stdout=log, stderr=log, env=env, start_new_session=True))
    wait_until(healthy, 240, "OpenSearch")


def first_start() -> None:
    """Create the database, the sample employees and the search index once."""
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("sqlite:///"):
        path = Path(url.removeprefix("sqlite:///"))
        if (path if path.is_absolute() else ROOT / path).exists():
            return
    elif os.environ.get("KB_CML_INITIALISED") == "1":  # Postgres: set this once init has run
        return
    say("first start: creating the database and the search index")
    subprocess.run([sys.executable, "-m", "kb.cli", "init"], cwd=ROOT, check=True)


def check() -> int:
    """Settings as KBase sees them, and what app.py would start. Secrets are not printed."""
    sys.path.insert(0, str(ROOT))
    problems = []
    try:
        from kb.config import settings
        cfg = settings()
        say(f"settings load: mode {cfg.app_mode}, storage {cfg.storage_backend}, search {cfg.search_engine} at "
            f"{cfg.opensearch_url} (index {cfg.opensearch_index}), database {cfg.database_url.split('://')[0]}, "
            f"journal {cfg.sqlite_journal_mode}, model API {cfg.model_api}")
        for role, field in MODEL_NAMES.items():
            url = getattr(cfg, f"{role}_model_url")
            key = "set" if getattr(cfg, f"{role}_model_key") else "not set"
            say(f"  {role:<10} model {getattr(cfg, field) or '(default)'} at {url or '(fallback)'}; key {key}")
            if role in ("answer", "vision", "embedding") and not url:
                problems.append(f"{role.upper()}_MODEL_URL is blank")
    except Exception as exc:  # noqa: BLE001 - report the settings problem as it is
        problems.append(f"settings do not load: {exc}")
    for label, plan in (("Garage", garage_plan()), ("OpenSearch", opensearch_plan())):
        if plan is None:
            say(f"{label}: not started by app.py (an existing service is used)")
            continue
        target = plan.get("binary")
        say(f"{label}: would start {target or '(no binary)'} on {plan['host']}:{plan['port']}")
        if not target or not Path(target).is_file():
            problems.append(f"{label} binary not found: {target or '(none)'}")
        if label == "Garage" and not Path(plan["config"]).is_file():
            problems.append(f"Garage config not found: {plan['config']}")
    if os.environ.get("STORAGE_BACKEND") == "s3":
        for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
            if not os.environ.get(key):
                problems.append(f"{key} is not set (the Garage access key)")
    say(f"port: {os.environ.get('CDSW_APP_PORT') or '(CDSW_APP_PORT not set; 8000 outside CML)'}")
    for problem in problems:
        say(f"TO FIX: {problem}")
    say("ready" if not problems else f"{len(problems)} thing(s) to fix before starting")
    return 1 if problems else 0


def main() -> int:
    load_profile()
    if "--check" in sys.argv:
        return check()
    children: list[subprocess.Popen] = []

    def stop(*_):
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                child.kill()

    signal.signal(signal.SIGTERM, lambda *_: (stop(), sys.exit(0)))
    try:
        if (plan := garage_plan()) is not None:
            start_garage(plan, children)
        if (plan := opensearch_plan()) is not None:
            start_opensearch(plan, children)
        first_start()
        port = os.environ.get("CDSW_APP_PORT") or os.environ.get("CDSW_READONLY_PORT") or "8000"
        command = [sys.executable, "-m", "kb.cli", "serve", "--port", port]
        if os.environ.get("KB_CML_PARTIAL_CORPUS") == "1":
            command.append("--partial-corpus")
        say(f"serving KBase on 127.0.0.1:{port}")
        server = subprocess.Popen(command, cwd=ROOT)
        children.append(server)
        return server.wait()
    finally:
        stop()


if __name__ == "__main__":
    sys.exit(main())
