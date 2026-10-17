import test from 'node:test';
import assert from 'node:assert/strict';
import {createSession} from './session.js';
const defer=()=>{let resolve;const promise=new Promise(r=>resolve=r);return{promise,resolve}};
test('polls actual task until terminal, preserving source and trace',async()=>{
 const paths=[];let poll=0;
 const session=createSession(async(path,body)=>{paths.push([path,body]);if(body)return{id:'t1',status:'running'};return ++poll===1?{id:'t1',status:'running',trace:[{tool:'list_files'}]}:{id:'t1',status:'completed',summary:'done',sources:[{path:'README.md'}]}},async()=>{});
 session.goal='Find API';await session.start();assert.equal(poll,2);assert.equal(session.task.summary,'done');assert.equal(session.pending,false);assert.deepEqual(paths[0],['/api/tasks',{goal:'Find API'}]);
});
test('examples and empty goals do not create tasks; double submit is ignored',async()=>{
 const deferred=defer();let calls=0;const session=createSession(async()=>{calls++;return deferred.promise});
 await session.start();assert.equal(calls,0);session.goal='Inspect';const pending=session.start();await session.start();assert.equal(calls,1);session.reset();deferred.resolve({id:'old'});await pending;assert.equal(session.task,null);
});
test('reset ignores late poll from previous task',async()=>{
 const deferred=defer();let count=0;const session=createSession(async()=>++count===1?{id:'old'}:deferred.promise);
 session.goal='old';const pending=session.start();await Promise.resolve();session.reset();deferred.resolve({id:'old',status:'completed',summary:'outdated'});await pending;assert.equal(session.task,null);assert.equal(session.pending,false);
});
test('provider error and unknown remain distinct',async()=>{
 for(const status of ['error','unknown']){let call=0;const session=createSession(async()=>++call===1?{id:'x'}:{id:'x',status,error:'Provider unavailable'});session.goal='x';await session.start();assert.equal(session.task.status,status);assert.equal(session.error,status==='error'?'Provider unavailable':'')}
});
test('stale apply error stays visible and duplicate applies are blocked',async()=>{
 let calls=0;const session=createSession(async()=>{calls++;throw Error('409: исходный файл изменился')});session.task={id:'a',status:'completed',change:{diff:'diff'}};
 await session.apply();assert.match(session.error,/409/);assert.equal(session.task.applied,undefined);assert.equal(session.applying,false);assert.equal(calls,1);
});
test('successful apply is idempotent in UI and reset ignores late write response',async()=>{
 let calls=0;const session=createSession(async()=>{calls++;return{applied:true}});session.task={id:'a',status:'completed',change:{diff:'x'}};await session.apply();await session.apply();assert.equal(calls,1);assert.equal(session.task.applied,true);assert.match(session.notice,/сохранены/);
 const deferred=defer();const pendingSession=createSession(async()=>deferred.promise);pendingSession.task={id:'b',status:'completed',change:{diff:'x'}};const pending=pendingSession.apply();pendingSession.reset();deferred.resolve({applied:true});await pending;assert.equal(pendingSession.task,null);assert.equal(pendingSession.notice,'');
});
test('request errors stop polling and release controls',async()=>{
 const session=createSession(async()=>{throw Error('network unavailable')});session.goal='x';await session.start();assert.equal(session.pending,false);assert.equal(session.error,'network unavailable');
});

test('applies all plural changes through one request, including a task without legacy change',async()=>{
 let calls=0;const session=createSession(async(path)=>{calls++;assert.equal(path,'/api/tasks/multi/apply');return{applied:true}});
 session.task={id:'multi',status:'completed',changes:[{path:'a.md',diff:'a'},{path:'b.md',diff:'b'}]};
 await session.apply();await session.apply();assert.equal(calls,1);assert.equal(session.task.applied,true);assert.equal(session.task.changes.length,2);
});
test('empty plural list and no-op or failed tasks cannot apply a stale legacy change',async()=>{
 let calls=0;const session=createSession(async()=>{calls++;return{applied:true}});
 for(const task of [
  {status:'completed',changes:[],change:{diff:'old'}},
  {status:'completed',changes:[{diff:''}]},
  {status:'error',changes:[{diff:'bad'}]},
  {status:'unknown',changes:[{diff:'bad'}]},
  {status:'running',changes:[{diff:'partial'}]},
 ]){session.task={id:'x',...task};await session.apply()}
 assert.equal(calls,0);
});
test('plural apply error keeps proposals available; reset clears proposals',async()=>{
 const session=createSession(async()=>{throw Error('409: второй файл изменился')});
 session.task={id:'x',status:'completed',changes:[{diff:'a'},{diff:'b'}]};
 await session.apply();assert.match(session.error,/второй/);assert.equal(session.task.changes.length,2);assert.equal(session.applying,false);
 session.reset();assert.equal(session.task,null);assert.equal(session.error,'');
});
