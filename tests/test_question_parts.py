import unittest
from kb.agent import question_parts


class QuestionPartsTests(unittest.TestCase):
    def test_independent_topics(self):
        self.assertEqual(question_parts('When did the outage start? What is the refund policy?'),
                         ['When did the outage start?', 'What is the refund policy?'])

    def test_list_and_inline_requests(self):
        self.assertEqual(question_parts('1. List the branches\n2. Describe holiday coverage'),
                         ['List the branches', 'Describe holiday coverage'])
        self.assertEqual(question_parts('Who runs support and when is payroll due?'),
                         ['Who runs support', 'when is payroll due?'])

    def test_comparison_and_dependent_followup(self):
        for question in ['Compare revenue and costs for FY24.', 'What was revenue? How did it change?']:
            self.assertEqual(question_parts(question), [question])


class ModelPlanningTests(unittest.TestCase):
    def plan(self, parts):
        import json
        from unittest.mock import patch
        from kb.providers import plan_questions
        result = {'choices': [{'message': {'content': json.dumps({'parts': parts})}, 'finish_reason': 'stop'}]}
        with patch('kb.providers.model_request', return_value=result):
            return plan_questions('State revenue and headcount for FY22.')

    def test_shared_period_is_carried_into_each_search(self):
        plan = self.plan([{'original': 'State revenue', 'query': 'What was FY22 revenue?'},
                          {'original': 'and headcount for FY22.', 'query': 'What was FY22 headcount?'}])
        self.assertEqual([query for _, query in plan], ['What was FY22 revenue?', 'What was FY22 headcount?'])

    def test_omitted_request_invalidates_plan(self):
        from kb.providers import ModelUnavailable
        with self.assertRaises(ModelUnavailable):
            self.plan([{'original': 'State revenue', 'query': 'What was FY22 revenue?'}])


class CitationAliasTests(unittest.TestCase):
    def generate(self, ref):
        import json
        from unittest.mock import patch
        from kb.providers import synthesize
        answer = {'claims': [{'text': 'FY22 revenue was 100.', 'citations': [ref]}], 'insufficient': False}
        raw = {'choices': [{'message': {'content': json.dumps(answer)}}]}
        with patch('kb.providers.model_request', return_value=raw):
            return synthesize('What was revenue?', [{'id': 'published:000001', 'text': 'FY22 revenue was 100.'}])

    def test_alias_maps_back_to_original_chunk(self):
        self.assertEqual(self.generate('S1')['claims'][0]['citations'], ['published:000001'])

    def test_unretrieved_alias_rejected(self):
        from kb.providers import AnswerValidationError
        with self.assertRaises(AnswerValidationError):
            self.generate('S9')


class PlanningFastPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_simple_questions_skip_model_and_preserve_query(self):
        from unittest.mock import patch
        from kb.agent import build_question_plan
        with patch('kb.agent.plan_questions') as planner:
            for question in ['Covid-19 deaths in Q2 2022 ?', 'What was net profit for wholesale banking in FY 2022?',
                             'Who runs support?', 'Explain the refund policy']:
                plan, trace = await build_question_plan(question)
                self.assertEqual(plan, [(question, question)])
                self.assertEqual(trace['result']['method'], 'Direct search')
            planner.assert_not_called()

    async def test_complex_questions_use_model(self):
        from unittest.mock import patch
        from kb.agent import build_question_plan
        questions = ['Covid deaths? What was profit?', 'State revenue and headcount for FY22.',
                     'Compare FY22 with FY23', 'What was revenue? How did it change?',
                     '1. Revenue\n2. Headcount', 'Explain revenue; describe costs',
                     'What is revenue, profit, margin?', 'What were revenue/profit?',
                     'Explain ' + 'context ' * 40]
        for question in questions:
            with self.subTest(question=question), patch('kb.agent.plan_questions', return_value=[(question, question)]) as planner:
                _, trace = await build_question_plan(question)
                planner.assert_called_once_with(question)
                self.assertEqual(trace['result']['method'], 'LLM')

    async def test_planning_failure_uses_local_fallback(self):
        from unittest.mock import patch
        from kb.agent import build_question_plan
        from kb.providers import ModelUnavailable
        with patch('kb.agent.plan_questions', side_effect=ModelUnavailable('unavailable')):
            plan, trace = await build_question_plan('Who runs support? When is payroll?')
            self.assertEqual(len(plan), 2)
            self.assertEqual(trace['result']['method'], 'Local fallback')
