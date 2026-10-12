import { reactive } from 'vue';
import test from 'node:test';
import assert from 'node:assert/strict';
import { BASELINE_SETTINGS, DEFAULT_SETTINGS, createSettingsEditor, refreshSettingsEditor, resetSettingsEditor, cloneSettings, profileSettings, stopToText, stopFromText, parameterRows, beginQuestion, clearResults, createState, failQuestion, finishQuestion, questionError, requestEndpoint, requestPayload, settingsError, parseSchema, shouldSend, validResult, validService } from '../src/optimization-state.js';
const provider = (profile) => {
  const chosen = cloneSettings(profile === 'baseline' ? BASELINE_SETTINGS : DEFAULT_SETTINGS);
  const { system, ...parameters } = chosen;
  return { profile, label: profile, model: 'qwen3', parameters: { ...parameters, keep_alive: '1m' }, system, status: 'answered', answer: 'Ответ', called: true, truncated: false, reason: 'answered', context_warning: null, error: null, metrics: { total_seconds: 2 }, resources: null };
};
const response = () => ({ question: 'Вопрос?', mode: 'compare', settings: cloneSettings(DEFAULT_SETTINGS), order: ['baseline','optimized'], comparison: { same_input: true, input_sha256: 'a'.repeat(64) }, baseline: provider('baseline'), optimized: provider('optimized') });
const fixtureService = () => ({status:'ready',message:'Готов',model:'qwen3',profiles:{baseline:provider('baseline'),optimized:provider('optimized')}});
const pending = () => beginQuestion({ ...createState(), draft: ' Вопрос? ' }, { ...DEFAULT_SETTINGS });
test('settings boundaries and strict numeric types', () => { for (const temperature of [0,1,2,.2,.25]) assert.equal(settingsError({ ...DEFAULT_SETTINGS, temperature }), ''); for (const temperature of [-.1,2.1,NaN,Infinity,'0.2',true]) assert.ok(settingsError({ ...DEFAULT_SETTINGS, temperature })); });
test('only supported context and output budgets accepted', () => { for (const num_predict of [256,512,768,1024,1536,2048]) for (const num_ctx of [3072,4096,6144,8192]) assert.equal(settingsError({ ...DEFAULT_SETTINGS, num_predict, num_ctx }), ''); for (const change of [{ num_predict: 4096 }, { num_ctx: 1024 }, { debug: true }]) assert.ok(settingsError({ ...DEFAULT_SETTINGS, ...change })); });
test('question UTF8 limits and Unicode codepoint counting', () => { assert.equal(questionError('🙂'.repeat(2500)), ''); assert.ok(questionError('🙂'.repeat(2501))); assert.equal(questionError('я'.repeat(4000)), ''); assert.ok(questionError('a'.repeat(4001))); assert.ok(questionError('  ')); });
test('begin snapshots settings and preserves draft until success', () => { const original = { ...DEFAULT_SETTINGS }; const state = beginQuestion({ ...createState(), draft: 'Вопрос?' }, original); original.temperature = .9; assert.equal(state.pending.settings.temperature,0); assert.equal(state.draft,'Вопрос?'); assert.throws(() => beginQuestion(state,original)); });
test('payload is isolated from pending settings and uses chosen endpoint', () => { const state = pending(), payload = requestPayload(state); payload.settings.num_ctx = 8192; assert.equal(state.pending.settings.num_ctx,4096); assert.equal(requestEndpoint(state),'/api/compare'); assert.equal(requestEndpoint(beginQuestion({ ...createState(), draft: 'Вопрос?' }, DEFAULT_SETTINGS,'optimized')),'/api/ask'); });
test('Tab and modified Tab retain ordinary navigation without sending or replacing free text', () => {
  const state={...createState(),draft:'/6'};const before=structuredClone(state);
  for(const event of [{key:'Tab'},{key:'Tab',shiftKey:true},{key:'Tab',ctrlKey:true},{key:'Tab',isComposing:true},{key:'Tab',keyCode:229}]) assert.equal(shouldSend(event),false);
  assert.deepEqual(state,before);
  assert.equal(beginQuestion(state,DEFAULT_SETTINGS).pending.question,'/6');
});
test('Enter compares, modified Enter and IME do not send', () => { assert.equal(shouldSend({ key: 'Enter' }),true); for (const flag of ['shiftKey','ctrlKey','metaKey','altKey','isComposing']) assert.equal(shouldSend({ key: 'Enter', [flag]: true }),false); assert.equal(shouldSend({ key: 'Enter', keyCode: 229 }),false); });
test('accepts partial provider error and null metrics/resources', () => { const result = response(); result.baseline = { ...result.baseline, status: 'error', answer: 'Ошибка', error: { code: 'timeout', message: 'Истекло время' }, metrics: null }; assert.equal(validResult(result),true); assert.equal(finishQuestion(pending(),result).results.length,1); });
test('nonempty uncertainty is an ordinary answer and old RAG status is rejected', () => { const result = response(); result.optimized.answer = 'Не знаю: данных о цене нет.'; assert.equal(validResult(result),true); result.optimized.status = 'unknown'; assert.equal(validResult(result),false); });
test('successful result preserves parameters from request snapshot', () => { const state = finishQuestion(pending(),response()); assert.equal(state.draft,''); assert.equal(state.pending,null); assert.equal(state.results[0].settings.temperature,0); });
test('rejects invalid JSON-shaped contract and mismatched request results', () => { for (const change of [{ order: ['optimized','optimized'] }, { mode: 'cloud' }, { comparison: { same_input: false } }, { optimized: null }, { comparison: { same_input: true, input_sha256: 'broken' } }]) assert.equal(validResult({ ...response(), ...change }),false); const wrong = response(); wrong.settings.temperature = .9; wrong.optimized.parameters.temperature = .9; assert.throws(() => finishQuestion(pending(),wrong)); assert.throws(() => finishQuestion(pending(),{ ...response(), question: 'Другой вопрос' })); });
test('failed request retains draft and selected settings for retry', () => { const state = failQuestion(pending(),'Таймаут'); assert.equal(state.draft,'Вопрос?'); assert.equal(state.error,'Таймаут'); assert.equal(state.pending,null); assert.equal(beginQuestion(state,DEFAULT_SETTINGS).pending.question,'Вопрос?'); });
test('clear history only while idle', () => { const busy = pending(); assert.equal(clearResults(busy),busy); assert.deepEqual(clearResults({ ...createState(),results:[response()] }),createState()); });
test('service state needs settings and readiness without a question catalog', () => { const service=fixtureService(); assert.equal(Object.hasOwn(service,'questions'),false); assert.equal(validService(service),true); assert.equal(validService({...service,profiles:null}),false);service.profiles.optimized.parameters.seed='42';assert.equal(validService(service),false); });

test('rejects malformed metrics and modified baseline', () => { const wrong = response(); wrong.baseline.parameters.temperature = .2; assert.throws(() => finishQuestion(pending(),wrong)); const malformed = response(); malformed.optimized.metrics = 'broken'; assert.equal(validResult(malformed),false); });

test('advanced range boundaries reject strings, bools, fractions and non-finite values', () => {
  const ranges = { top_k: [0,100,true,-1,101,1.1], top_p: [0,1,'0.8',-.1,1.1,NaN], min_p: [0,1,false,-.1,1.1,Infinity], repeat_penalty: [.8,1.5,'1',.79,1.51,NaN], repeat_last_n: [0,2048,true,-1,2049,1.2], seed: [-1,2147483647,'7',-2,2147483648,1.3] };
  for (const [key, [min,max,...invalid]] of Object.entries(ranges)) { for (const value of [min,max]) assert.equal(settingsError({ ...DEFAULT_SETTINGS, [key]:value }), ''); for (const value of invalid) assert.ok(settingsError({ ...DEFAULT_SETTINGS,[key]:value }),`${key} ${value}`); }
});
test('stop editor preserves spaces, rejects duplicate empty and excessive phrases', () => {
  assert.deepEqual(stopFromText(''),[]); assert.deepEqual(stopFromText(' end ' + String.fromCharCode(10) + 'finish'), [' end ', 'finish']);
  for (const stop of [[''], ['a','a'], ['a','b','c','d','e'], ['x'.repeat(81)], ['🙂'.repeat(65)], 'text']) assert.ok(settingsError({ ...DEFAULT_SETTINGS,stop }));
  for (const stop of [[], [' '], ['a',' a'], ['🙂'.repeat(64)], ['x'.repeat(80)]]) assert.equal(settingsError({ ...DEFAULT_SETTINGS,stop }), '');
});
test('custom system supports empty, Unicode limits and preserves exact instructions', () => {
  for (const system of ['', ' Соблюдай мой формат. ', 'я'.repeat(3000), '🙂'.repeat(2000)]) assert.equal(settingsError({ ...DEFAULT_SETTINGS,system }), '');
  for (const system of ['a'.repeat(3001),'🙂'.repeat(2001),null,true]) assert.ok(settingsError({ ...DEFAULT_SETTINGS,system }));
  const settings = { ...DEFAULT_SETTINGS,system:'  Своя инструкция  ' }, state = beginQuestion({ ...createState(),draft:'Вопрос?' },settings);
  assert.equal(requestPayload(state).settings.system,settings.system); assert.equal(requestPayload(state).question,'Вопрос?');
});
test('server profile reset clones the complete recommended settings without mutating state', () => {
  const server = provider('optimized'); server.parameters.stop = ['done']; server.parameters.seed = 123; server.system = 'Серверная инструкция';
  const restored = profileSettings(server); assert.equal(restored.seed,123); assert.equal(restored.system,'Серверная инструкция'); restored.stop.push('edited'); assert.deepEqual(server.parameters.stop,['done']); assert.equal(Object.hasOwn(restored,'keep_alive'),false);
});
test('pending and request snapshots deeply isolate stop arrays and system text', () => {
  const settings = { ...DEFAULT_SETTINGS,stop:['finish'],system:'Проверяй условия' }, state = beginQuestion({ ...createState(),draft:'Вопрос?' },settings);
  settings.stop.push('external'); settings.system = 'Изменили'; const payload = requestPayload(state); payload.settings.stop.push('payload');
  assert.deepEqual(state.pending.settings.stop,['finish']); assert.equal(state.pending.settings.system,'Проверяй условия');
});
test('finished cards retain immutable full snapshots after input and response mutations', () => {
  const settings = { ...DEFAULT_SETTINGS,stop:['end'],system:'Своя инструкция',seed:42 }, pending = beginQuestion({ ...createState(),draft:'Вопрос?' },settings);
  const result = response(); result.settings = cloneSettings(settings); result.optimized.parameters = { ...cloneSettings(settings),keep_alive:'1m' }; delete result.optimized.parameters.system; result.optimized.system = settings.system;
  const done = finishQuestion(pending,result); result.settings.stop.push('response mutation'); result.optimized.parameters.stop.push('provider mutation'); settings.stop.push('form mutation');
  assert.deepEqual(done.results[0].settings.stop,['end']); assert.deepEqual(done.results[0].optimized.parameters.stop,['end']); assert.equal(done.results[0].optimized.system,'Своя инструкция');
});
test('response mismatching seed stop system or baseline advanced option is rejected', () => {
  for (const update of [{ seed:43 },{ stop:['end'] },{ system:'Changed' }]) { const result = response(); Object.assign(result.settings,update); if (update.system !== undefined) result.optimized.system = update.system; else Object.assign(result.optimized.parameters,update); assert.throws(() => finishQuestion(pending(),result)); }
  const result = response(); result.baseline.parameters.top_k = 21; assert.throws(() => finishQuestion(pending(),result));
});
test('frontend requires full current state and renders all readonly parameters', () => {
  const service = fixtureService();
  delete service.profiles.optimized.parameters.seed; assert.equal(validService(service),false);
  const rows = parameterRows({ ...DEFAULT_SETTINGS,stop:[' end '] }); assert.equal(rows.length,12); assert.equal(rows.find(row => row.key === 'stop').value,'[" end "]');
});

test('snapshot rows retain exact custom decimal precision', () => { const rows = parameterRows({ ...DEFAULT_SETTINGS,top_p:.123456789012 }); assert.equal(rows.find(row => row.key === 'top_p').value,'0,123456789012'); });

test('baseline stays frozen and independent from the selected candidate', () => { assert.equal(BASELINE_SETTINGS.seed,-1); assert.equal(BASELINE_SETTINGS.temperature,.7); assert.equal(BASELINE_SETTINGS.num_predict,1024); assert.equal(DEFAULT_SETTINGS.seed,-1); assert.equal(DEFAULT_SETTINGS.temperature,0); assert.equal(DEFAULT_SETTINGS.num_predict,1024); const editable = cloneSettings(DEFAULT_SETTINGS); editable.seed = 99; editable.stop.push('end'); assert.equal(BASELINE_SETTINGS.seed,-1); assert.deepEqual(BASELINE_SETTINGS.stop,[]); assert.throws(() => { BASELINE_SETTINGS.seed = 42; }); assert.throws(() => BASELINE_SETTINGS.stop.push('end')); });

test('stop codec roundtrips ordinary phrases, multiline phrases and multiple stops', () => {
  for (const stops of [[], [' end '], ['\n\n(', '\n\nПримечание:'], ['строка\r\nконец', ' finish ']]) {
    assert.deepEqual(stopFromText(stopToText(stops)),stops);
    assert.equal(settingsError({...DEFAULT_SETTINGS,stop:stopFromText(stopToText(stops))}),'');
  }
  assert.equal(stopToText(['\n\n(', '\n\nПримечание:']), '\\n\\n(\n\\n\\nПримечание:');
});
test('stop codec distinguishes an actual newline from literal backslash-n', () => {
  assert.equal(stopToText(['\n']),'\\n');
  assert.equal(stopToText(['\\n']),'\\\\n');
  assert.deepEqual(stopFromText('\\n'),['\n']);
  assert.deepEqual(stopFromText('\\\\n'),['\\n']);
  assert.deepEqual(stopFromText(stopToText(['\\n','\\','\r'])),['\\n','\\','\r']);
});
test('stop decoder preserves unknown escapes, spaces and incomplete escapes', () => {
  assert.deepEqual(stopFromText(' \\t\\x конец\\'),[' \\t\\x конец\\']);
  assert.deepEqual(stopFromText('\\'),['\\']);
  assert.deepEqual(stopFromText('\\n'),['\n']);
});
test('a trailing physical empty line remains an invalid empty stop', () => {
  const stops=stopFromText('end\n');
  assert.deepEqual(stops,['end','']);
  assert.ok(settingsError({...DEFAULT_SETTINGS,stop:stops}));
  assert.deepEqual(stopFromText(''),[]);
});


test('presence penalty validates finite numbers only and rejects unsupported native repeat window', () => {
  for (const value of [0,.25,1.5,2]) assert.equal(settingsError({...DEFAULT_SETTINGS,presence_penalty:value}),'');
  for (const value of [-.01,2.01,true,false,'1.5',null,NaN,Infinity,-Infinity]) assert.ok(settingsError({...DEFAULT_SETTINGS,presence_penalty:value}));
  assert.ok(settingsError({...DEFAULT_SETTINGS,repeat_last_n:-1}));
  const legacy=cloneSettings(DEFAULT_SETTINGS); delete legacy.presence_penalty;
  assert.equal(settingsError(legacy),'');
  assert.ok(settingsError({...legacy,presence_penalty:undefined}));
});
test('legacy results validate presence as zero without rewriting immutable snapshots or display', () => {
  const result=response(); delete result.settings.presence_penalty; delete result.optimized.parameters.presence_penalty; delete result.baseline.parameters.presence_penalty;
  const before=structuredClone(result); assert.equal(validResult(result),true);
  assert.equal(profileSettings(result.optimized).presence_penalty,0);
  assert.equal(parameterRows(result.optimized.parameters).length,12);
  assert.equal(parameterRows(result.optimized.parameters).find(row=>row.key==='presence_penalty').value,'Не предоставлено');
  const finished=finishQuestion(pending(),result);
  assert.deepEqual(result,before); assert.deepEqual(finished.results[0],before);
  assert.equal(Object.hasOwn(finished.results[0].settings,'presence_penalty'),false);
  assert.equal(Object.hasOwn(finished.results[0].optimized.parameters,'presence_penalty'),false);
  const high=beginQuestion({...createState(),draft:'Вопрос?'},{...DEFAULT_SETTINGS,presence_penalty:1.5});
  assert.throws(()=>finishQuestion(high,result));
  result.optimized.parameters.presence_penalty=null; assert.equal(validResult(result),false);
});


test('single editable settings initialize once refresh preserves edits and reset clones current defaults', () => {
  const server=fixtureService();let editor=refreshSettingsEditor(createSettingsEditor(),server);
  editor.draft.system='Моя инструкция';editor.draft.stop.push('end');editor.draft.presence_penalty=.75;
  assert.deepEqual(server.profiles.optimized.parameters.stop,[]);
  const update=fixtureService();update.profiles.optimized.parameters.seed=123;
  assert.equal(refreshSettingsEditor(editor,update),editor);assert.equal(editor.draft.seed,-1);
  assert.equal(resetSettingsEditor(editor,update,true),editor);
  const reset=resetSettingsEditor(editor,update);assert.equal(reset.draft.seed,123);assert.equal(reset.draft.presence_penalty,0);
  reset.draft.stop.push('edited');assert.deepEqual(update.profiles.optimized.parameters.stop,[]);
});
test('free input preserves literal content and freezes settings without presets or automatic requests', () => {
  const editor=refreshSettingsEditor(createSettingsEditor(),fixtureService());editor.draft.presence_penalty=1.5;
  const history=[response()];const chat={...createState(),draft:'/6\nМой собственный вопрос',results:history};
  const started=beginQuestion(chat,editor.draft);const payload=requestPayload(started);
  assert.equal(payload.question,chat.draft);assert.deepEqual(Object.keys(payload).sort(),['question','settings']);
  assert.equal(chat.pending,null);assert.equal(started.results,history);assert.equal(payload.settings.presence_penalty,1.5);
  payload.settings.stop.push('payload');editor.draft.system='Изменение после отправки';
  assert.deepEqual(started.pending.settings.stop,[]);assert.equal(started.pending.settings.system,'');
  assert.equal(clearResults(started),started);
});


test('empty SYSTEM and random seed initialize and reset while explicit user edits remain usable', () => {
  const service=fixtureService();let editor=refreshSettingsEditor(createSettingsEditor(),service);
  assert.equal(DEFAULT_SETTINGS.system,'');assert.equal(DEFAULT_SETTINGS.seed,-1);
  assert.equal(editor.draft.system,'');assert.equal(editor.draft.seed,-1);
  editor.draft.system='Моя явная инструкция';editor.draft.seed=42;
  const pending=beginQuestion({...createState(),draft:'Вопрос?'},editor.draft);
  assert.equal(requestPayload(pending).settings.system,'Моя явная инструкция');assert.equal(requestPayload(pending).settings.seed,42);
  editor=resetSettingsEditor(editor,service);assert.equal(editor.draft.system,'');assert.equal(editor.draft.seed,-1);
  assert.equal(pending.pending.settings.system,'Моя явная инструкция');assert.equal(pending.pending.settings.seed,42);
  assert.equal(profileSettings(service.profiles.baseline).seed,-1);assert.equal(profileSettings(service.profiles.baseline).system,'');
});


test('format validates supported modes, JSON, depth, size and refs', () => {
 for(const format of [null,'json',{type:'object'}]) assert.equal(settingsError({...DEFAULT_SETTINGS,format}),'');
 for(const format of [true,false,0,'text',[],{},{$ref:'#/x'},{properties:{x:{$ref:'https://example.com'}}},{description:'a'.repeat(5000)},{description:'😀'.repeat(3100)},{enum:Array.from({length:256},(_,i)=>i)}]) assert.ok(settingsError({...DEFAULT_SETTINGS,format}));
 for(const text of ['', '{broken','null','[]','{}','"json"']) assert.ok(parseSchema(text).error);
 assert.equal(parseSchema('{"type":"object"}').error,'');assert.ok(settingsError({...DEFAULT_SETTINGS,think:false}));
 let schema={type:'string'};for(let i=0;i<6;i++)schema={items:schema};assert.equal(settingsError({...DEFAULT_SETTINGS,format:schema}),'');assert.ok(settingsError({...DEFAULT_SETTINGS,format:{items:schema}}));
});
test('Vue reactive settings clone nested schema and compare key order independently', () => {
 const settings=reactive({...DEFAULT_SETTINGS,format:{type:'object',properties:{answer:{type:'string'}}}});
 const state=beginQuestion({...createState(),draft:'Вопрос?'},settings);settings.format.properties.answer.type='integer';
 assert.equal(state.pending.settings.format.properties.answer.type,'string');
 const payload=requestPayload(state);payload.settings.format.properties.answer.type='boolean';assert.equal(state.pending.settings.format.properties.answer.type,'string');
 const result=response();result.settings.format={properties:{answer:{type:'string'}},type:'object'};result.optimized.parameters.format=structuredClone(result.settings.format);
 const done=finishQuestion(state,result);result.optimized.parameters.format.properties.answer.type='number';assert.equal(done.results[0].optimized.parameters.format.properties.answer.type,'string');
 assert.throws(()=>finishQuestion(state,response()));
 assert.equal(parameterRows(done.results[0].optimized.parameters).find(row=>row.key==='format').value,JSON.stringify(done.results[0].optimized.parameters.format));
});
test('format refresh preserves edit, reset and pending locks apply', () => {
 const service=fixtureService();let editor=refreshSettingsEditor(createSettingsEditor(),service);editor.draft.format={type:'object'};
 assert.equal(refreshSettingsEditor(editor,service),editor);assert.equal(resetSettingsEditor(editor,service,true),editor);
 editor=resetSettingsEditor(editor,service);assert.equal(editor.draft.format,null);
});
test('legacy absent format is text without rewriting saved results', () => {
 const result=response();delete result.settings.format;delete result.optimized.parameters.format;delete result.baseline.parameters.format;result.baseline.parameters.think=false;
 const original=JSON.stringify(result),state=finishQuestion(pending(),result);assert.equal(validResult(result),true);
 assert.equal(profileSettings(result.optimized).format,null);assert.equal(parameterRows(result.optimized.parameters).find(row=>row.key==='format').value,'Обычный текст');
 assert.equal(Object.hasOwn(state.results[0].settings,'format'),false);assert.equal(JSON.stringify(result),original);
 const broken=response();broken.baseline.parameters.format='json';assert.equal(validResult(broken),false);
});
