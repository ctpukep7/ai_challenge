import test from 'node:test'
import assert from 'node:assert/strict'
import {scopeLabel,quoteChecks,lines,dayNumbers,statusLabel,taskPayload} from '../src/view.js'
test('unknown cannot count as sourced answer even if malformed sources supplied',()=>assert.equal(quoteChecks({status:'unknown',sources:[{}]}).sources,false))
test('quote and meaning checks remain independent',()=>assert.deepEqual(quoteChecks({status:'answered',sources:[{}],validation:{quotes_exact:true,semantic_status:'uncertain'}}),{sources:true,exact:true,semantic:false}))
test('explicit retrieval scope stays visible',()=>assert.equal(scopeLabel({sources:['day-07/README.md','day-09/README.md']}),'day-07/README.md, day-09/README.md'))
test('task list parsing strips empty lines and retains terms',()=>assert.deepEqual(lines('  goal\n\n RAG '),['goal','RAG']))
test('source day editor deduplicates and rejects invalid scope values',()=>assert.deepEqual(dayNumbers('7, 7, 9,21,-2,no'),[7,9]))
test('empty task reset preserves explicit shape',()=>assert.deepEqual(taskPayload({}),{goal:'',clarifications:[],constraints:[],terms:[],source_days:[]}))
test('provider errors never called unknown',()=>assert.equal(statusLabel('error'),'Ошибка провайдера'))
