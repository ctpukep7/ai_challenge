// A scenario stages text only. Sending is always an explicit caller action.
export function createScenarioDraft({getContext,onDraft,onProgress}) {
 let step=0,loaded=null
 function reset(){if(loaded)onDraft('');loaded=null;step=0;onProgress(step,false)}
 function loadNext(){
  if(loaded)return false
  const context=getContext(),item=context.steps?.[step]
  if(!context.sessionId || !item)return false
  const text=typeof item==='string'?item:item.question || item.content
  if(!text)return false
  loaded={sessionId:context.sessionId,scenarioId:context.scenarioId,index:step}
  onDraft(text);onProgress(step,true);return true
 }
 async function send(text,submit){
  const marker=loaded
  const response=await submit(text)
  const context=getContext()
  if(marker && loaded===marker && marker.sessionId===context.sessionId && marker.scenarioId===context.scenarioId && marker.index===step){step++;loaded=null;onProgress(step,false)}
  return response
 }
 return {reset,loadNext,send}
}
