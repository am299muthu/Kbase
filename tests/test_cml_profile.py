"""The CML profile and launcher: the profile loads into valid settings, project settings win over it,
and switched-off services are not started."""
import importlib.util
import os
from pathlib import Path

import pytest
from dotenv import dotenv_values

from kb.config import ROOT, Settings, worker_python_flags

PROFILE = ROOT / "scripts" / "cml" / "cml.env"
ENDPOINTS = {"ANSWER_MODEL": "internal-llm", "ANSWER_MODEL_URL": "https://models.example/llm/v1",
             "VISION_MODEL": "granite-vision", "VISION_MODEL_URL": "https://models.example/granite/v1",
             "EMBEDDING_MODEL": "bge-small", "EMBEDDING_MODEL_URL": "https://models.example/bge/v1"}


def load_launcher():
    spec = importlib.util.spec_from_file_location("cml_app", ROOT / "scripts" / "cml" / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def clean_env():
    """An environment without any profile or endpoint setting, restored afterwards (load_profile writes
    straight into os.environ)."""
    saved = dict(os.environ)
    for key in set(dotenv_values(PROFILE)) | set(ENDPOINTS) | {"DOCLING_PYTHON", "LOCAL_GLINER_PYTHON"}:
        os.environ.pop(key, None)
    yield os.environ
    os.environ.clear()
    os.environ.update(saved)


def test_the_profile_loads_into_valid_settings(clean_env):
    clean_env.update({key: value or "" for key, value in {**dotenv_values(PROFILE), **ENDPOINTS}.items()})
    cfg = Settings(_env_file=None)
    assert (cfg.storage_backend, cfg.search_backend, cfg.model_api, cfg.sqlite_journal_mode) == ("s3", "opensearch", "openai", "delete")
    assert (cfg.embedding_backend, cfg.llm_provider) == ("openrouter", "openrouter")  # 'api' in the file
    assert cfg.vision_model_url == "https://models.example/granite/v1" and cfg.opensearch_vector_search


def test_project_settings_win_and_off_switches_a_service_off(clean_env):
    launcher = load_launcher()
    clean_env["OPENSEARCH_INDEX"] = "from-the-project"
    clean_env["KB_CML_OPENSEARCH_HOME"] = "off"
    clean_env["KB_CML_GARAGE_CONFIG"] = ""                        # blank in the project also wins
    launcher.load_profile()
    assert os.environ["OPENSEARCH_INDEX"] == "from-the-project"
    assert launcher.opensearch_plan() is None and launcher.garage_plan() is None
    assert Path(os.environ["HF_HOME"]).is_absolute()              # relative paths become absolute
    assert os.environ["S3_ENDPOINT_URL"] == "http://127.0.0.1:3900"  # an untouched profile value


def test_worker_flag_is_minus_i_unless_set(monkeypatch):
    monkeypatch.delenv("KB_WORKER_PYTHON_FLAG", raising=False)
    assert worker_python_flags() == ["-I"]
    monkeypatch.setenv("KB_WORKER_PYTHON_FLAG", "-P")
    assert worker_python_flags() == ["-P"]
    monkeypatch.setenv("KB_WORKER_PYTHON_FLAG", "-c print(1)")
    with pytest.raises(ValueError):
        worker_python_flags()
