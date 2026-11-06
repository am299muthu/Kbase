"""Request-local latency measurements without prompts, content, or credentials."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from time import perf_counter

_samples = ContextVar('latency_samples', default=None)


@contextmanager
def collect_timings():
    samples = []
    token = _samples.set(samples)
    try:
        yield samples
    finally:
        _samples.reset(token)


@contextmanager
def measure(stage):
    started = perf_counter()
    status = 'ok'
    try:
        yield
    except BaseException:
        status = 'error'
        raise
    finally:
        samples = _samples.get()
        if samples is not None:
            samples.append({'stage': stage, 'duration_ms': round((perf_counter() - started) * 1000, 3),
                            'status': status})


def timed(stage):
    def decorate(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            with measure(stage):
                return func(*args, **kwargs)
        return wrapper
    return decorate


def capture_chat_timings(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        with collect_timings() as samples:
            with measure('chat_total'):
                result = await func(*args, **kwargs)
            result['timings'] = samples
            return result
    return wrapper
