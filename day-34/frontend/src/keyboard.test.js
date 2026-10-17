import test from 'node:test';
import assert from 'node:assert/strict';
import {handleGoalKeydown,handleEscape,canStart} from './keyboard.js';
function setup(overrides={}){
 let starts=0,prevented=0;
 const session={goal:'Найди API',pending:false,applying:false,start(){starts++},...overrides};
 const event={key:'Enter',ctrlKey:false,metaKey:false,shiftKey:false,altKey:false,repeat:false,isComposing:false,preventDefault(){prevented++}};
 return {session,event,get starts(){return starts},get prevented(){return prevented}};
}
function withTextarea(s, start=5, end=start){
 const inputEvents=[];
 const textarea={value:s.session.goal,selectionStart:start,selectionEnd:end,maxLength:4000,disabled:false,
  setRangeText(text,from,to,mode){assert.equal(mode,'end');this.value=this.value.slice(0,from)+text+this.value.slice(to);this.selectionStart=this.selectionEnd=from+text.length},
  dispatchEvent(event){inputEvents.push(event);s.session.goal=this.value}
 };
 s.event.currentTarget=textarea;
 return {textarea,inputEvents};
}
test('plain Enter submits once and prevents textarea newline',()=>{
 const s=setup();assert.equal(handleGoalKeydown(s.event,s.session,{}),true);assert.equal(s.starts,1);assert.equal(s.prevented,1);
});
test('Ctrl+Enter inserts newline at caret, preserves trailing text and synchronizes input',()=>{
 const s=setup();const {textarea,inputEvents}=withTextarea(s);s.event.ctrlKey=true;
 assert.equal(handleGoalKeydown(s.event,s.session,{}),true);assert.equal(s.starts,0);assert.equal(s.prevented,1);
 assert.equal(textarea.value,'Найди\n API');assert.equal(textarea.selectionStart,6);assert.equal(textarea.selectionEnd,6);
 assert.equal(s.session.goal,textarea.value);assert.equal(inputEvents.length,1);assert.equal(inputEvents[0].type,'input');assert.equal(inputEvents[0].bubbles,true);
});
test('Ctrl+Enter replaces selection with newline without requiring loaded project',()=>{
 const s=setup();const {textarea}=withTextarea(s,0,5);s.event.ctrlKey=true;
 assert.equal(handleGoalKeydown(s.event,s.session,null),true);assert.equal(textarea.value,'\n API');assert.equal(textarea.selectionStart,1);assert.equal(s.starts,0);
});
test('Ctrl+Enter respects disabled textarea and maximum length',()=>{
 for(const overrides of [{disabled:true},{maxLength:9}]){
  const s=setup();const {textarea,inputEvents}=withTextarea(s);Object.assign(textarea,overrides);s.event.ctrlKey=true;
  assert.equal(handleGoalKeydown(s.event,s.session,{}),false);assert.equal(inputEvents.length,0);assert.equal(s.session.goal,'Найди API');assert.equal(s.starts,0);
 }
});
test('Shift+Enter preserves native newline; Meta/Alt and unrelated keys do not submit',()=>{
 for(const overrides of [{shiftKey:true},{ctrlKey:true,shiftKey:true},{metaKey:true},{altKey:true},{ctrlKey:true,altKey:true},{key:'s',ctrlKey:true}]){
  const s=setup();Object.assign(s.event,overrides);assert.equal(handleGoalKeydown(s.event,s.session,{}),false);assert.equal(s.starts,0);assert.equal(s.prevented,0);
 }
});
test('IME and repeated key events cannot submit or insert explicit newline',()=>{
 for(const ctrlKey of [false,true])for(const overrides of [{isComposing:true},{keyCode:229},{repeat:true}]){
  const s=setup();const {inputEvents}=withTextarea(s);Object.assign(s.event,{ctrlKey},overrides);
  assert.equal(handleGoalKeydown(s.event,s.session,{}),false);assert.equal(s.starts,0);assert.equal(inputEvents.length,0);assert.equal(s.prevented,0);
 }
});
test('pending and applying block both submission and explicit newline',()=>{
 for(const ctrlKey of [false,true])for(const overrides of [{pending:true},{applying:true}]){
  const s=setup(overrides);const {inputEvents}=withTextarea(s);s.event.ctrlKey=ctrlKey;
  assert.equal(canStart(s.session,{}),false);assert.equal(handleGoalKeydown(s.event,s.session,{}),false);assert.equal(s.starts,0);assert.equal(inputEvents.length,0);
 }
});
test('blank goal and unavailable project block Enter submission',()=>{
 for(const [overrides,project] of [[{goal:'  '},{}],[{},null]]){
  const s=setup(overrides);assert.equal(canStart(s.session,project),false);assert.equal(handleGoalKeydown(s.event,s.session,project),false);assert.equal(s.starts,0);assert.equal(s.prevented,1);
 }
});
test('Escape focuses goal without clearing task and is ignored during work',()=>{
 let focus=0;const s=setup({task:{id:'existing'}});s.event.key='Escape';assert.equal(handleEscape(s.event,s.session,()=>focus++),true);assert.equal(focus,1);assert.equal(s.session.task.id,'existing');
 for(const flag of ['pending','applying']){s.session[flag]=true;assert.equal(handleEscape(s.event,s.session,()=>focus++),false);s.session[flag]=false}assert.equal(focus,1);
});
