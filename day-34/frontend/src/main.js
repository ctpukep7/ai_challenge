import {createApp} from 'vue/dist/vue.esm-bundler.js';
import {createSession} from './session.js';
import {taskChanges} from './changes.js';
import {renderMarkdown} from './markdown.js';
import {canStart, handleGoalKeydown, handleEscape} from './keyboard.js';
import {DiffView} from './diff-view.js';
import 'diff2html/bundles/css/diff2html.min.css';
import './style.css';
async function request(path,body){
  const response=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  let data;try{data=await response.json()}catch{throw Error('Сервер вернул некорректный ответ')}
  if(!response.ok)throw Error(typeof data.detail==='string'?data.detail:`Ошибка запроса (${response.status})`);
  return data;
}
createApp({
  components:{DiffView},
  data:()=>({session:createSession(request),project:null,projectError:''}),
  async mounted(){window.addEventListener('keydown', this.onEscape);try{this.project=await request('/api/project')}catch(e){this.projectError=e.message}},
  beforeUnmount(){window.removeEventListener('keydown', this.onEscape)},
  computed:{changes(){return taskChanges(this.session.task)}},
  methods:{renderMarkdown,canStart,focusGoal(){this.$nextTick(()=>this.$refs.goal?.focus())},chooseExample(example){this.session.goal=example;this.focusGoal()},resetTask(){this.session.reset();this.focusGoal()},startTask(){if(canStart(this.session,this.project))this.session.start()},onGoalKeydown(event){handleGoalKeydown(event,this.session,this.project)},onEscape(event){handleEscape(event,this.session,this.focusGoal)},toolLabel(tool){return {list_files:'Список файлов',search_text:'Поиск по проекту',read_file:'Чтение файла',stage_file:'Подготовка изменений'}[tool]||tool},status(value){return {running:'Выполняется',completed:'Завершено',unknown:'Недостаточно данных',error:'Ошибка'}[value]||value},pretty(value){return JSON.stringify(value,null,2)}},
  template:`<div class="shell"><a class="skip-link" href="#goal">Перейти к задаче</a>
    <header><div class="eyebrow">PIPELINE / DAY 34</div><h1>Ассистент файлов проекта</h1><p>Задайте цель — ассистент сам выберет файлы, найдёт сведения и подготовит изменения.</p></header>
    <section class="toolbar" aria-label="Контекст проекта"><div><strong>{{project?.name||'Подключение проекта…'}}</strong><p v-if="project">{{project.root_label}} · ветка {{project.branch||'не определена'}}</p></div><span class="badge">Локальные файловые инструменты</span></section>
    <p v-if="projectError" class="error" role="alert">{{projectError}}</p>
    <form @submit.prevent="startTask()"><label for="goal">Что нужно сделать в проекте?</label><textarea ref="goal" id="goal" aria-describedby="keyboard-hint data-hint" aria-keyshortcuts="Enter" @keydown="onGoalKeydown" v-model="session.goal" maxlength="4000" placeholder="Например: найди, как вопрос поддержки проходит от интерфейса до ответа" :disabled="session.pending||session.applying"></textarea>
      <div class="examples"><button v-for="(example,i) in project?.examples||[]" type="button" class="secondary" :disabled="session.pending||session.applying" @click="chooseExample(example)">{{i===0?'Найти использование API':'Подготовить документ'}}</button></div>
      <div class="actions"><button :disabled="!session.goal.trim()||session.pending||session.applying||!project">{{session.pending?'Ассистент работает…':'Запустить задачу'}}</button><button type="button" class="secondary" v-if="session.task&&!session.pending&&!session.applying" @click="resetTask()">Новая задача</button></div>
      <p class="keyboard-hint" id="keyboard-hint"><kbd>Enter</kbd> — запустить · <kbd>Ctrl</kbd> + <kbd>Enter</kbd> / <kbd>Shift</kbd> + <kbd>Enter</kbd> — новая строка · <kbd>Esc</kbd> — вернуться к задаче</p><small id="data-hint">Работает с публичными файлами AI Challenge. Вопрос, прочитанные фрагменты и результаты инструментов передаются DeepSeek. Не вводите секреты. Изменения сначала показываются как diff.</small>
    </form>
    <main aria-live="polite">
      <section v-if="!session.task&&!session.pending" class="empty"><h2>От цели — к проверяемому результату</h2><p>Найдите использование API в нескольких файлах или подготовьте Markdown-документ на основе кода и тестов.</p><p>Запись разрешена только в <code>{{project?.write_scope||'day-34/generated/*.md'}}</code> и выполняется кнопкой после просмотра изменений.</p></section>
      <article v-if="session.pending&&!session.task"><p class="pending">Создаём задачу…</p></article>
      <template v-if="session.task"><article class="result"><div class="label">{{status(session.task.status)}}</div><h2>Результат работы</h2><div v-if="session.task.summary" class="markdown" v-html="renderMarkdown(session.task.summary)"></div><p v-else>Ассистент выбирает инструменты и исследует проект…</p><small v-if="session.task.metrics">LLM: {{session.task.metrics.llm_calls}} · инструменты: {{session.task.metrics.tool_calls}} · {{Number(session.task.metrics.elapsed_seconds||0).toFixed(1)}} с</small></article>
      <details class="trace" v-if="session.task.trace?.length" :open="session.pending"><summary>Работа с файлами · {{session.task.trace.length}} операций</summary><p>Реальные операции, выбранные ассистентом. Подготовка файла ещё не означает его сохранение.</p><article v-for="(step,i) in session.task.trace" :key="step.id||i" class="step"><div class="step-title"><strong>{{i+1}}. {{toolLabel(step.tool)}}</strong><span :class="step.status==='error'?'warning':'success'">{{step.status==='error'?'Ошибка':'Выполнено'}} · {{step.duration_ms}} мс</span></div><details><summary>Параметры и результат · {{step.tool}}</summary><pre>{{pretty(step.arguments)}}</pre><pre>{{pretty(step.result)}}</pre></details></article></details>
      <section v-if="session.task.sources?.length"><h2>Прочитанные источники · {{session.task.sources.length}}</h2><details v-for="source in session.task.sources" :key="source.id" class="source"><summary>{{source.path}}:{{source.start_line}}–{{source.end_line}}</summary><small>{{source.id}} · SHA256 {{source.sha256}}</small><pre>{{source.text}}</pre></details></section>
      <section v-if="changes.length" class="changes" aria-label="Изменения файлов"><div class="label">{{session.task.applied?'Сохранено в проекте':'Предварительный просмотр'}}</div><h2>Изменения файлов · {{changes.length}}</h2><p>Каждый файл показан отдельно. Прокручивайте страницу, чтобы увидеть все строки.</p>
      <article v-for="change in changes" :key="change.path" class="change"><h3>{{change.path}}</h3><p v-if="!change.diff">Файл уже соответствует предложению. Изменений нет.</p><diff-view v-else :key="session.task.id+':'+change.path" :patch="change.diff" :filename="change.path" /><details class="document-preview"><summary>Предпросмотр документа · {{change.path}}</summary><div class="markdown" v-html="renderMarkdown(change.after)"></div></details><details><summary>Исходный текст: до и после · {{change.path}}</summary><h4>До</h4><pre>{{change.before||'(новый файл)'}}</pre><h4>После</h4><pre>{{change.after}}</pre></details></article>
      <div class="actions"><button v-if="changes.some(change=>change.diff)" type="button" :disabled="session.pending||session.applying||session.task.applied||session.task.status!=='completed'" @click="session.apply()">{{session.applying?'Сохранение…':session.task.applied?'Изменения сохранены':'Сохранить все изменения'}}</button><a v-if="changes.some(change=>change.diff)" class="button secondary" :href="'/api/tasks/'+encodeURIComponent(session.task.id)+'/patch'" download="day34.patch">Скачать общий patch</a></div><small>Перед записью сервер проверит все прочитанные файлы и целевые документы. При конфликте сохранение будет отменено.</small></section>
      </template>
      <p v-if="session.error" class="error" role="alert">{{session.error}}</p><p v-if="session.notice" class="notice" role="status">{{session.notice}}</p>
    </main>
  </div>`
}).mount('#app');
