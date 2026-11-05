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
        with patch('kb.guardrails.protect_with_inference', side_effect=lambda value: value), \
                patch('kb.providers.model_request', return_value=result):
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
        with patch('kb.guardrails.protect_with_inference', side_effect=lambda value: value), \
                patch('kb.providers.model_request', return_value=raw):
            return synthesize('What was revenue?', [{'id': 'published:000001', 'text': 'FY22 revenue was 100.'}])

    def test_alias_maps_back_to_original_chunk(self):
        self.assertEqual(self.generate('S1')['claims'][0]['citations'], ['published:000001'])

    def test_unretrieved_alias_rejected(self):
        from kb.providers import AnswerValidationError
        with self.assertRaises(AnswerValidationError):
            self.generate('S9')
