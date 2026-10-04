import sys
from pathlib import Path
import httpx
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.llm import DeepSeekClient,LLMError

@pytest.mark.parametrize('code',[429,503])
def test_bounded_retry_and_json_settings(tmp_path,code):
    (tmp_path/'.env').write_text('DEEPSEEK_API_KEY=test\nDEEPSEEK_MODEL=test-model')
    requests=[]
    def handler(request):
        requests.append(request)
        if len(requests)<3:return httpx.Response(code)
        return httpx.Response(200,json={'choices':[{'message':{'content':'{}'}}],'usage':{'total_tokens':4}})
    import json
    c=DeepSeekClient(httpx.Client(transport=httpx.MockTransport(handler)),sleep=lambda _:None,root=tmp_path,max_tokens=900)
    assert c([{'role':'user','content':'JSON'}])['usage']['total_tokens']==4
    body=json.loads(requests[-1].content)
    assert len(requests)==3 and body['response_format']=={'type':'json_object'} and body['max_tokens']==900
    assert body['thinking']=={'type':'disabled'}
    c.close()

def test_read_timeout_never_repeats(tmp_path):
    (tmp_path/'.env').write_text('DEEPSEEK_API_KEY=test')
    calls=[]
    def handler(req):calls.append(req);raise httpx.ReadTimeout('private')
    c=DeepSeekClient(httpx.Client(transport=httpx.MockTransport(handler)),root=tmp_path)
    with pytest.raises(LLMError):c([])
    assert len(calls)==1
    c.close()
