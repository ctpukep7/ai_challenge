import test from 'node:test'
import assert from 'node:assert/strict'
import {createChatObserver} from '../src/chatObserver.js'
const detail=(id,messages=[],goal='')=>({session:{id},messages,task_state:{goal}})
function recorder(api){const seen={sessions:[],runs:[],states:[]};return {...seen,observer:createChatObserver({api,onSession:d=>seen.sessions.push(d),onRun:d=>seen.runs.push(d),onState:d=>seen.states.push(d)})}}
test('accepted job ID opens session immediately; polling exposes pending, answer, memory and final completion',async()=>{
 let phase='answering';let posts=0
 const r=recorder(async(path,body)=>{
  if(path==='/scenarios/run'){posts++;assert.equal(body.scenario_id,'history');return {id:'job'}}
  if(path==='/runs/job')return {id:'job',session_id:'chat',status:phase==='completed'?'completed':'running',phase}
  if(path==='/state')return {active_run:phase==='completed'?null:{id:'job',session_id:'chat',status:'running'},sessions:[{id:'chat'}]}
  if(path==='/sessions/chat')return detail('chat',[{id:'m',question:'Question',status:phase==='answering'?'pending':'answered',...(phase==='answering'?{}:{result:{answer:phase==='completed'?'Final answer':'Answer'}})}],phase==='answering'?'':'Remembered goal')
  throw Error(path)
 })
 await r.observer.start({scenario_id:'history'})
 assert.equal(r.sessions.at(-1).session.id,'chat');assert.equal(r.sessions.at(-1).messages[0].status,'pending')
 phase='waiting';await r.observer.refresh();assert.equal(r.sessions.at(-1).messages[0].result.answer,'Answer');assert.equal(r.sessions.at(-1).task_state.goal,'Remembered goal')
 phase='completed';await r.observer.refresh();assert.equal(r.runs.at(-1).status,'completed');assert.equal(r.sessions.at(-1).messages[0].result.answer,'Final answer');assert.equal(posts,1)
})
test('partial job response and active state are fetched without reposting after reload',async()=>{
 let posts=0
 const r=recorder(async path=>{if(path==='/state')return {active_run:{id:'j',status:'running',session_id:'s'}};if(path==='/runs/j')return {id:'j',status:'running',session_id:'s'};if(path==='/sessions/s')return detail('s');posts++;throw Error(path)})
 await r.observer.refresh();assert.equal(r.sessions.at(-1).session.id,'s');assert.equal(posts,0)
})
test('manual pending question receives updates without an active scenario',async()=>{
 let pending=true
 const r=recorder(async path=>path==='/state'?{active_run:null}:detail('my',[{id:'m',status:pending?'pending':'answered'}]))
 await r.observer.select('my');await r.observer.refresh();assert.equal(r.sessions.at(-1).messages[0].status,'pending');pending=false;await r.observer.refresh();assert.equal(r.sessions.at(-1).messages[0].status,'answered')
})
test('late response from old selection cannot replace a newly selected session',async()=>{
 let release;const old=new Promise(resolve=>{release=resolve})
 const r=recorder(async path=>path==='/sessions/old'?old:detail('new'))
 const first=r.observer.select('old');await r.observer.select('new');release(detail('old'));await first
 assert.deepEqual(r.sessions.map(s=>s.session.id),['new'])
})
test('a selected session deleted elsewhere is cleared; late stale 404 cannot clear a new session',async()=>{
 const missing=Object.assign(new Error('Missing'),{status:404})
 const r=recorder(async()=>{throw missing});await r.observer.select('gone');assert.equal(r.sessions.at(-1),null)
 let reject;const stale=new Promise((_,r)=>{reject=r});const q=recorder(async path=>path==='/sessions/old'?stale:detail('new'))
 const pending=q.observer.select('old');await q.observer.select('new');reject(missing);await pending;assert.equal(q.sessions.at(-1).session.id,'new')
})
test('a late old run poll cannot overwrite a newly started run',async()=>{
 let release,oldPoll=false
 const stale=new Promise(resolve=>{release=resolve})
 const r=recorder(async(path)=>{
  if(path==='/state')return {active_run:{id:'old',status:'running',session_id:'old-chat'}}
  if(path==='/runs/old')return oldPoll?stale:{id:'old',status:'running',session_id:'old-chat'}
  if(path==='/scenarios/run')return {id:'new'}
  if(path==='/runs/new')return {id:'new',status:'running',session_id:'new-chat'}
  if(path.startsWith('/sessions/'))return detail(path.split('/').at(-1))
  throw Error(path)
 })
 await r.observer.refresh();oldPoll=true;const polling=r.observer.refresh();await Promise.resolve();await r.observer.start({scenario_id:'history'});release({id:'old',status:'completed',session_id:'old-chat'});await polling
 assert.equal(r.runs.at(-1).id,'new');assert.equal(r.sessions.at(-1).session.id,'new-chat')
})
