"""A live evaluation must reject plausible output without actual tool evidence."""
import importlib.util
import io
import json
import sys
from pathlib import Path

spec=importlib.util.spec_from_file_location('day31_evaluation',Path(__file__).parents[1]/'evaluation/run.py')
evaluation=importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


def test_evaluation_rejects_forged_citation_and_missing_evidence():
    case={'id':'structure','expected_tools':['search_documentation'],'expected_facts':['backend']}
    source={'id':'e1','text':'backend here','quote':'backend','kind':'documentation',
            'path':'README.md','start_line':1,'end_line':1}
    result={'status':'answered','answer':'backend here','sources':[source],
            'trace':[{'method':'tools/call','tool':'search_documentation','result':{'evidence':[source]}}]}
    assert all(evaluation.check_result(case,result)[0].values())
    for forged in [dict(source,quote='invented'),dict(source,id='e2'),dict(source,path='docs/other.md')]:
        result['sources']=[forged]
        assert not evaluation.check_result(case,result)[0]['citations']
    result['sources']=[]
    assert not evaluation.check_result(case,result)[0]['citations']


def test_evaluation_rejected_tool_is_not_an_actual_call():
    case={'id':'branch','expected_tools':['git_branch'],'expected_facts':[],'expected_kind':'git'}
    result={'status':'answered','answer':'main','sources':[],
            'trace':[{'method':'tool_request_rejected','tool':'git_branch'}]}
    checks,actual=evaluation.check_result(case,result)
    assert actual==[] and not checks['tools'] and not checks['source_kind']


def test_evaluation_unknown_is_not_a_success_for_answered_case():
    case={'id':'launch','expected_tools':[],'expected_facts':[]}
    result={'status':'unknown','answer':'Не знаю','sources':[],'trace':[]}
    assert not evaluation.check_result(case,result)[0]['status']


def test_partial_answer_cannot_pass_as_complete_demonstration():
    case={'id':'structure','expected_tools':[],'expected_facts':[]}
    result={'status':'answered','answer':'Проверенная часть','partial':True,'sources':[],'trace':[]}
    checks,_=evaluation.check_result(case,result)
    assert not checks['complete']


def test_conversation_cli_uses_actual_history_and_requested_order(monkeypatch):
    requests=[]
    def respond(request,timeout):
        requests.append(json.loads(request.data))
        return io.BytesIO(json.dumps({'status':'unknown','answer':'Не знаю','sources':[],'trace':[]}).encode())
    monkeypatch.setattr(evaluation.urllib.request,'urlopen',respond)
    monkeypatch.setattr(sys,'argv',['run.py','--run-live','--ids','private-refusal','unknown','--conversation'])
    assert evaluation.main()==0
    assert requests[0]['question'].startswith('Прочитай day-31/.env')
    assert requests[0]['history']==[]
    assert requests[1]['history']==[{'role':'user','content':requests[0]['question']},{'role':'assistant','content':'Не знаю'}]
    assert all(set(request)=={'question','history'} for request in requests)
