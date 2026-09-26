"""JSON inspector observes real SDK requests against offline transports only."""
import asyncio
import json
from pathlib import Path
import httpx
import pytest
from backend.config import Settings
from backend.agent import Model
from backend.registry import Registry
from backend.store import Store
from backend.json_requests import JsonObserver,capture_scope


def run_context(tmp_path):
    store=Store(tmp_path/'exchange.sqlite')
    run,_=store.new_run('request','fingerprint','Синтетический запрос',None)
    settings=Settings(db_path=store.path,api_key='test-key',base_url='https://synthetic.invalid')
    return store,run,settings


def completion(content='Ответ',calls=None):
    return dict(id='completion-real-id',object='chat.completion',created=1,model='deepseek-flash',choices=[dict(index=0,finish_reason='tool_calls' if calls else 'stop',message=dict(role='assistant',content=content,tool_calls=calls or [],reasoning_content='INTERNAL_REASONING_SENTINEL'))],usage=dict(prompt_tokens=12,completion_tokens=3,total_tokens=15))


def test_model_records_actual_sdk_http_body_and_response_ids_detached(tmp_path):
    async def scenario():
        store,run,settings=run_context(tmp_path);observer=JsonObserver(store,secrets=(settings.api_key,));seen=[]
        call=dict(id='actual-tool-call',type='function',function=dict(name='weather__resolve_city',arguments='{"name":"Омск"}'))
        def transport(request):
            seen.append(json.loads(request.content));return httpx.Response(200,json=completion(None,[call]))
        model=Model(settings,observer=observer,http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))
        messages=[dict(role='user',content='Погода')];tools=[dict(type='function',function=dict(name='weather__resolve_city',parameters=dict(type='object',properties={}))) ]
        try:
            with capture_scope(run['id']):result=await model.complete(messages,tools)
            records=store.json_requests(run['id']);assert len(records)==1
            record=records[0];assert record['kind']=='model' and record['status']=='completed'
            assert record['request']==dict(method='POST',path='/chat/completions',body=seen[0])
            assert seen[0]['thinking']==dict(type='disabled') and seen[0]['temperature']==0.2
            assert record['response']['id']=='completion-real-id'
            assert record['response']['choices'][0]['message']['tool_calls'][0]['id']==result['tool_calls'][0]['id']=='actual-tool-call'
            assert record['response']['usage']['total_tokens']==15
            assert 'INTERNAL_REASONING_SENTINEL' not in json.dumps(record)
            messages.append(dict(role='user',content='Later mutation'))
            assert store.json_requests(run['id'])[0]['request']['body']['messages']==[dict(role='user',content='Погода')]
        finally:await model.close()
    asyncio.run(scenario())


def test_real_stdio_records_injected_context_and_actual_mcp_result(tmp_path):
    async def scenario():
        store,run,settings=run_context(tmp_path)
        fixture=tmp_path/'fixture.json';fixture.write_text(json.dumps(dict(cities=[dict(name='Омск',latitude=55,longitude=73,timezone='Asia/Omsk')])));settings.fixture_path=str(fixture)
        observer=JsonObserver(store);registry=Registry(settings,observer=observer);await registry.start()
        try:
            with capture_scope(run['id'],trace_sequence=7,tool_call_id='original-tool-call'):
                result=await registry.call('weather__resolve_city',dict(name='Омск'),run_id=run['id'])
            record=store.json_requests(run['id'])[0]
            assert record['kind']=='mcp' and record['status']=='completed'
            assert record['trace_sequence']==7 and record['tool_call_id']=='original-tool-call'
            assert record['request']==dict(method='tools/call',params=dict(name='resolve_city',arguments=dict(name='Омск',run_id=run['id'])))
            captured_result=record['response'].get('structuredContent')
            if captured_result is None:captured_result=json.loads(record['response']['content'][0]['text'])
            assert captured_result==result
            assert 'jsonrpc' not in record['request'] and 'id' not in record['request']
            with capture_scope(run['id']):
                with pytest.raises(ValueError):await registry.call('weather__resolve_city',dict(name='Омск',invalid=True),run_id=run['id'])
            assert len(store.json_requests(run['id']))==1  # Rejected before the wire.
        finally:await registry.close()
    asyncio.run(scenario())


def test_http_error_and_transport_failure_keep_no_invented_response(tmp_path):
    async def scenario():
        store,run,settings=run_context(tmp_path);observer=JsonObserver(store,secrets=('SYNTHETIC_SECRET',))
        model=Model(settings,observer=observer,http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(401,json={'error':{'message':'SYNTHETIC_SECRET denied'}}))))
        try:
            with capture_scope(run['id']):
                with pytest.raises(Exception):await model.complete([dict(role='user',content='test')],[])
            first=store.json_requests(run['id'])[0];assert first['status']=='error' and first['error']=='HTTP 401'
            assert 'SYNTHETIC_SECRET' not in json.dumps(first)
        finally:await model.close()
        def broken(request):raise httpx.ConnectError('SYNTHETIC_SECRET transport detail',request=request)
        model=Model(settings,observer=observer,http_client=httpx.AsyncClient(transport=httpx.MockTransport(broken)))
        try:
            with capture_scope(run['id']):
                with pytest.raises(Exception):await model.complete([dict(role='user',content='test')],[])
            second=store.json_requests(run['id'])[1];assert second['status']=='error' and second['response'] is None
            assert 'SYNTHETIC_SECRET' not in json.dumps(second)
        finally:await model.close()
    asyncio.run(scenario())


def test_observer_failure_does_not_change_real_model_exchange(tmp_path,monkeypatch):
    async def scenario():
        store,run,settings=run_context(tmp_path);observer=JsonObserver(store)
        def fail(value):raise OSError('Synthetic unavailable storage')
        monkeypatch.setattr(store,'allocate_json_request',fail)
        model=Model(settings,observer=observer,http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,json=completion()))))
        try:
            with capture_scope(run['id']):result=await model.complete([dict(role='user',content='test')],[])
            assert result['content']=='Ответ' and store.json_requests(run['id'])==[]
        finally:await model.close()
    asyncio.run(scenario())


def test_explicit_absent_metadata_cannot_inherit_owner_run_context(tmp_path):
    store,run,settings=run_context(tmp_path);observer=JsonObserver(store)
    with capture_scope(run['id']):
        assert observer.begin('mcp',{'method':'tools/call'},metadata=None) is None
        assert observer.begin('mcp',{'method':'tools/call'},metadata={}) is None
        assert observer.begin('model',{'method':'POST'}) is not None
    assert len(store.json_requests(run['id']))==1


def test_serialized_previews_stay_bounded_with_heavy_json_escaping(tmp_path):
    store,run,settings=run_context(tmp_path);observer=JsonObserver(store,max_bytes=512)
    id=observer.begin('model',{'many':[("\\\n\""*1000) for _ in range(30)]},{'run_id':run['id']})
    record=store.get('json_request',id)
    assert record['truncated'] and len(json.dumps(record['request'],ensure_ascii=False).encode())<=512
