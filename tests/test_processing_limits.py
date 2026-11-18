"""Processing time limits: which value wins, validation, and what each run records."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kb import processing_limits as limits
from kb.db import Base, ProcessingSetting


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[ProcessingSetting.__table__])
    session = sessionmaker(engine)()
    yield session
    session.close()


def test_run_values_beat_environment_which_beats_default(monkeypatch):
    monkeypatch.delenv("GRANITE_SECONDS_PER_CHART", raising=False)
    assert limits.processing_limit("GRANITE_SECONDS_PER_CHART") == 90
    monkeypatch.setenv("GRANITE_SECONDS_PER_CHART", "45")
    assert limits.processing_limit("GRANITE_SECONDS_PER_CHART") == 45
    with limits.using_limits({"GRANITE_SECONDS_PER_CHART": 200}):
        assert limits.processing_limit("GRANITE_SECONDS_PER_CHART") == 200
    assert limits.processing_limit("GRANITE_SECONDS_PER_CHART") == 45
    monkeypatch.setenv("GRANITE_SECONDS_PER_CHART", "not a number")
    assert limits.processing_limit("GRANITE_SECONDS_PER_CHART") == 90


def test_save_validates_records_changes_and_clears(db, monkeypatch):
    monkeypatch.delenv("DOCLING_TIMEOUT_SECONDS", raising=False)
    with pytest.raises(ValueError, match="from 10 to 1800"):
        limits.save(db, {"GRANITE_SECONDS_PER_CHART": 5}, "E011")
    with pytest.raises(ValueError, match="Unknown setting"):
        limits.save(db, {"NOT_A_LIMIT": 30}, "E011")
    with pytest.raises(ValueError):
        limits.save(db, {"GRANITE_SECONDS_PER_CHART": True}, "E011")
    assert db.query(ProcessingSetting).count() == 0  # nothing saved when any value is invalid

    changes = limits.save(db, {"GRANITE_SECONDS_PER_CHART": 150, "DOCLING_TIMEOUT_SECONDS": 3600}, "E011")
    assert changes == [{"key": "GRANITE_SECONDS_PER_CHART", "before": 90, "after": 150}]  # 3600 is already the value
    row = {r["key"]: r for r in limits.describe(db)}["GRANITE_SECONDS_PER_CHART"]
    assert row["value"] == 150 and row["source"] == "saved" and row["updated_by"] == "E011"

    assert limits.save(db, {"GRANITE_SECONDS_PER_CHART": None}, "E011") == [
        {"key": "GRANITE_SECONDS_PER_CHART", "before": 150, "after": 90}]
    assert {r["key"]: r for r in limits.describe(db)}["GRANITE_SECONDS_PER_CHART"]["source"] == "default"


def test_developers_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("KB_DEVELOPERS", " E011, E012 ,")
    assert limits.is_developer("E011") and limits.is_developer("E012") and not limits.is_developer("E001")
    monkeypatch.delenv("KB_DEVELOPERS")
    assert not limits.is_developer("E011")


def test_each_parse_records_the_limits_it_ran_with(tmp_path):
    from kb.parsers import parse_document

    source = tmp_path / "note.txt"
    source.write_text("A short note.", encoding="utf-8")
    with limits.using_limits({**limits.current_limits(), "OCR_TIMEOUT_SECONDS": 30}):
        parsed = parse_document(source, tmp_path / "assets")
    assert parsed.extraction_evidence["time_limits"]["OCR_TIMEOUT_SECONDS"] == 30
    assert set(parsed.extraction_evidence["time_limits"]) == set(limits.LIMITS)
