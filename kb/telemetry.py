"""Content-free operational telemetry. Direct OTLP/gRPC export to Data Prepper.

Only explicit metadata is collected; never instrument HTTP bodies, exception
messages or arbitrary logging handlers (these can contain document content).
"""
import atexit
import functools
import inspect
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

from opentelemetry import trace
from opentelemetry.propagate import extract, inject
from opentelemetry.trace import Status, StatusCode, SpanKind

_lock = Lock()
_providers = None
_logger = None
_allowed = {'request_id', 'document_id', 'stage', 'completed', 'total', 'model',
            'attempt', 'http_status', 'error_type', 'error_code', 'duration_ms',
            'operation', 'method', 'route', 'result_count', 'retrieval_mode',
            'question_sha256', 'escalation', 'smoke_id'}


def configure():
    global _providers, _logger
    if _providers is not None or os.getenv('KB_TELEMETRY_ENABLED', 'false').lower() != 'true':
        return
    with _lock:
        if _providers is not None:
            return
        import base64
        import grpc
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
        from kb.config import ROOT
        config = json.loads(Path(os.getenv('KB_TELEMETRY_CONFIG', str(ROOT / 'var/telemetry/client.json'))).read_text())
        credentials = grpc.ssl_channel_credentials(Path(config['ca']).read_bytes())
        auth = base64.b64encode(('kb_telemetry:' + config['password']).encode()).decode()
        options = dict(credentials=credentials, insecure=False,
                       headers=(('authorization', 'Basic ' + auth),), timeout=3)
        resource = Resource.create({'service.name': os.getenv('KB_TELEMETRY_SERVICE', 'kbase'),
                                    'service.version': '0.1.0'})
        spans = TracerProvider(resource=resource)
        spans.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=config['traces'], **options),
            max_queue_size=2048, max_export_batch_size=64, schedule_delay_millis=1000))
        logs = LoggerProvider(resource=resource)
        logs.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(endpoint=config['logs'], **options),
            max_queue_size=2048, max_export_batch_size=64, schedule_delay_millis=1000))
        trace.set_tracer_provider(spans)
        _logger = logs.get_logger('kb.operations')
        _providers = (spans, logs)
        atexit.register(shutdown)


def flush():
    return all(p.force_flush(timeout_millis=5000) for p in (_providers or ()))


def shutdown():
    for provider in _providers or ():
        provider.shutdown()


def event(name, *, error=False, **metadata):
    if _logger is None:
        return
    from opentelemetry._logs import SeverityNumber
    safe = {k: v for k, v in metadata.items() if k in _allowed and v is not None}
    _logger.emit(body=name, severity_text='ERROR' if error else 'INFO',
                 severity_number=SeverityNumber.ERROR if error else SeverityNumber.INFO,
                 attributes=safe)


def failure(exc, **metadata):
    span = trace.get_current_span()
    span.set_status(Status(StatusCode.ERROR))
    span.set_attribute('error.type', type(exc).__name__)
    event('operation.failed', error=True, error_type=type(exc).__name__, **metadata)


@contextmanager
def operation(name, *, kind=SpanKind.INTERNAL, **metadata):
    configure()
    parent = None
    if not trace.get_current_span().get_span_context().is_valid and os.getenv('KB_TRACEPARENT'):
        parent = extract({'traceparent': os.environ['KB_TRACEPARENT']})
    with trace.get_tracer('kb.operations').start_as_current_span(name, context=parent, kind=kind,
            record_exception=False, set_status_on_exception=False) as span:
        for key, value in metadata.items():
            if key in _allowed and value is not None:
                span.set_attribute('kb.' + key, value)
        started = time.perf_counter()
        try:
            yield span
        except BaseException as exc:
            failure(exc, operation=name)
            raise
        finally:
            event('operation.finished', operation=name, duration_ms=(time.perf_counter()-started)*1000,
                  **metadata)


def traced(name):
    def decorate(fn):
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def asynchronous(*args, **kwargs):
                with operation(name):
                    result = await fn(*args, **kwargs)
                    if name == 'chat.answer' and isinstance(result, dict):
                        event('chat.completed', escalation=result.get('escalation'), result_count=len(result.get('citations', [])))
                        if result.get('escalation') in {'guardrail_unavailable', 'model_unavailable', 'tool_unavailable'}:
                            trace.get_current_span().set_status(Status(StatusCode.ERROR))
                    return result
            return asynchronous
        @functools.wraps(fn)
        def synchronous(*args, **kwargs):
            with operation(name):
                return fn(*args, **kwargs)
        return synchronous
    return decorate


def propagation_env():
    carrier = {}
    inject(carrier)
    return {'KB_TRACEPARENT': carrier.get('traceparent', ''), 'KB_TELEMETRY_SERVICE': 'kbase-mcp'}


class TelemetryMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('path', '').startswith('/static/'):
            return await self.app(scope, receive, send)
        # Deliberately ignore incoming untrusted trace headers and raw URL/query.
        with operation('HTTP request', kind=SpanKind.SERVER) as span:
            status = 500
            async def capture(message):
                nonlocal status
                if message['type'] == 'http.response.start':
                    status = message['status']
                    ctx = span.get_span_context()
                    if ctx.is_valid:
                        message['headers'] = list(message.get('headers', [])) + [
                            (b'x-trace-id', format(ctx.trace_id, '032x').encode())]
                await send(message)
            try:
                await self.app(scope, receive, capture)
            finally:
                route = getattr(scope.get('route'), 'path', 'unmatched')
                method = scope.get('method', 'UNKNOWN')
                span.update_name(method + ' ' + route)
                span.set_attribute('http.route', route)
                span.set_attribute('http.response.status_code', status)
                if status >= 500:
                    span.set_status(Status(StatusCode.ERROR))
                event('http.request', error=status >= 500, route=route, method=method, http_status=status)
