import test from 'node:test';import assert from 'node:assert/strict';import {nextHistory} from './session.js';
test('four complete pairs only',()=>{let h=[];for(let i=0;i<8;i++)h=nextHistory(h,String(i),'a');assert.equal(h.length,8);assert.equal(h[0].content,'4');assert.equal(h[0].role,'user');assert.equal(h.at(-1).role,'assistant')});

import {groupSources, toolSteps, argumentsLabel} from './session.js';
import {App} from './app.js';
import {createSSRApp} from 'vue';
import {renderToString} from '@vue/server-renderer';

test('group by evidence ID, retain every distinct quote and distinct ranges',()=>{
  const sources=[{id:'e1',kind:'documentation',path:'README.md',start_line:1,quote:'one'},
    {id:'e1',kind:'documentation',path:'README.md',start_line:1,quote:'two'},
    {id:'e1',kind:'documentation',path:'README.md',start_line:1,quote:'one'},
    {id:'e2',kind:'documentation',path:'README.md',start_line:5,quote:'three'},
    {id:'e3',kind:'code',path:'a.py',quote:'four'}];
  const grouped=groupSources(sources);
  assert.equal(grouped.length,3);assert.deepEqual(grouped[0].quotes,['one','two']);
  assert.equal(grouped[1].start_line,5);assert.equal(grouped[2].kind,'code');
  assert.equal(sources[0].quotes,undefined);
});

test('discovery is not an executed tool and rejection is not successful MCP',()=>{
  const result=toolSteps([{method:'tools/list',tools:[]},
    {method:'tools/call',tool:'git_branch',result:{branch:'main'}},
    {method:'tool_request_rejected',tool:'unknown',result:{error:'bad args'}},
    {method:'tools/call',tool:'read_project_file',result:{error:'private'}}]);
  assert.equal(result.length,3);assert.equal(result[0].failed,false);
  assert.equal(result[1].rejected,true);assert.equal(result[2].failed,true);
  assert.equal(argumentsLabel({scope:'project',day:31}),'scope: project · day: 31');
});

test('new conversation is explicit and locked while pending',()=>{
  const state={question:'q',messages:[1],history:[2],error:'e',pending:true};
  App.methods.newConversation.call(state);assert.deepEqual(state.messages,[1]);
  state.pending=false;App.methods.newConversation.call(state);
  assert.deepEqual(state.messages,[]);assert.deepEqual(state.history,[]);assert.equal(state.question,'');
});

test('error response retains visible trace but never enters history',async()=>{
  const state={...App.data(),question:'q',request:async()=>({status:'error',answer:'failure',trace:[{method:'tools/list'}]})};
  await App.methods.send.call(state);
  assert.deepEqual(state.history,[]);assert.equal(state.messages[1].trace.length,1);assert.equal(state.pending,false);
});

test('render readable tool steps, grouped quotes, escaped content and separate technical trace',async()=>{
  const app={...App,data:()=>({...App.data(),messages:[{role:'assistant',answer:'<script>bad</script>',
    sources:[{id:'e1',kind:'code',path:'a.py',text:'text',quote:'one'},{id:'e1',kind:'code',path:'a.py',text:'text',quote:'two'}],
    suggestions:['Follow up'],trace:[{method:'tools/list',tools:[{function:{name:'git_branch'}}]},
      {method:'tools/call',tool_call_id:'c1',tool:'git_branch',seconds:.2,result:{branch:'main',sha:'abc'},arguments:{}}]}]})};
  const html=await renderToString(createSSRApp(app));
  assert.match(html,/Подтверждённые источники · 1/);assert.match(html,/<blockquote>one<\/blockquote>/);assert.match(html,/<blockquote>two<\/blockquote>/);
  assert.match(html,/Текущая ветка/);assert.match(html,/Выполнен/);assert.match(html,/Результат инструмента/);
  assert.match(html,/Каталог MCP · 1 инструментов/);assert.match(html,/Это не выполнение инструмента/);assert.match(html,/Технический JSON трассы/);
  assert.match(html,/подсказки интерфейса/);assert.match(html,/&lt;script&gt;bad&lt;\/script&gt;/);assert.doesNotMatch(html,/<script>bad/);
});

test('never merge conflicting provenance even if malformed response repeats ID',()=>{
  const result=groupSources([{id:'e1',kind:'code',path:'a.py',start_line:1,quote:'a'},
    {id:'e1',kind:'code',path:'a.py',start_line:5,quote:'b'},
    {id:'e1',kind:'documentation',path:'a.py',start_line:1,quote:'c'}]);
  assert.equal(result.length,3);
});

test('partial response is visible but never enters conversation history',async()=>{
  const initial=[{role:'user',content:'old'},{role:'assistant',content:'ok'}];
  const state={...App.data(),history:initial,question:'q',request:async()=>({status:'answered',partial:true,answer:'verified only',validation:{repair_attempted:true,repaired:false,omitted_claims:1},trace:[{method:'answer/validation',status:'partial'}]})};
  await App.methods.send.call(state);
  assert.deepEqual(state.history,initial);assert.equal(state.messages[1].partial,true);
  assert.equal(toolSteps(state.messages[1].trace).length,0);
});

test('fully repaired answer enters history, unknown partial does not',async()=>{
  for(const [data,expected] of [
    [{status:'answered',answer:'verified',validation:{repair_attempted:true,repaired:true}},2],
    [{status:'unknown',partial:true,answer:'Не знаю',validation:{repair_attempted:true,repaired:false}},0],
  ]){
    const state={...App.data(),question:'q',request:async()=>data};
    await App.methods.send.call(state);assert.equal(state.history.length,expected);
  }
});

test('validation outcomes are visible and never counted as MCP calls',async()=>{
  const app={...App,data:()=>({...App.data(),messages:[
    {role:'assistant',status:'answered',answer:'verified',validation:{repair_attempted:true,repaired:true},metrics:{llm_calls:3,tool_calls:1},trace:[{method:'answer/validation',status:'repaired'}]},
    {role:'assistant',status:'answered',partial:true,answer:'safe <b>text</b>',validation:{repair_attempted:true,repaired:false}},
    {role:'assistant',status:'unknown',partial:true,answer:'Не знаю'},
  ]})};
  const html=await renderToString(createSSRApp(app));
  assert.match(html,/Цитирование исправлено после проверки/);
  assert.match(html,/Выполнена попытка исправления цитирования/);
  assert.match(html,/Частичный ответ · неподтверждённые части не показаны/);
  assert.match(html,/Недостаточно данных/);assert.match(html,/LLM: 3 · MCP: 1/);
  assert.match(html,/safe &lt;b&gt;text&lt;\/b&gt;/);assert.doesNotMatch(html,/Выполнен ·/);
});
