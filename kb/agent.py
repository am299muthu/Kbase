from kb.telemetry import traced, operation, event, failure
import asyncio
import hashlib
import json
import os
import re
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from kb.config import ROOT, settings
from kb.db import audit, db_session
from kb.providers import AnswerValidationError, ModelUnavailable, plan_questions, synthesize
from kb.security import authenticate, pii


# Only split at explicit boundaries. An ordinary conjunction such as "compare
# revenue and costs" belongs to the same evidence request.
_QUESTION_START = r'(?:who|what|when|where|why|how|which|is|are|was|were|did|does|do|can|could|list|give|describe|explain|summari[sz]e|compare)\b'
_LIST_MARKER = r'(?:[-*•]|\d{1,2}[.)])\s+'
_BOUNDARY = re.compile(
    rf'(?<=\?)\s+|\n\s*(?={_LIST_MARKER}|{_QUESTION_START})|'
    rf'\s*;\s*(?={_QUESTION_START})|\s+(?:and|also)\s+(?={_QUESTION_START})', re.I)
_DEPENDENT = re.compile(r"\b(?:it|its|they|them|their|those|these|this|former|latter|that figure|that period|the same|the two|the difference|what about|how about)\b", re.I)


def question_parts(message):
    parts = []
    for fragment in _BOUNDARY.split(message.strip()):
        question = re.sub(rf'^(?:{_LIST_MARKER})', '', fragment.strip())
        if not question:
            continue
        if parts and (_DEPENDENT.search(question) or parts[-1].endswith(':')):
            parts[-1] += ' ' + question
        else:
            parts.append(question)
    return parts or [message]


@traced('chat.answer')
async def answer_question(token: str, message: str, top_k: int = 5) -> dict:
    cfg = settings()
    with db_session() as db:
        user = authenticate(db, token)
        employee_id, unmasked = user.id, user.pii_unmasked
        audit(db, user.id, 'chat.started', user.id, {'question_sha256': hashlib.sha256(message.encode()).hexdigest()})
    event("chat.started", question_sha256=hashlib.sha256(message.encode()).hexdigest())
    trace = []
    mode = 'openrouter' if cfg.llm_provider == 'openrouter' else 'local-extractive-demo'

    async def response(answer: str, citations=None, escalation=None):
        # Refresh identity and entitlements for ALL exits, including errors and withdrawn evidence.
        from kb.search import get_section
        from kb.security import permitted_folders
        try:
            with db_session() as db:
                current = authenticate(db, token)
                safe_unmasked = current.pii_unmasked
                allowed = set(permitted_folders(db, current.id))
                traced_ids, traced_folders = set(), set()
                for step in trace:
                    result = step.get('result', {})
                    for item in result.get('evidence', []):
                        traced_ids.add(item['id'])
                    if result.get('section'):
                        traced_ids.add(result['section']['id'])
                    for item in result.get('folders', []):
                        if isinstance(item, dict) and item.get('id'):
                            traced_folders.add(item['id'])
                changed = not traced_folders.issubset(allowed) or any(get_section(db, current.id, ref) is None for ref in traced_ids)
                if changed:
                    return {'answer': 'Access changed while the answer was prepared. Please ask again.', 'citations': [], 'trace': [], 'mode': mode, 'escalation': 'access_changed'}
        except Exception:
            return {'answer': 'Your session or access is no longer available. Please sign in again.', 'citations': [], 'trace': [], 'mode': mode, 'escalation': 'session_unavailable'}
        payload = pii.protect({'answer': answer, 'citations': citations or [], 'trace': trace, 'mode': mode, 'escalation': escalation}, safe_unmasked)
        if cfg.external_models_enabled and cfg.guardrails_enabled and not safe_unmasked:
            from kb.guardrails import protect_with_inference
            try:
                payload = await asyncio.to_thread(protect_with_inference, payload)
            except ModelUnavailable:
                return {'answer': 'PII inference guardrails could not verify safe output. Content was withheld.',
                        'citations': [], 'trace': [], 'mode': mode, 'escalation': 'guardrail_unavailable'}
            # Guardrail inference may take time: recheck access after the last
            # external call, not just before it, including PII entitlement changes.
            try:
                with db_session() as db:
                    current = authenticate(db, token)
                    if (current.pii_unmasked != safe_unmasked
                            or not traced_folders.issubset(set(permitted_folders(db, current.id)))
                            or any(get_section(db, current.id, ref) is None for ref in traced_ids)):
                        return {'answer': 'Access changed while the answer was prepared. Please ask again.',
                                'citations': [], 'trace': [], 'mode': mode, 'escalation': 'access_changed'}
            except Exception:
                return {'answer': 'Your session or access is no longer available. Please sign in again.',
                        'citations': [], 'trace': [], 'mode': mode, 'escalation': 'session_unavailable'}
        return payload

    if re.search(r'(?i)\b(ignore (?:all |previous |your )*instructions|reveal (?:your |the )?(?:system|prompt)|bypass (?:access|security)|disable (?:masking|guardrails))', message):
        return await response('I cannot bypass access controls or disclose system instructions. Ask a question about your authorised knowledge sources.', escalation='refused')
    from kb.telemetry import propagation_env
    env = {**os.environ, **propagation_env(), 'KB_MCP_SESSION': token, 'PYTHONPATH': str(ROOT)}
    parameters = StdioServerParameters(command=sys.executable, args=['-m', 'kb.mcp_server'], env=env, cwd=str(ROOT))
    try:
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                discovered = await session.list_tools()
                available = {tool.name for tool in discovered.tools}

                async def call(name, arguments=None):
                    if name not in available:
                        raise RuntimeError('Required MCP tool unavailable')
                    start = time.perf_counter()
                    result = await session.call_tool(name, arguments=arguments or {})
                    if result.isError:
                        raise RuntimeError('MCP tool failed')
                    structured = getattr(result, 'structuredContent', None)
                    if structured is None:
                        text = ''.join(getattr(item, 'text', '') for item in result.content)
                        structured = json.loads(text)
                    trace.append({'tool': name, 'arguments': pii.protect(arguments or {}, unmasked),
                                  'result': pii.protect(structured, unmasked), 'duration_ms': round((time.perf_counter() - start) * 1000)})
                    return structured

                access = await call('lookupemployeeaccesscontrol')
                if not access['folders']:
                    return await response('You do not have access to any business folders. Contact a folder owner or the Data Office.', escalation='no_access')
                questions = question_parts(message)
                if len(questions) == 1 and re.search(r'(?i)^request access to ([a-z0-9_-]+)', message):
                    folder_id = re.search(r'(?i)^request access to ([a-z0-9_-]+)', message).group(1)
                    request = await call('requestaccess', {'folder_id': folder_id, 'reason': 'Employee explicitly requested access review via chat.'})
                    return await response('Access review requested. No access permissions were changed.' if request['status'] == 'pending_owner_review' else request['message'], escalation=request['status'])
                folders = await call('listbusinessfolders')

                permission_query = r'(?i)\b(my access|my permissions|which folders|what folders|pii entitlement)\b'
                plan = [(question, question) for question in questions]
                if cfg.llm_provider == 'openrouter' and not re.search(permission_query, message):
                    started = time.perf_counter()
                    method = 'LLM'
                    try:
                        plan = await asyncio.to_thread(plan_questions, message)
                    except ModelUnavailable:
                        method = 'Local fallback'
                    trace.append({'tool': 'plan_questions', 'arguments': {},
                                  'result': {'method': method, 'parts': [query for _, query in plan]},
                                  'duration_ms': round((time.perf_counter() - started) * 1000)})

                async def answer_part(question, query):
                    if re.search(permission_query, question):
                        lines = [f"{item['path']} ({item['business_unit']})" for item in folders['folders']]
                        safe = await call('checkpiicompliance', {'text': 'Your authorised folders:\n' + '\n'.join(lines) + '\nPII visibility: ' + ('unmasked' if access['pii_unmasked'] else 'masked')})
                        return safe['text'], [], None
                    found = await call('searchdocuments', {'query': query, 'top_k': top_k})
                    evidence = found['evidence']
                    if not evidence:
                        return 'I could not find supporting evidence in your authorised, indexed documents. Try a more specific question or ask a folder owner to onboard the relevant source.', [], 'insufficient_evidence'
                    # Resolve actual source sections through MCP, not direct functions; gather multiple formats when retrieved.
                    resolved = []
                    for item in evidence:
                        section = await call('getdocsection', {'chunk_id': item['id']})
                        if section.get('section'):
                            resolved.append(section['section'])
                    if not resolved:
                        return 'The retrieved evidence is no longer available to your account.', [], 'insufficient_evidence'
                    by_id = {item['id']: item for item in resolved}
                    used = []
                    if cfg.llm_provider == 'openrouter':
                        evidence = [{key: item.get(key) for key in ('id', 'title', 'section', 'page', 'text')}
                                    for item in resolved]
                        intent = question if query == question else (
                            f'Original request: {question}\nSearch wording (context only): {query}\n'
                            'Answer the original request. Use search wording only to resolve references.')
                        from kb.guardrails import verify_grounded_claims
                        for correction in (False, True):
                            try:
                                generated = await asyncio.to_thread(synthesize, intent, evidence, correction)
                                if generated['insufficient']:
                                    return 'The available evidence is insufficient to answer this question.', [], 'insufficient_evidence'
                                await asyncio.to_thread(verify_grounded_claims, intent, generated['claims'], evidence)
                                break
                            except AnswerValidationError:
                                if correction:
                                    raise
                        lines = []
                        for claim in generated['claims']:
                            refs = claim.get('citations', [])
                            if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in by_id for ref in refs) or not isinstance(claim.get('text'), str):
                                raise ModelUnavailable('Answer failed citation validation.')
                            used.extend(refs)
                            lines.append(claim['text'] + ' ' + ' '.join(f'[{ref}]' for ref in refs))
                        if not lines:
                            raise ModelUnavailable('No grounded claims were returned.')
                        answer = '\n\n'.join(lines)
                    else:
                        # No invented synthesis: clearly labelled verbatim evidence with stable citations.
                        lines = ['Local evidence excerpts (no generative model used):']
                        for item in resolved:
                            excerpt = item['text'][:1200]
                            lines.append(f"{excerpt}\n[{item['id']}]")
                            used.append(item['id'])
                        answer = '\n\n'.join(lines)
                    citations = [{'id': ref, 'document_id': by_id[ref]['document_id'], 'title': by_id[ref]['title'],
                                  'section': by_id[ref]['section'], 'page': by_id[ref].get('page'), 'pages': by_id[ref].get('pages', []),
                                  'snippet': by_id[ref]['text'][:1200], 'source_uri': by_id[ref]['source_uri']} for ref in dict.fromkeys(used)]
                    return answer, citations, None

                if len(plan) > 6:
                    return await response('Please send up to six questions per message so each can be checked against its sources.', escalation='clarification_needed')
                answers, sources, errors = [], {}, []
                slots = asyncio.Semaphore(2)

                async def run_part(question, query):
                    try:
                        async with slots:
                            part, references, error = await answer_part(question, query)
                            part = (await call('checkpiicompliance', {'text': part}))['text']
                    except ModelUnavailable as exc:
                        failure(exc)
                        part, references, error = str(exc), [], 'model_unavailable'
                    except Exception as exc:
                        failure(exc)
                        part, references, error = ('A knowledge tool failed while checking this question. Please retry it.',
                                                   [], 'tool_unavailable')
                    return part, references, error

                results = await asyncio.gather(*(run_part(question, query) for question, query in plan))
                for number, ((question, _), (part, references, error)) in enumerate(zip(plan, results), 1):
                    answers.append(part if len(plan) == 1 else f'{number}. {question}\n{part}')
                    sources.update((ref['id'], ref) for ref in references)
                    errors.append(error)
                used, citations = list(sources), list(sources.values())
                escalation = errors[0] if len(set(errors)) == 1 else 'partial_answer'
                with db_session() as db:
                    # Check session and entitlement once more just before releasing the response.
                    from kb.search import get_section
                    authenticate(db, token)
                    if any(get_section(db, employee_id, ref) is None for ref in used):
                        return await response('Access changed while the answer was prepared. Please ask again.', escalation='access_changed')
                    audit(db, employee_id, 'chat.completed', employee_id, {'citation_ids': used, 'tools': [item['tool'] for item in trace], 'mode': mode})
                return await response('\n\n'.join(answers), citations, escalation)
    except ModelUnavailable as exc:
        return await response(str(exc), escalation='model_unavailable')
    except Exception as exc:
        failure(exc)
        return await response('The knowledge tools are currently unavailable. No unsupported answer was generated. Check service health and try again.', escalation='tool_unavailable')
