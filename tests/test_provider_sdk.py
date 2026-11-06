import json
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from kb.providers import ModelHTTPError, ModelUnavailable, model_request as traced_model_request, start_diagnostics


# Exercise the real SDK transport without initializing the process-wide tracer.
model_request = traced_model_request.__wrapped__


class ProviderSdkTests(unittest.TestCase):
    def setUp(self):
        self.cfg = SimpleNamespace(external_models_enabled=True, openrouter_api_key='test-key',
            openrouter_base_url='https://openrouter.ai', openrouter_chat_path='/api/v1/chat/completions',
            openrouter_embedding_path='/api/v1/embeddings', model_timeout_seconds=20)
        self.requests=[]
        self.responses=[httpx.Response(200,json={'choices':[{'message':{'content':'ok'}}]})]
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        real_client=httpx.Client
        def handler(request):
            self.requests.append(request)
            response=self.responses.pop(0)
            if isinstance(response,Exception): raise response
            return response
        def make_client(**kwargs):
            self.assertFalse(kwargs['trust_env'])
            self.assertFalse(kwargs['follow_redirects'])
            return real_client(transport=httpx.MockTransport(handler), **kwargs)
        self.stack.enter_context(patch('kb.providers.settings',return_value=self.cfg))
        self.stack.enter_context(patch('kb.providers.httpx', SimpleNamespace(Client=make_client, RequestError=httpx.RequestError)))
        self.approval=self.stack.enter_context(patch('kb.processing_context.check_transfer_approval'))
        self.stack.enter_context(patch('kb.providers.report_progress'))
        self.stack.enter_context(patch('kb.providers.event'))
        self.stack.enter_context(patch('kb.providers.time.sleep'))
        self.diagnostics=start_diagnostics()
        self.payload={'model':'vision/model:free','messages':[{'role':'user','content':[
            {'type':'text','text':'Synthetic test'}, {'type':'image_url','image_url':{'url':'data:image/png;base64,AAAA'}}]}],
            'reasoning':{'enabled':False}, 'response_format':{'type':'json_object'},'max_tokens':2400}

    def call(self,retries=0):
        return model_request(self.cfg.openrouter_chat_path,self.payload,retries)

    def test_chat_sdk_keeps_openrouter_endpoint_and_vision_fields(self):
        result=self.call()
        self.assertEqual(result['choices'][0]['message']['content'],'ok')
        request=self.requests[0];body=json.loads(request.content)
        self.assertEqual(str(request.url),'https://openrouter.ai/api/v1/chat/completions')
        self.assertEqual(request.headers['authorization'],'Bearer test-key')
        self.assertIn('OpenAI/Python',request.headers['user-agent'])
        for key,value in self.payload.items(): self.assertEqual(body[key],value)
        self.assertEqual(body['provider'],{'allow_fallbacks':False,'max_price':{'prompt':0,'completion':0}})

    def test_embeddings_request_float_vectors(self):
        self.responses=[httpx.Response(200,json={'data':[{'index':0,'embedding':[0.1,0.2]}]})]
        result=model_request(self.cfg.openrouter_embedding_path,{'model':'embed/model:free','input':['synthetic']},0)
        self.assertEqual(result['data'][0]['embedding'],[0.1,0.2])
        self.assertEqual(self.requests[0].url.path,'/api/v1/embeddings')
        self.assertEqual(json.loads(self.requests[0].content)['encoding_format'],'float')

    def test_rate_limit_has_only_application_retries_and_checks_approval(self):
        self.responses=[httpx.Response(429,json={'error':{'message':'limited'}}),self.responses[0]]
        self.call(retries=1)
        self.assertEqual(len(self.requests),2)
        self.assertEqual(self.approval.call_count,2)
        self.assertEqual(self.diagnostics[0]['http_status'],429)

    def test_http_error_preserves_diagnostics_and_redacts_key(self):
        self.responses=[httpx.Response(401,json={'error':{'message':'bad test-key'}})]
        with self.assertRaises(ModelHTTPError) as caught:self.call(retries=3)
        self.assertEqual(caught.exception.status_code,401)
        self.assertEqual(len(self.requests),1)
        self.assertNotIn('test-key',self.diagnostics[-1]['response'])

    def test_network_failure_is_wrapped(self):
        self.responses=[httpx.ConnectError('unreachable')]
        with self.assertRaises(ModelUnavailable):self.call()
        self.assertEqual(self.diagnostics[-1]['kind'],'network_error')

    def test_revoked_approval_prevents_next_attempt(self):
        self.responses=[httpx.Response(429,json={'error':{'message':'limited'}})]
        self.approval.side_effect=[None,ModelUnavailable('approval revoked')]
        with self.assertRaises(ModelUnavailable):self.call(retries=1)
        self.assertEqual(len(self.requests),1)

    def test_redirect_does_not_forward_credentials(self):
        self.responses=[httpx.Response(307,headers={'location':'https://other.example/collect'})]
        with self.assertRaises(ModelUnavailable):self.call()
        self.assertEqual(len(self.requests),1)


if __name__=='__main__':unittest.main()
