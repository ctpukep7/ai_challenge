import {taskChanges} from './changes.js';

export function createSession(request,wait=(ms)=>new Promise(resolve=>setTimeout(resolve,ms))){
  let generation=0;
  return {
    goal:'',task:null,pending:false,applying:false,error:'',notice:'',
    reset(){generation++;this.task=null;this.pending=false;this.applying=false;this.error='';this.notice=''},
    async start(){
      if(this.pending||this.applying||!this.goal.trim())return;
      const token=++generation;this.pending=true;this.error='';this.notice='';this.task=null;
      try{
        const created=await request('/api/tasks',{goal:this.goal.trim()});
        if(token!==generation)return;
        this.task={...created,goal:this.goal,trace:[],sources:[]};
        while(token===generation){
          const task=await request(`/api/tasks/${encodeURIComponent(created.id)}`);
          if(token!==generation)return;
          this.task=task;
          if(task.status!=='running'){
            if(task.status==='error')this.error=typeof task.error==='string'?task.error:'Ошибка выполнения задачи';
            return;
          }
          await wait(650);
        }
      }catch(e){if(token===generation)this.error=e.message}
      finally{if(token===generation)this.pending=false}
    },
    async apply(){
      if(this.pending||this.applying||this.task?.status!=='completed'||!taskChanges(this.task).some(change=>change.diff)||this.task.applied)return;
      const token=generation;this.applying=true;this.error='';this.notice='';
      try{
        const result=await request(`/api/tasks/${encodeURIComponent(this.task.id)}/apply`,{});
        if(token!==generation)return;
        if(result.applied)this.task={...this.task,applied:true};
        this.notice=result.applied?'Изменения сохранены в проекте.':'Файлы уже соответствуют предложению: изменений нет.';
      }catch(e){if(token===generation)this.error=e.message}
      finally{if(token===generation)this.applying=false}
    }
  };
}
