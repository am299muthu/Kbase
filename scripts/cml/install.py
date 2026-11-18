"""One-time setup for KBase in a CML session (Python 3.11 or 3.12 runtime).

    python scripts/cml/install.py            # install the libraries, place the OCR models, then check
    python scripts/cml/install.py --check    # only check what is in place

Libraries come from the project's package index (PIP_INDEX_URL, set by the platform team). Nothing is
downloaded from Hugging Face: the model folders are uploaded into the project (see scripts/cml/README.md).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
# Model folders needed on CML (every other model is reached over its endpoint).
MODELS = {
    "models--fastino--gliner2.5-base-v1": "GLiNER2.5: entity scan and knowledge graph",
    "models--docling-project--docling-layout-heron": "Docling: PDF layout",
    "models--docling-project--docling-models": "Docling: tables",
    "models--docling-project--DocumentFigureClassifier-v2.5": "Docling: charts versus pictures",
}


def say(message: str) -> None:
    print(f"[kbase-cml] {message}", flush=True)


def install() -> None:
    if sys.version_info[:2] not in {(3, 11), (3, 12)}:
        say(f"warning: tested with Python 3.11 and 3.12; this runtime is {sys.version.split()[0]}")
    say("installing the KBase, Docling and GLiNER libraries")
    subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt"),
                    "-r", str(HERE / "requirements-cml.txt")], check=True)


def place_ocr_models() -> None:
    """RapidOCR keeps its models inside its package folder and downloads them on first use. Without
    internet, copy the uploaded models/rapidocr files there."""
    source = ROOT / "models" / "rapidocr"
    if not source.is_dir():
        return
    try:
        import rapidocr
    except ImportError:
        return
    target = Path(rapidocr.__file__).parent / "models"
    target.mkdir(exist_ok=True)
    copied = 0
    for file in source.iterdir():
        if file.suffix in {".onnx", ".pth"} and not (target / file.name).exists():
            shutil.copy2(file, target / file.name)
            copied += 1
    say(f"OCR models: {copied} copied into {target}")


def check() -> int:
    sys.path.insert(0, str(HERE))
    import app  # reads the profile exactly as the Application will

    app.load_profile()
    problems = []
    for module in ("fastapi", "opensearchpy", "boto3", "docling", "gliner2", "torch", "transformers", "rapidocr"):
        try:
            __import__(module)
        except ImportError:
            problems.append(f"library not installed: {module}")
    hub = Path(os.environ.get("HF_HOME") or ROOT / "models" / "huggingface") / "hub"
    for folder, use in MODELS.items():
        if (hub / folder).is_dir():
            say(f"model ok: {folder} ({use})")
        else:
            problems.append(f"model missing: {hub / folder} ({use})")
    try:
        import rapidocr
        ocr_on = os.environ.get("DOCLING_OCR", "true").lower() in {"1", "true", "yes", "on"}
        if ocr_on and not any((Path(rapidocr.__file__).parent / "models").glob("*.onnx")):
            problems.append("OCR models missing: upload them to models/rapidocr and rerun install.py, or set DOCLING_OCR=false")
    except ImportError:
        pass
    for problem in problems:
        say(f"TO FIX: {problem}")
    return app.check() or (1 if problems else 0)


if __name__ == "__main__":
    if "--check" not in sys.argv:
        install()
        place_ocr_models()
    sys.exit(check())
