export async function api(path, body, method) {
 const response = await fetch(`/api${path}`, {method:method || (body===undefined?'GET':'POST'),headers:{'Content-Type':'application/json'},...(body===undefined?{}:{body:JSON.stringify(body)})})
 const data=await response.json().catch(()=>({})); if(!response.ok) throw new Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail||data.error||`HTTP ${response.status}`)); return data
}
export function scopeLabel(scope) { return scope?.sources ? scope.sources.join(', ') : 'Все README корпуса' }
export function statusLabel(status) { return ({memory_updated:'Память задачи обновлена',answered:'Ответ подтверждён',unknown:'Не знаю · уточните вопрос',error:'Ошибка провайдера',pending:'Запрос выполняется',interrupted:'Запрос прерван'})[status] || status }
export function quoteChecks(result) { return {sources:result?.status==='answered' && result.sources?.length>0,exact:result?.validation?.quotes_exact===true,semantic:result?.validation?.semantic_status==='supported'} }
export function exportJSON(value, name) {const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'}));const link=document.createElement('a');link.href=url;link.download=name;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}
export function taskPayload(task) { return {goal:task.goal||'',clarifications:task.clarifications||[],constraints:task.constraints||[],terms:task.terms||[],source_days:task.source_days||[]} }
export function lines(value) { return String(value||'').split('\n').map(s=>s.trim()).filter(Boolean) }
export function dayNumbers(value) { return [...new Set(String(value||'').split(/[ ,]+/).filter(Boolean).map(Number))].filter(n=>Number.isInteger(n)&&n>=1&&n<=20) }
