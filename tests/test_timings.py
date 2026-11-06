import asyncio
import unittest
from kb.timings import collect_timings, measure, capture_chat_timings


class TimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_contexts_are_isolated_and_threads_are_collected(self):
        async def request(name):
            with collect_timings() as samples:
                def work():
                    with measure(name):
                        pass
                await asyncio.to_thread(work)
                return samples
        first, second = await asyncio.gather(request('first'), request('second'))
        self.assertEqual([s['stage'] for s in first], ['first'])
        self.assertEqual([s['stage'] for s in second], ['second'])

    async def test_failure_duration_and_chat_total_are_retained(self):
        @capture_chat_timings
        async def request():
            try:
                with measure('failed_stage'):
                    raise ValueError()
            except ValueError:
                return {'answer': 'withheld'}
        result = await request()
        self.assertEqual(result['timings'][0]['status'], 'error')
        self.assertEqual(result['timings'][1]['stage'], 'chat_total')
