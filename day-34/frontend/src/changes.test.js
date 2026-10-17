import test from 'node:test';
import assert from 'node:assert/strict';
import {taskChanges} from './changes.js';
test('plural list is authoritative, including empty list; legacy tasks remain readable',()=>{
 const changes=[{path:'a.md'},{path:'b.md'}];
 assert.equal(taskChanges({changes,change:{path:'old.md'}}),changes);
 assert.deepEqual(taskChanges({changes:[],change:{path:'old.md'}}),[]);
 assert.deepEqual(taskChanges({change:{path:'legacy.md'}}),[{path:'legacy.md'}]);
 for(const task of [null,undefined,{}, {change:null}])assert.deepEqual(taskChanges(task),[]);
});
