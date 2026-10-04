import {isActiveRun} from './view.js'

// One observer owns selection and rejects responses from superseded requests.
export function createChatObserver({api,onState,onRun,onSession}) {
 let selectedId=null,selectionVersion=0,readVersion=0,runVersion=0,currentRun=null,starting=false
 async function readSelected(){
  const id=selectedId,selection=selectionVersion,read=++readVersion
  if(!id)return
  try{const data=await api(`/sessions/${id}`)
   if(id===selectedId && selection===selectionVersion && read===readVersion)await onSession(data)
  }catch(error){if(error.status===404){if(id===selectedId && selection===selectionVersion && read===readVersion)await select(null)}else throw error}
 }
 async function select(id){selectedId=id;selectionVersion++;readVersion++;if(id)await readSelected();else await onSession(null)}
 async function refresh(){
  const version=runVersion
  const state=await api('/state');onState(state)
  if(!starting && version===runVersion){
   const active=state.active_run
   if(active && (!currentRun || !isActiveRun(currentRun) || currentRun.id===active.id))currentRun={...currentRun,...active}
   if(currentRun?.id && (isActiveRun(currentRun) || !currentRun.status)){
    const job=await api(`/runs/${currentRun.id}`)
    if(!starting && version===runVersion){currentRun=job;onRun(job);if(!selectedId && job.session_id)await select(job.session_id)}
   }
  }
  // Also exposes an ordinary manually submitted question while its HTTP call waits.
  await readSelected()
 }
 async function start(body){
  const version=++runVersion;starting=true
  try{
   const accepted=await api('/scenarios/run',body)
   currentRun={...accepted,status:accepted.status || 'queued'};onRun(currentRun)
   const job=await api(`/runs/${accepted.id}`)
   if(version!==runVersion)return
   currentRun=job;onRun(job)
   if(job.session_id)await select(job.session_id)
  }finally{starting=false}
 }
 return {select,refresh,start,readSelected}
}
