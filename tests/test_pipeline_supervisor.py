"""The ingestion supervisor can stop a stuck worker on this platform (Windows has no os.killpg)."""
import subprocess
import sys

from kb.pipeline import _stop_worker, _worker_options


def test_a_stuck_worker_and_its_child_are_stopped():
    # The worker starts a child of its own, as a real worker starts the Docling worker.
    script = ("import subprocess, sys, time; "
              "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); time.sleep(120)")
    with subprocess.Popen([sys.executable, "-c", script], **_worker_options()) as worker:
        try:
            worker.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        assert worker.poll() is None
        _stop_worker(worker)
        assert worker.wait(timeout=20) is not None
