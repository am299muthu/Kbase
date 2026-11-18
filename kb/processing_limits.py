"""Processing time limits: their defaults and bounds, and values saved from the settings page.

Each limit resolves in this order: a value a developer saved on the Processing settings page, then
the environment variable of the same name, then the default below. The pipeline loads the values
once per processing run and passes them down in a context variable, so the parsers never read the
database themselves. Each run records the values it used in its extraction evidence.

Time limits only say how long to wait. They do not change what data is used or where it goes, so
they are not part of the approved processing scope: changing one does not void approvals.
Only employees listed in KB_DEVELOPERS (comma-separated employee IDs) may change them.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar

# key: (label, help, default seconds, minimum, maximum)
LIMITS: dict[str, tuple[str, str, int, int, int]] = {
    "GRANITE_SECONDS_PER_CHART": (
        "Granite: chart table",
        "Time Granite may spend writing one chart as a data table. If it runs out, it writes a summary instead.",
        90, 10, 1800),
    "GRANITE_SUMMARY_SECONDS": (
        "Granite: chart summary",
        "Time for the written summary that replaces a table Granite could not finish.",
        60, 10, 600),
    "DOCLING_TIMEOUT_SECONDS": (
        "Docling: whole document",
        "Time for the whole Docling and Granite run on one PDF or image. A longer document needs more.",
        3600, 60, 21600),
    "OCR_TIMEOUT_SECONDS": (
        "Tesseract OCR: one image",
        "Time for reading the text in one image when the Tesseract parser is used instead of Docling.",
        60, 5, 1800),
    "DOC_CONVERSION_TIMEOUT_SECONDS": (
        "Word .doc conversion",
        "Time for converting an old Word .doc file before it is read.",
        120, 10, 1800),
    "GRAPH_EXTRACTION_SECONDS": (
        "Knowledge graph: graphify extraction",
        "Time for graphify to read one folder's chosen documents. A large report can need 30 minutes or more.",
        3600, 300, 21600),
    "GRAPH_ALIGNMENT_SECONDS": (
        "Knowledge graph: ontology alignment",
        "Time for one call that types entities and links against the ontology.",
        600, 60, 3600),
}

_current: ContextVar[dict | None] = ContextVar("kb_processing_limits", default=None)


def _from_environment(key: str) -> int | None:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def processing_limit(key: str) -> int:
    """The limit for this run: the run's loaded values, else the environment, else the default."""
    values = _current.get()
    if values and key in values:
        return int(values[key])
    environment = _from_environment(key)
    return environment if environment is not None else LIMITS[key][2]


def current_limits() -> dict[str, int]:
    return {key: processing_limit(key) for key in LIMITS}


@contextmanager
def using_limits(values: dict[str, int]):
    token = _current.set(dict(values))
    try:
        yield
    finally:
        _current.reset(token)


def _saved(db) -> dict:
    from kb.db import ProcessingSetting

    return {row.key: row for row in db.query(ProcessingSetting).all() if row.key in LIMITS}


def load_limits() -> dict[str, int]:
    """Saved values over the environment over defaults, for one processing run."""
    from kb.db import db_session

    with db_session() as db:
        saved = {key: row.value for key, row in _saved(db).items()}
    return {key: saved.get(key) or _from_environment(key) or LIMITS[key][2] for key in LIMITS}


def describe(db) -> list[dict]:
    saved = _saved(db)
    rows = []
    for key, (label, help_text, default, minimum, maximum) in LIMITS.items():
        environment = _from_environment(key)
        row = saved.get(key)
        value, source = ((row.value, "saved") if row else (environment, "environment") if environment is not None
                         else (default, "default"))
        rows.append({"key": key, "label": label, "help": help_text, "unit": "seconds", "default": default,
                     "minimum": minimum, "maximum": maximum, "environment": environment, "value": value,
                     "source": source, "updated_by": row.updated_by if row else None,
                     "updated_at": row.updated_at if row else None})
    return rows


def developers() -> set[str]:
    return {part.strip() for part in os.environ.get("KB_DEVELOPERS", "").split(",") if part.strip()}


def is_developer(employee_id: str) -> bool:
    return employee_id in developers()


def save(db, values: dict[str, int | None], employee_id: str) -> list[dict]:
    """Validate every value first, then save. None clears a saved value (back to the environment
    or the default). Returns the changes as {key, before, after} for the activity log."""
    from kb.db import ProcessingSetting, now

    unknown = sorted(set(values) - set(LIMITS))
    if unknown:
        raise ValueError(f"Unknown setting: {', '.join(unknown)}")
    for key, value in values.items():
        if value is None:
            continue
        label, _help, _default, minimum, maximum = LIMITS[key]
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"{label} must be a whole number of seconds from {minimum} to {maximum}.")
    saved = _saved(db)
    before = {row["key"]: row["value"] for row in describe(db)}
    changes = []
    for key, value in values.items():
        row = saved.get(key)
        if value is None:
            if row is not None:
                db.delete(row)
        elif row is None:
            db.add(ProcessingSetting(key=key, value=value, updated_by=employee_id, updated_at=now()))
        elif row.value != value:
            row.value, row.updated_by, row.updated_at = value, employee_id, now()
        else:
            continue
    db.flush()
    after = {row["key"]: row["value"] for row in describe(db)}
    for key in values:
        if before[key] != after[key]:
            changes.append({"key": key, "before": before[key], "after": after[key]})
    return changes
