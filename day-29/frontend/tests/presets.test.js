import test from 'node:test';
import assert from 'node:assert/strict';
import { reactive } from 'vue';
import { PRESETS, selectPreset, beginPresetQuestion, finishPresetQuestion, qualityChecks, metricDeltas } from '../src/presets.js';
import { DEFAULT_SETTINGS, BASELINE_SETTINGS, cloneSettings, createState, createSettingsEditor, requestPayload, settingsError } from '../src/optimization-state.js';
const answer = (value, changes = {}) => ({ status: 'answered', called: true, truncated: false, context_warning: null, answer: value, ...changes });
const snapshot = id => { const p=PRESETS.find(item=>item.id===id);return beginPresetQuestion({...createState(),draft:p.question},p.settings,'compare',id).pending.preset; };
const sql = snapshot('sql-cards-16'), triage = snapshot('billing-triage');
const topics = ['SELECT','WHERE','ORDER BY','LIMIT','DISTINCT','COUNT','SUM','GROUP BY','HAVING','INNER JOIN','LEFT JOIN','NULL','UNION','EXISTS','CASE','подзапрос'];
const cards = () => ({cards:topics.map((topic,index)=>({number:index+1,topic,explanation:'Короткое определение темы. Второе предложение с уточнением.',exercise:'Напишите запрос по этой теме.'}))});
const ticket = () => ({queue:'billing',order_id:'7318',amount_rub:2490,date:null,requested_action:'Вернуть только второе списание',cancel_order:false});
const checks = (value,preset) => qualityChecks(answer(JSON.stringify(value)),preset.question,preset);
const deterministic = rows => rows.filter(row=>row.status!=='manual');
const provider = (profile, settings) => { const {system,...parameters}=cloneSettings(settings); return {...answer('Ответ'),profile,label:profile,model:'qwen3',system,parameters:{...parameters,keep_alive:'1m'},reason:'answered',metrics:null,resources:null}; };
test('two new presets fill forms locally and preserve history/defaults/catalog',()=>{
 assert.equal(PRESETS.length,2);assert.deepEqual(PRESETS.map(p=>p.id),['sql-cards-16','billing-triage']);
 const state={...createState(),results:[{question:'previous'}]},editor=createSettingsEditor();
 for(const p of PRESETS){const chosen=selectPreset(state,editor,p.id);assert.equal(settingsError(chosen.editor.draft),'');assert.equal(chosen.chat.pending,null);assert.equal(chosen.chat.results,state.results);assert.equal(chosen.chat.draft,p.question);assert.equal(state.draft,'');assert.equal(editor.draft,null);chosen.editor.draft.stop.push('changed');chosen.editor.draft.system='changed';assert.deepEqual(p.settings.stop,[]);assert.notEqual(p.settings.system,'changed');assert.equal(chosen.formatMode,'schema');assert.deepEqual(JSON.parse(chosen.schemaText),p.settings.format);}
 const chosen=selectPreset(state,editor,'billing-triage');chosen.editor.draft.format.properties.order_id.type[0]='boolean';assert.equal(PRESETS[1].settings.format.properties.order_id.type[0],'string');
 assert.equal(DEFAULT_SETTINGS.system,'');assert.equal(DEFAULT_SETTINGS.seed,-1);assert.equal(BASELINE_SETTINGS.temperature,.7);assert.throws(()=>PRESETS[0].settings.format.required.push('extra'));
 assert.equal(selectPreset(state,editor,'support'),null);assert.equal(selectPreset(beginPresetQuestion({...state,draft:PRESETS[0].question},PRESETS[0].settings,'compare','sql-cards-16'),editor,'billing-triage'),null);
});
test('both pending/result snapshots freeze nested metadata and never add API fields',()=>{
 for(const p of PRESETS){
  const chosen=selectPreset(createState(),createSettingsEditor(),p.id),state=beginPresetQuestion(reactive(chosen.chat),reactive(chosen.editor.draft),'compare',p.id),payload=requestPayload(state);
  assert.deepEqual(Object.keys(payload).sort(),['question','settings']);assert.equal(Object.hasOwn(payload.settings,'preset'),false);
  const result={question:state.pending.question,mode:'compare',settings:cloneSettings(state.pending.settings),order:['baseline','optimized'],comparison:{same_input:true,input_sha256:'a'.repeat(64)},baseline:provider('baseline',BASELINE_SETTINGS),optimized:provider('optimized',state.pending.settings)};
  const done=finishPresetQuestion(state,result);chosen.editor.draft.format.properties={changed:{type:'boolean'}};chosen.chat.draft='new';result.optimized.answer='changed';
  assert.equal(done.results[0].preset.question,p.question);assert.deepEqual(done.results[0].settings.format,p.settings.format);assert.equal(done.results[0].optimized.answer,'Ответ');assert.throws(()=>state.pending.preset.criteria.push('mutated'));
  if(p.id==='sql-cards-16')assert.throws(()=>done.results[0].preset.expected.topics.push('DROP'));
  else assert.throws(()=>{done.results[0].preset.expected.queue='returns';});
 }
});
test('SQL full document passes structural checks and leaves SQL correctness/sentence count manual',()=>{
 const rows=checks(cards(),sql);assert.ok(deterministic(rows).every(row=>row.status==='pass'));assert.equal(rows.filter(row=>row.status==='manual').length,2);
 const invented=cards();invented.cards[0].explanation='Неправильное определение.';
 assert.ok(deterministic(checks(invented,sql)).every(row=>row.status==='pass'),'parser never certifies factual correctness');
});
test('SQL rejects incomplete documents, wrong order, duplicate themes/numbers and invalid fields/types',()=>{
 const changes=[doc=>doc.cards.pop(),doc=>doc.cards.reverse(),doc=>{doc.cards[1].topic='SELECT';},doc=>{doc.cards[1].number=1;},doc=>{delete doc.cards[0].exercise;},doc=>{doc.cards[0].number='1';},doc=>{doc.cards[0].explanation=null;},doc=>{doc.cards[0].exercise='  ';},doc=>{doc.cards[0].extra='x';},doc=>{doc.extra='x';},doc=>{doc.cards=null;}];
 for(const change of changes){const doc=cards();change(doc);assert.ok(checks(doc,sql).some(row=>row.status==='fail'));}
 for(const value of [null,[],42,'hello',{cards:[]}])assert.ok(checks(value,sql).some(row=>row.status==='fail'));
});
test('triage extracts five exact values and six fields, action meaning remains manual',()=>{
 const rows=checks(ticket(),triage);assert.ok(deterministic(rows).every(row=>row.status==='pass'));assert.equal(rows.filter(row=>row.status==='manual').length,1);
 const keyOrder=Object.fromEntries(Object.entries(ticket()).reverse());assert.ok(deterministic(checks(keyOrder,triage)).every(row=>row.status==='pass'));
 const semanticWrong={...ticket(),requested_action:'Отменить заказ'};assert.ok(deterministic(checks(semanticWrong,triage)).every(row=>row.status==='pass'));assert.equal(checks(semanticWrong,triage).find(row=>row.label==='Смысл requested_action').status,'manual');
});
test('triage rejects incorrect queue/amount/type/date/cancel, blank action and missing/extra fields',()=>{
 for(const change of [{queue:'returns'},{queue:'delivery'},{amount_rub:4980},{amount_rub:'2490'},{amount_rub:null},{order_id:7318},{date:'null'},{date:'2026-10-12'},{cancel_order:true},{cancel_order:'false'},{requested_action:''},{requested_action:'   '},{requested_action:null},{extra:'invented'}])assert.ok(checks({...ticket(),...change},triage).some(row=>row.status==='fail'));
 for(const key of Object.keys(ticket())){const doc=ticket();delete doc[key];assert.ok(checks(doc,triage).some(row=>row.status==='fail'));}
});
test('raw JSON never gets repaired and changed examples never use frozen expectations',()=>{
 for(const p of [sql,triage]){
  const good=JSON.stringify(p.id==='sql-cards-16'?cards():ticket());
  for(const value of ['```json\n'+good+'\n```','Ответ: '+good,'{broken'])assert.ok(qualityChecks(answer(value),p.question,p).every(row=>row.status==='fail'));
  for(const question of [p.question+' ',p.question+' Изменение','Свободный вопрос'])assert.deepEqual(qualityChecks(answer(good),question,p).map(row=>row.status),['manual']);
  assert.equal(qualityChecks(answer(good),p.question,null)[0].status,'manual');
 }
});
test('truncated answers fail completeness explicitly, failures/context limits cannot announce success',()=>{
 for(const p of [sql,triage]){
  const good=JSON.stringify(p.id==='sql-cards-16'?cards():ticket());
  const truncated=qualityChecks(answer(good,{truncated:true}),p.question,p);assert.equal(truncated[0].label,'Полнота ответа');assert.equal(truncated[0].status,'fail');assert.match(truncated[0].detail,/обрезан/);
  for(const change of [{status:'error'},{called:false},{context_warning:'limited'}])assert.ok(qualityChecks(answer(good,change),p.question,p).every(row=>row.status!=='pass'));
 }
});
test('metric deltas preserve real observations, zero baseline and missing values',()=>{
 const b=answer('',{metrics:{total_seconds:10,tokens_per_second:20},resources:{peak_size_bytes:2*1073741824}}),a=answer('',{metrics:{total_seconds:5,tokens_per_second:30},resources:{peak_size_bytes:1073741824}});
 const rows=metricDeltas(b,a);assert.match(rows[0].value,/10 → 5 с · Δ -5 с \(-50%\)/);assert.match(rows[1].value,/20 → 30 ток\/с · Δ \+10 ток\/с \(\+50%\)/);assert.match(rows[2].value,/2 → 1 ГиБ · Δ -1 ГиБ/);
 b.metrics.total_seconds=0;assert.match(metricDeltas(b,a)[0].value,/процент не вычисляется/);
 for(const value of [null,undefined,NaN,Infinity,-1,'3']){b.metrics.total_seconds=value;assert.equal(metricDeltas(b,a)[0].value,'Нет данных');}
 for(const absent of [null,{...b,status:'error'},{...b,called:false}])assert.ok(metricDeltas(absent,a).every(row=>row.value==='Нет данных'));
 assert.ok(metricDeltas(b,{...a,called:false}).every(row=>row.value==='Нет данных'));
});
