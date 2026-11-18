"""Coordinate in-process PDFium use by preview handlers and ingestion."""
import os
from functools import wraps
from pathlib import Path
from threading import RLock

pdf_render_lock = RLock()


def serialize_pdf_processing(function):
    @wraps(function)
    def wrapped(path, *args, **kwargs):
        # docling_local parses in a separate worker process, so it uses no PDFium in this process;
        # holding the lock there would block every page preview for the whole (long) parse.
        local_worker = os.environ.get('PDF_PARSER', 'pypdf').lower() == 'docling_local'
        if Path(path).suffix.lower() == '.pdf' and not local_worker:
            with pdf_render_lock:
                return function(path, *args, **kwargs)
        return function(path, *args, **kwargs)
    return wrapped
