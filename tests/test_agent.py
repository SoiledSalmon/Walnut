from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from google.genai import types

from buyorwait.agent import Agent, RateLimiter
from buyorwait.store import Store
from test_finance import case


def response(name,args):
    return SimpleNamespace(candidates=[SimpleNamespace(content=types.Content(role='model',parts=[types.Part(function_call=types.FunctionCall(name=name,args=args))]))],usage_metadata=None)


def test_model_driven_loop_finalizes_only_valid_certificate(tmp_path,monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY','test-only-placeholder')
    agent=Agent(Store(tmp_path/'test.sqlite'),RateLimiter(rpm=1000),'test')
    def generate(**kwargs):
        contents=kwargs['contents']
        if len(contents)==1: return response('evaluate_plans',{'facts':[]})
        result=contents[-1].parts[-1].function_response.response
        return response('finalize_decision',{'certificate_id':result['certificate_id']})
    agent.client=SimpleNamespace(models=SimpleNamespace(generate_content=generate))
    c=case();c.messages=[{'message_id':'m','message_text':'No financial amendment.'}]
    result=agent.process(c,'test-model')
    assert result['decision']['recommended_payment_method']=='full_payment'
    assert [x['tool'] for x in result['trace']]==['evaluate_plans','finalize_decision']


def test_agent_iteration_cap(tmp_path,monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY','test-only-placeholder')
    agent=Agent(Store(tmp_path/'test.sqlite'),RateLimiter(rpm=1000),'test')
    generate=Mock(return_value=response('get_records',{}))
    agent.client=SimpleNamespace(models=SimpleNamespace(generate_content=generate))
    c=case();c.messages=[{'message_id':'m','message_text':'No financial amendment.'}]
    with pytest.raises(RuntimeError,match='Investigation limit'):agent.process(c,'test-model')
    assert generate.call_count==4


def test_resume_and_fingerprint_invalidation(tmp_path):
    store=Store(tmp_path/'checkpoints.sqlite')
    store.save('r','case','fingerprint','complete',{'answer':1})
    assert store.cached('r','case','fingerprint')=={'answer':1}
    assert store.cached('r','case','changed') is None
    store.save('r','case','fingerprint','unresolved',{'error':'missing evidence'})
    assert store.cached('r','case','fingerprint') is None


def test_usage_counts_failed_provider_calls(tmp_path):
    store=Store(tmp_path/'usage.sqlite')
    first=store.reserve_call('r','case','model')
    store.finish_call(first,error='network unavailable')
    second=store.reserve_call('r','case','model')
    store.finish_call(second,{'prompt_token_count':12,'candidates_token_count':3,'total_token_count':15})
    report=store.report('r')[0]
    assert report['calls']==2 and report['errors']==1 and report['total_tokens']==15
