"""One endpoint per API model: each role's own URL and key, the fallbacks, and plain OpenAI-compatible
requests for an internal model server (MODEL_API=openai)."""
import json
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from kb.config import Settings
from kb.providers import ModelUnavailable, model_request as traced_model_request

model_request = traced_model_request.__wrapped__
CHAT, EMBED = '/api/v1/chat/completions', '/api/v1/embeddings'


def base_cfg(**overrides):
    values = dict(external_models_enabled=True, openrouter_api_key='shared-key', openrouter_base_url='https://openrouter.ai',
                  openrouter_chat_path=CHAT, openrouter_embedding_path=EMBED, model_timeout_seconds=20,
                  openrouter_paid_max_price_prompt=0, openrouter_paid_max_price_completion=0, model_api='openrouter')
    for role in ('answer', 'planner', 'guardrail', 'vision', 'embedding', 'image_embedding'):
        values[f'{role}_model_url'] = ''
        values[f'{role}_model_key'] = ''
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def send():
    """Run model_request against a fake transport; returns (call, requests)."""
    requests = []

    def run(cfg, path, payload, role=None):
        def handler(request):
            requests.append(request)
            body = {'data': [{'index': 0, 'embedding': [0.1, 0.2]}]} if path == EMBED else {'choices': [{'message': {'content': 'ok'}}]}
            return httpx.Response(200, json=body)

        real_client = httpx.Client
        with ExitStack() as stack:
            stack.enter_context(patch('kb.providers.settings', return_value=cfg))
            stack.enter_context(patch('kb.providers.httpx', SimpleNamespace(
                Client=lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw), RequestError=httpx.RequestError)))
            stack.enter_context(patch('kb.processing_context.check_transfer_approval'))
            stack.enter_context(patch('kb.providers.report_progress'))
            stack.enter_context(patch('kb.providers.event'))
            return model_request(path, payload, 0, role=role)

    return run, requests


CHAT_PAYLOAD = {'model': 'granite-vision', 'messages': [{'role': 'user', 'content': 'synthetic'}],
                'reasoning': {'enabled': False}, 'response_format': {'type': 'json_object'}}


def test_each_role_uses_its_own_endpoint_and_key(send):
    run, requests = send
    cfg = base_cfg(model_api='openai', vision_model_url='https://models.internal/granite/v1', vision_model_key='vision-key',
                   answer_model_url='https://models.internal/llm/v1', answer_model_key='answer-key')
    run(cfg, CHAT, CHAT_PAYLOAD, role='vision')
    run(cfg, CHAT, CHAT_PAYLOAD, role='planner')        # no planner endpoint: the answer model's
    run(cfg, CHAT, CHAT_PAYLOAD, role='guardrail')      # likewise
    run(cfg, EMBED, {'model': 'embed:free', 'input': ['x']}, role='embedding')  # none set: the shared gateway
    assert [str(r.url) for r in requests] == [
        'https://models.internal/granite/v1/chat/completions', 'https://models.internal/llm/v1/chat/completions',
        'https://models.internal/llm/v1/chat/completions', 'https://openrouter.ai/api/v1/embeddings']
    assert [r.headers['authorization'] for r in requests] == [
        'Bearer vision-key', 'Bearer answer-key', 'Bearer answer-key', 'Bearer shared-key']


def test_a_full_endpoint_url_is_accepted(send):
    run, requests = send
    cfg = base_cfg(model_api='openai', embedding_model_url='https://models.internal/bge/v1/embeddings/')
    run(cfg, EMBED, {'model': 'bge-small', 'input': ['x']}, role='embedding')
    assert str(requests[0].url) == 'https://models.internal/bge/v1/embeddings'


def test_an_internal_server_gets_a_plain_openai_request(send):
    run, requests = send
    cfg = base_cfg(model_api='openai', openrouter_api_key='', answer_model_url='https://models.internal/llm/v1')
    run(cfg, CHAT, {**CHAT_PAYLOAD, 'model': 'internal-llm'}, role='answer')   # no key anywhere: allowed here
    body = json.loads(requests[0].content)
    assert 'provider' not in body and 'reasoning' not in body
    assert body['response_format'] == {'type': 'json_object'} and body['model'] == 'internal-llm'


def test_openrouter_style_still_needs_a_key_and_https(send):
    run, _ = send
    with pytest.raises(ModelUnavailable, match='no key'):
        run(base_cfg(openrouter_api_key=''), CHAT, CHAT_PAYLOAD, role='answer')
    with pytest.raises(ModelUnavailable, match='HTTPS'):
        run(base_cfg(answer_model_url='http://models.internal/llm/v1'), CHAT, CHAT_PAYLOAD, role='answer')


def test_settings_take_plain_names_and_reject_plain_http(monkeypatch):
    for name in ('ANSWER_MODEL', 'VISION_MODEL', 'EMBEDDING_MODEL', 'OPENROUTER_MODEL', 'LLM_PROVIDER', 'EMBEDDING_BACKEND'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('ANSWER_MODEL', 'internal-llm')
    monkeypatch.setenv('VISION_MODEL', 'granite-vision')
    monkeypatch.setenv('EMBEDDING_MODEL', 'bge-small')
    monkeypatch.setenv('LLM_PROVIDER', 'api')
    cfg = Settings(_env_file=None)
    assert (cfg.openrouter_model, cfg.openrouter_vision_model, cfg.openrouter_embedding_model) == (
        'internal-llm', 'granite-vision', 'bge-small')
    assert cfg.llm_provider == 'openrouter'   # 'api' is the plain name for the model-API adapter
    with pytest.raises(ValueError, match='HTTPS'):
        Settings(_env_file=None, vision_model_url='http://models.internal/granite/v1')


def test_internal_servers_keep_the_configured_model_order():
    from kb import config
    with patch('kb.config.settings', return_value=base_cfg(model_api='openai')):
        assert config.model_chain('big-llm, small-llm', 'big-llm') == ['big-llm', 'small-llm']
    with patch('kb.config.settings', return_value=base_cfg()):  # OpenRouter: paid models need ceilings
        assert config.model_chain('big-llm, small-llm:free') == ['small-llm:free']


def test_endpoints_join_the_approval_scope_only_when_set():
    from kb import governance
    plain = governance.processing_scope()
    with patch('kb.governance.settings', return_value=Settings(_env_file=None, vision_model_url='https://models.internal/granite/v1',
                                                               vision_model_key='secret', model_api='openai')):
        scoped = governance.processing_scope()
    assert 'vision_model_url' not in plain['config'] and 'model_api' not in plain['config']
    assert scoped['config']['vision_model_url'] == 'https://models.internal/granite/v1' and scoped['config']['model_api'] == 'openai'
    assert 'secret' not in json.dumps(scoped)
