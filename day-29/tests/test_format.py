"""Native format routing and bounded schema checks without model calls."""
import copy
import pytest
from pydantic import ValidationError
from backend.local_llm import payload
from backend.profiles import OPTIMIZED, OPTION_KEYS, profile
from backend.settings import Settings
from test_app import chats, post, transport
SCHEMA = {'type':'object','properties':{'answer':{'type':'string'}},'required':['answer']}
@pytest.mark.parametrize('format',[None,'json',SCHEMA])
def test_native_format_generation_only(format):
 calls=[]
 result=post({'question':'Вопрос','settings':{'format':format}},provider=transport(calls=calls)).json()
 requests=chats(calls)
 assert all(row['think'] is False and 'format' not in row['options'] for row in requests)
 assert 'format' not in OPTION_KEYS
 assert all('format' not in row for row in requests[:3])
 if format is None: assert 'format' not in requests[3]
 else: assert requests[3]['format']==format
 assert result['settings']['format']==result['optimized']['parameters']['format']==format
 assert result['baseline']['parameters']['format'] is None
 assert 'think' not in result['settings'] and 'think' not in result['optimized']['parameters']
 assert profile('baseline',{'format':SCHEMA})['parameters']['format'] is None
@pytest.mark.parametrize('invalid',[True,False,0,1,[],{},'text','',{'$ref':'#/$defs/x'},{'properties':{'x':{'$ref':'https://example.com'}}},{'description':'a'*5000},{'description':'😀'*3100},{'enum':list(range(256))}])
def test_invalid_format_rejected_before_provider(invalid):
 with pytest.raises(ValidationError): Settings(format=invalid)
 calls=[]
 assert post({'question':'Вопрос','settings':{'format':invalid}},provider=transport(calls=calls)).status_code==422
 assert calls==[]
def test_depth_and_json_types():
 value={'type':'string'}
 for _ in range(6): value={'items':value}
 Settings(format=value)
 with pytest.raises(ValidationError): Settings(format={'items':value})
 for invalid in [{'x':float('nan')},{'x':object()}]:
  with pytest.raises(ValidationError): Settings(format=invalid)
def test_independent_schema_snapshots_and_legacy():
 source=copy.deepcopy(SCHEMA); settings=Settings(format=source); snapshot=profile('optimized',settings.model_dump())
 source['properties']['answer']['type']='integer';settings.format['properties']['answer']['type']='boolean'
 assert snapshot['parameters']['format']==SCHEMA
 native=payload(snapshot['parameters'],[{'role':'user','content':'Q'}]);native['format']['type']='array'
 assert snapshot['parameters']['format']==SCHEMA
 legacy=copy.deepcopy(OPTIMIZED);old=copy.deepcopy(legacy)
 assert 'format' not in payload(legacy,[{'role':'user','content':'Q'}]);assert legacy==old
 for think in [True,False]:
  calls=[]
  assert post({'question':'Q','settings':{'think':think}},provider=transport(calls=calls)).status_code==422
  assert calls==[]
