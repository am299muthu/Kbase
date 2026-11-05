"""Coordinate in-process PDFium use by preview handlers and ingestion."""
from functools import wraps
from pathlib import Path
from threading import RLock

pdf_render_lock = RLock()


def serialize_pdf_processing(function):
    @wraps(function)
    def wrapped(path, *args, **kwargs):
        if Path(path).suffix.lower() == '.pdf':
            with pdf_render_lock:
                return function(path, *args, **kwargs)
        return function(path, *args, **kwargs)
    return wrapped
