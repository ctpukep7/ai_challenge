import test from 'node:test';
import assert from 'node:assert/strict';
import {initialSession,clearSession,requestHistory,appendAnswer,errorMessage,statistics,apiRequest,failureMessage,serviceLimits} from '../src/chat-state.js';
test('logout and 401 reset clear token, limits and history',()=>{assert.deepEqual(clearSession(),{token:'',limits:null,history:[]});assert.notEqual(clearSession(),initialSession());});
test('last four complete pairs sent without metadata; existing array unchanged',()=>{const history=Array.from({length:6},(_,i)=>({user:String(i),assistant:`a${i}`,response:{model:'local'}}));assert.deepEqual(requestHistory(history),history.slice(2).map(({user,assistant})=>({user,assistant})));assert.equal(history.length,6);});
test('success appends without changing previous history',()=>{const history=[{user:'first',assistant:'reply'}];const response={answer:'second reply',model:'local'};assert.deepEqual(appendAnswer(history,'second',response)[1],{user:'second',assistant:'second reply',response});assert.equal(history.length,1);});
test('initial history empty',()=>assert.deepEqual(requestHistory([]),[]));
test('rate limit shows Retry-After, common errors clear',()=>{assert.match(errorMessage(429,{},'59'),/59 с/);assert.match(errorMessage(429,{},null),/минуту/);assert.match(errorMessage(409,{}),/занята/);assert.match(errorMessage(413,{}),/Контекст/);assert.match(errorMessage(401,{}),/Войдите/);});
test('structured errors and fallback',()=>{assert.equal(errorMessage(502,{error:{message:'Ошибка модели'}}),'Ошибка модели');assert.match(errorMessage(502,{}),/не смог/);});
test('seven statistics retain zero and distinguish missing',()=>{const rows=statistics({total_seconds:0,load_seconds:null,generation_seconds:0.005,prompt_tokens:10,output_tokens:0,total_tokens:10});assert.equal(rows.length,7);assert.equal(rows[0][1],'0 с');assert.equal(rows[1][1],'Нет данных');assert.equal(rows[2][1],'< 0,01 с');assert.equal(rows[4][1],'0');assert.equal(rows[6][1],'Нет данных');});
test('invalid numbers not presented as metrics',()=>assert.ok(statistics({total_seconds:NaN,load_seconds:Infinity,generation_seconds:-1}).slice(0,3).every(([,value])=>value==='Нет данных')));
test('login sends token only in Bearer header and no body',async()=>{let call;const fetcher=async(path,options)=>{call={path,options};return new Response(JSON.stringify({model:'local'}));};assert.deepEqual(await apiRequest('/api/limits','test-token',undefined,{fetcher}),{model:'local'});assert.equal(call.path,'/api/limits');assert.equal(call.options.headers.Authorization,'Bearer test-token');assert.equal(call.options.method,'GET');assert.equal(call.options.body,undefined);assert.equal(call.options.credentials,'omit');assert.equal(call.options.cache,'no-store');assert.equal(call.options.redirect,'error');});
test('chat exact payload and signal once',async()=>{let calls=0;const payload={prompt:'Question',history:[]};const signal=new AbortController().signal;await apiRequest('/api/chat','token',payload,{signal,fetcher:async(path,options)=>{calls++;assert.equal(path,'/api/chat');assert.deepEqual(JSON.parse(options.body),payload);assert.equal(options.signal,signal);assert.equal(options.method,'POST');return new Response(JSON.stringify({answer:'Answer'}));}});assert.equal(calls,1);});
test('401 retains status for clearing session',async()=>{await assert.rejects(apiRequest('/api/limits','wrong',undefined,{fetcher:async()=>new Response(JSON.stringify({error:{message:'denied'}}),{status:401})}),error=>error.status===401&&/Войдите/.test(error.message));});
test('429 header no retry',async()=>{let calls=0;await assert.rejects(apiRequest('/api/chat','token',{}, {fetcher:async()=>{calls++;return new Response(JSON.stringify({error:{message:'limited'}}),{status:429,headers:{'Retry-After':'12'}});}}),error=>error.status===429&&/12 с/.test(error.message));assert.equal(calls,1);});
test('malformed JSON retains status',async()=>{await assert.rejects(apiRequest('/api/limits','token',undefined,{fetcher:async()=>new Response('html',{status:401})}),error=>error.status===401&&/JSON/.test(error.message));});
test('abort and transport errors no retries',async()=>{let calls=0;const error=new DOMException('Aborted','AbortError');await assert.rejects(apiRequest('/api/chat','token',{}, {fetcher:async()=>{calls++;throw error;}}),cause=>cause===error);assert.equal(calls,1);});

test('limits response contract renders every server limit',async()=>{
 const limits={model:'qwen3:4b-instruct-2507-q4_K_M',max_history_pairs:4,max_context_bytes:6000,context_window:4096,max_output_tokens:1024,rate_limit_per_minute:6,concurrency:1};
 const actual=await apiRequest('/api/limits','token',undefined,{fetcher:async()=>new Response(JSON.stringify(limits))});
 assert.deepEqual(serviceLimits(actual).map(([,value])=>value),['4 пары','6000 байт UTF-8','4096 токенов','1024 токенов','6 в минуту','1 запрос']);
});
test('network and timeout errors are Russian; structured errors preserved',()=>{
 assert.match(failureMessage(new TypeError('Failed to fetch')),/Проверьте сеть/);
 assert.match(failureMessage(new DOMException('Aborted','AbortError')),/автоматически не повторяется/);
 assert.equal(failureMessage(new Error('Модель занята')),'Модель занята');
});
