import asyncio
import os
import unittest
from unittest.mock import patch
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from kb import telemetry

class TelemetryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(cls.exporter))
        trace.set_tracer_provider(provider)

    def setUp(self):
        self.exporter.clear()

    def test_error_keeps_type_without_sensitive_message(self):
        with patch.dict(os.environ, {'KB_TELEMETRY_ENABLED': 'false'}):
            with self.assertRaises(ValueError):
                with telemetry.operation('test.failure', question='secret prompt'):
                    raise ValueError('SECRET DOCUMENT AND TOKEN')
        span = self.exporter.get_finished_spans()[0]
        self.assertEqual(span.status.status_code, trace.StatusCode.ERROR)
        self.assertEqual(span.attributes['error.type'], 'ValueError')
        self.assertFalse(span.events)
        self.assertNotIn('SECRET', str(span.to_json()))
        self.assertNotIn('secret prompt', str(span.to_json()))

    def test_nested_trace_and_subprocess_propagation(self):
        with telemetry.operation('parent') as parent:
            env = telemetry.propagation_env()
            parent_id = parent.get_span_context().trace_id
        with patch.dict(os.environ, env):
            with telemetry.operation('child') as child:
                self.assertEqual(parent_id, child.get_span_context().trace_id)
        child_span = self.exporter.get_finished_spans()[-1]
        self.assertEqual(child_span.parent.span_id, parent.get_span_context().span_id)

    def test_http_records_template_not_sensitive_url(self):
        from types import SimpleNamespace
        async def app(scope, receive, send):
            scope['route'] = SimpleNamespace(path='/api/documents/{id}')
            await send({'type': 'http.response.start', 'status': 500, 'headers': []})
            await send({'type': 'http.response.body', 'body': b''})
        messages = []
        async def send(message): messages.append(message)
        asyncio.run(telemetry.TelemetryMiddleware(app)(
            {'type': 'http', 'path': '/api/documents/SECRET', 'query_string': b'token=SECRET', 'method': 'GET'}, None, send))
        span = self.exporter.get_finished_spans()[-1]
        self.assertEqual(span.name, 'GET /api/documents/{id}')
        self.assertNotIn('SECRET', str(span.to_json()))
        self.assertIn(b'x-trace-id', dict(messages[0]['headers']))

if __name__ == '__main__': unittest.main()
