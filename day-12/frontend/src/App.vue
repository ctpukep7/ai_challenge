<template>
  <div v-if="state" class="app-shell">
    <section class="workspace">
      <aside class="sidebar left-panel">
        <div class="brand"><span class="day-mark">12</span><div><b>AI Challenge</b><small>Персонализация</small></div></div>

        <div class="panel-heading"><div><span class="eyebrow">РЕЖИМ ОТВЕТА</span><h2>Профили</h2></div><button class="icon-btn" :disabled="busy" title="Создать профиль" @click="showCreate=true">＋</button></div>
        <div class="profile-list">
          <button v-for="profile in state.profiles" :key="profile.id" class="profile-item" :class="{active:profile.id===state.active_profile.id}" :disabled="busy" @click="activateProfile(profile.id)">
            <span class="avatar">{{ profile.name.slice(0,1) }}</span><span><b>{{ profile.name }}</b><small>{{ toneLabel(profile.tone) }} · {{ detailLabel(profile.detail_level) }}</small></span><i v-if="profile.is_active">●</i>
          </button>
        </div>

        <div class="divider"></div>
        <div class="panel-heading compact"><div><span class="eyebrow">КРАТКОСРОЧНАЯ</span><h2>Диалоги</h2></div><button class="icon-btn" :disabled="busy" title="Новый диалог" @click="createSession">＋</button></div>
        <div class="session-list">
          <div v-for="session in state.sessions" :key="session.id" class="session-row" :class="{active:session.is_active}">
            <button class="session-activate" :disabled="busy" @click="activateSession(session.id)">
              <span>{{ session.branched_from_id ? '⑂' : '◌' }}</span><span><b>{{ session.label }}</b><small>{{ session.message_count }} сообщений</small></span>
            </button>
            <button class="session-delete" :disabled="busy" :aria-label="`Удалить ${session.label}`" title="Удалить диалог" @click.stop="deleteSession(session)">×</button>
          </div>
        </div>

      </aside>

      <main class="chat-panel">
        <header class="chat-header">
          <div><span class="eyebrow">АКТИВНЫЙ ПРОФИЛЬ</span><h1>{{ state.active_profile.name }}</h1></div>
          <div class="header-actions"><span class="profile-badge">{{ formatLabel(state.active_profile.response_format) }}</span><button class="ghost" @click="showSettings=true">⚙ Настройки</button></div>
        </header>

        <div ref="messagesEl" class="messages">
          <div v-if="!state.active_session.messages.length" class="empty-state">
            <span class="empty-icon">◎</span><h2>Новый диалог для профиля «{{ state.active_profile.name }}»</h2>
            <p>Каждый профиль видит только свои диалоги, но использует общую рабочую и долговременную память.</p>
            <div class="empty-actions"><button v-for="(item,index) in personalizationQuestions" :key="item.label" :class="index===0 ? 'primary' : 'secondary'" @click="insertTest(item.prompt)">{{ item.label }}</button></div>
          </div>
          <article v-for="message in state.active_session.messages" :key="message.id" class="message" :class="message.role">
            <div class="message-label">{{ message.role==='user' ? 'Вы' : state.active_profile.name }}</div>
            <div v-if="message.role==='assistant'" class="bubble assistant-content" v-html="message.answer_html"></div>
            <div v-else class="bubble">{{ message.content }}</div>
            <button v-if="message.preview" class="preview-link" @click="preview=message.preview">Контекст ответа ↗</button>
          </article>
          <article v-if="busy" class="message assistant"><div class="message-label">{{ state.active_profile.name }}</div><div class="bubble typing"><i></i><i></i><i></i></div></article>
        </div>

        <div class="composer-wrap">
          <div v-if="error" class="error-banner">{{ error }}</div>
          <div class="quick-row"><button v-for="item in personalizationQuestions" :key="item.label" class="chip" :disabled="busy" @click="insertTest(item.prompt)">＋ {{ item.label }}</button><button class="chip" :disabled="busy" @click="branchSession">⑂ Создать ветку</button><button class="chip danger-text" :disabled="busy" @click="resetSession">Очистить чат</button></div>
          <form class="composer" @submit.prevent="send">
            <textarea v-model="prompt" rows="2" maxlength="4000" placeholder="Задайте один и тот же вопрос разным профилям…" @keydown.enter.exact.prevent="send"></textarea>
            <button class="send" :disabled="busy || !prompt.trim()" aria-label="Отправить">↑</button>
          </form>
          <small>Профиль применяется автоматически к каждому запросу · Enter — отправить</small>
        </div>
      </main>

      <aside class="sidebar right-panel">
        <section class="editor-card accent-card">
          <div class="panel-heading compact"><div><span class="eyebrow">ПЕРСОНАЛИЗАЦИЯ</span><h2>Профиль ответа</h2></div><span class="status-dot">активен</span></div>
          <form class="stack-form" @submit.prevent="saveProfile">
            <label>Название<input v-model="profileForm.name" maxlength="80"></label>
            <label>Роль и контекст<textarea v-model="profileForm.role_context" maxlength="500" rows="2"></textarea></label>
            <div class="triple"><label>Тон<select v-model="profileForm.tone"><option value="friendly">Дружелюбный</option><option value="neutral">Нейтральный</option><option value="business">Деловой</option></select></label><label>Глубина<select v-model="profileForm.detail_level"><option value="brief">Кратко</option><option value="balanced">Баланс</option><option value="deep">Глубоко</option></select></label><label>Формат<select v-model="profileForm.response_format"><option value="paragraphs">Абзацы</option><option value="bullets">Список</option><option value="steps">Шаги</option><option value="table">Таблица</option></select></label></div>
            <label>Язык<input v-model="profileForm.language" maxlength="500"></label>
            <label>Ограничения<textarea v-model="profileForm.constraints" maxlength="500" rows="2"></textarea></label>
            <button class="primary full" :disabled="busy">Сохранить профиль</button>
          </form>
          <p class="hint">Меняется только явно. Профиль не может отменить safety и жёсткие ограничения задачи.</p>
        </section>

        <section class="editor-card">
          <span class="eyebrow">ОБЩАЯ ДЛЯ ВСЕХ ПРОФИЛЕЙ</span><h2>Рабочая память</h2>
          <form class="stack-form" @submit.prevent="saveTask">
            <label>Цель<input v-model="taskForm.goal" maxlength="500" placeholder="Что нужно получить"></label>
            <label>Жёсткие ограничения<textarea v-model="taskForm.hard_constraints" maxlength="500" rows="2" placeholder="Бюджет, сроки, запреты"></textarea></label>
            <label>Данные<textarea v-model="taskForm.task_data" maxlength="500" rows="2" placeholder="Факты текущей задачи"></textarea></label>
            <label>Открытые вопросы<input v-model="taskForm.open_questions" maxlength="500"></label>
            <button class="secondary full" :disabled="busy || !taskForm.goal.trim()">Сохранить задачу</button>
          </form>
        </section>

        <section class="editor-card">
          <span class="eyebrow">ОБЩАЯ ДОЛГОВРЕМЕННАЯ</span><h2>Решения и знания</h2>
          <div class="memory-items"><div v-for="item in state.long_term" :key="item.id" class="memory-item"><span>{{ item.entry_type==='decision' ? 'Решение' : 'Знание' }}</span><p>{{ item.content }}</p><button :disabled="busy" @click="deleteLongTerm(item.id)">×</button></div></div>
          <form class="inline-add" @submit.prevent="addLongTerm"><select v-model="memoryType"><option value="knowledge">Знание</option><option value="decision">Решение</option></select><input v-model="memoryText" maxlength="500" placeholder="Явная запись"><button :disabled="busy || !memoryText.trim()">＋</button></form>
        </section>
      </aside>
    </section>

    <section class="after-chat">
      <div><span class="eyebrow">ПОСЛЕ ЧАТА</span><h2>Что доказывает демонстрация</h2><p>Профиль меняет подачу автоматически, а данные задачи остаются теми же. Диалоги не смешиваются.</p></div>
      <div class="metrics"><div><b>{{ state.metrics.profiles }}</b><span>профиля</span></div><div><b>{{ state.metrics.profile_sessions }}</b><span>сессий профиля</span></div><div><b>{{ state.metrics.profile_messages }}</b><span>сообщений</span></div><div><b>{{ state.metrics.knowledge + state.metrics.decisions }}</b><span>общих записей</span></div></div>
      <div class="audit"><h3>Безопасный аудит</h3><div v-for="event in state.events.slice(0,8)" :key="event.id"><span :class="'layer '+event.layer">{{ event.layer }}</span><b>{{ event.action }}</b><small>{{ event.object_type }} #{{ event.object_id || '—' }}</small></div><p v-if="!state.events.length" class="hint">Действий пока нет. Содержимое полей в аудит не записывается.</p></div>
    </section>

    <div v-if="showCreate" class="modal-backdrop" @click.self="showCreate=false"><section class="modal"><button class="modal-close" @click="showCreate=false">×</button><span class="eyebrow">НОВЫЙ РЕЖИМ</span><h2>Создать профиль</h2><p>Можно заполнить поля шаблоном или настроить профиль вручную. Создание произойдёт только по отдельной кнопке.</p><form class="stack-form" @submit.prevent="createProfile"><div class="template-picker"><label>Шаблон<select v-model="profileTemplate"><option value="expert">Эксперт</option><option value="manager">Руководитель</option></select></label><button type="button" class="secondary" @click="fillProfileTemplate">Заполнить шаблоном</button></div><label>Название<input v-model="createForm.name" maxlength="80" required placeholder="Например, Редактор"></label><label>Роль и контекст<textarea v-model="createForm.role_context" maxlength="500" rows="2"></textarea></label><div class="triple"><label>Тон<select v-model="createForm.tone"><option value="friendly">Дружелюбный</option><option value="neutral">Нейтральный</option><option value="business">Деловой</option></select></label><label>Глубина<select v-model="createForm.detail_level"><option value="brief">Кратко</option><option value="balanced">Баланс</option><option value="deep">Глубоко</option></select></label><label>Формат<select v-model="createForm.response_format"><option value="paragraphs">Абзацы</option><option value="bullets">Список</option><option value="steps">Шаги</option><option value="table">Таблица</option></select></label></div><label>Язык<input v-model="createForm.language" maxlength="500"></label><label>Ограничения<textarea v-model="createForm.constraints" maxlength="500" rows="2"></textarea></label><button class="primary full">Создать и активировать</button></form></section></div>

    <div v-if="showSettings" class="modal-backdrop" @click.self="showSettings=false"><section class="modal settings-modal"><button class="modal-close" @click="showSettings=false">×</button><span class="eyebrow">КОНТЕКСТ И МОДЕЛЬ</span><h2>Настройки запроса</h2><form class="stack-form" @submit.prevent="showSettings=false"><label>Провайдер<select v-model="controls.provider"><option value="deepseek">DeepSeek</option><option value="openrouter">OpenRouter</option></select></label><label>Модель<select v-model="controls.model"><template v-if="controls.provider==='deepseek'"><option value="deepseek-flash">DeepSeek Flash</option><option value="deepseek-v4-pro">DeepSeek V4 Pro</option></template><option v-else value="openrouter/free">OpenRouter Free</option></select></label><label>Режим памяти<select v-model="controls.compression_mode"><option value="full_session">Полная сессия</option><option value="sliding_window">Sliding Window</option><option value="sticky_facts">Sticky Facts</option><option value="rolling_summary">Rolling summary</option><option value="explicit_only">Только явная память</option></select></label><label v-if="['sliding_window','sticky_facts'].includes(controls.compression_mode)">Размер окна<input v-model.number="controls.window_size" type="number" min="2" max="20" step="2"></label><div v-if="controls.compression_mode==='rolling_summary'" class="triple"><label>Batch<input v-model.number="controls.batch_size" type="number" min="2" max="20" step="2"></label><label>Оставить<input v-model.number="controls.keep_recent" type="number" min="2" max="20" step="2"></label></div><label>Temperature<input v-model.number="controls.temperature" type="number" min="0" max="2" step="0.1"></label><label>Max tokens<input v-model="controls.max_tokens" type="number" min="1" max="4096" placeholder="По умолчанию"></label><button class="primary full">Готово</button></form><section class="danger-zone"><span class="eyebrow">ОПАСНАЯ ЗОНА</span><h3>Удалить все данные</h3><p>Будут удалены профили, диалоги, рабочая и долговременная память. Затем появится чистый профиль «Новичок».</p><button class="danger-button full" :disabled="busy" @click="clearAll">Удалить всё и начать заново</button></section></section></div>

    <div v-if="preview" class="modal-backdrop" @click.self="preview=null"><section class="modal preview-modal"><button class="modal-close" @click="preview=null">×</button><span class="eyebrow">ТОЧНЫЙ SNAPSHOT</span><h2>Контекст ответа</h2><div class="snapshot"><b>{{ preview.profile_snapshot?.name }}</b><span>{{ preview.profile_snapshot?.tone }} · {{ preview.profile_snapshot?.detail_level }} · {{ preview.profile_snapshot?.response_format }}</span></div><pre>{{ JSON.stringify(preview, null, 2) }}</pre></section></div>
  </div>
  <div v-else class="loading">Загружаем персонализированного агента…</div>
</template>

<script setup>
import { nextTick, onMounted, reactive, ref, watch } from 'vue'
import { personalizationQuestions, profileTemplates } from './demo-presets'

const state=ref(null), prompt=ref(''), busy=ref(false), error=ref(''), messagesEl=ref(null)
const showCreate=ref(false), showSettings=ref(false), preview=ref(null)
const memoryType=ref('knowledge'), memoryText=ref('')
const profileTemplate=ref('expert')
const blankProfile={name:'',role_context:'',language:'Русский',tone:'neutral',detail_level:'balanced',response_format:'paragraphs',constraints:''}
const createForm=reactive({...blankProfile}), profileForm=reactive({...blankProfile})
const taskForm=reactive({goal:'',hard_constraints:'',task_data:'',open_questions:''})
const controls=reactive({provider:'deepseek',model:'deepseek-flash',temperature:0,max_tokens:'',compression_mode:'full_session',window_size:6,batch_size:10,keep_recent:6})

async function api(path,options={}){
  error.value=''
  const response=await fetch(path,{headers:{'Content-Type':'application/json'},...options})
  const data=await response.json().catch(()=>({}))
  if(!response.ok) throw new Error(data.detail?.message || data.detail || 'Не удалось выполнить запрос')
  return data
}
function sync(next){
  state.value=next
  Object.assign(profileForm,Object.fromEntries(Object.keys(blankProfile).map(key=>[key,next.active_profile[key] ?? ''])))
  Object.assign(taskForm,next.task || {goal:'',hard_constraints:'',task_data:'',open_questions:''})
  nextTick(scrollBottom)
}
function scrollBottom(){if(messagesEl.value) messagesEl.value.scrollTop=messagesEl.value.scrollHeight}
async function mutate(path,method='POST',body){
  busy.value=true
  try{sync(await api(path,{method,body:body===undefined?undefined:JSON.stringify(body)}))}catch(e){error.value=e.message}finally{busy.value=false}
}
const activateProfile=id=>mutate(`/api/profiles/${id}/activate`)
const createSession=()=>mutate('/api/sessions')
const activateSession=id=>mutate(`/api/sessions/${id}/activate`)
const branchSession=()=>mutate(`/api/sessions/${state.value.active_session.id}/branch`)
async function resetSession(){if(confirm('Удалить историю этого диалога?')) await mutate(`/api/sessions/${state.value.active_session.id}/reset`)}
async function deleteSession(session){if(confirm(`Удалить диалог «${session.label}» вместе с его сообщениями?`)) await mutate(`/api/sessions/${session.id}`,'DELETE')}
async function clearAll(){
  if(!confirm('Удалить все профили, диалоги, рабочую и долговременную память? Это действие нельзя отменить.'))return
  await mutate('/api/memory/clear')
  if(!error.value)showSettings.value=false
}
async function saveProfile(){await mutate(`/api/profiles/${state.value.active_profile.id}`,'PUT',profileForm)}
async function createProfile(){await mutate('/api/profiles','POST',createForm);if(!error.value){showCreate.value=false;Object.assign(createForm,blankProfile)}}
function fillProfileTemplate(){Object.assign(createForm,profileTemplates[profileTemplate.value])}
async function saveTask(){await mutate('/api/task','PUT',taskForm)}
async function addLongTerm(){await mutate('/api/long-term','POST',{entry_type:memoryType.value,content:memoryText.value});if(!error.value)memoryText.value=''}
const deleteLongTerm=id=>mutate(`/api/long-term/${id}`,'DELETE')
function insertTest(question){prompt.value=question;nextTick(()=>document.querySelector('.composer textarea')?.focus())}
async function send(){
  if(busy.value || !prompt.value.trim())return
  const question=prompt.value;prompt.value='';busy.value=true;await nextTick();scrollBottom()
  try{
    const result=await api('/api/ask',{method:'POST',body:JSON.stringify({prompt:question,controls}),headers:{'Content-Type':'application/json'}})
    sync(result.state)
  }catch(e){prompt.value=question;error.value=e.message}finally{busy.value=false;await nextTick();scrollBottom()}
}
const toneLabel=value=>({friendly:'дружелюбно',neutral:'нейтрально',business:'по-деловому'}[value]||value)
const detailLabel=value=>({brief:'кратко',balanced:'баланс',deep:'глубоко'}[value]||value)
const formatLabel=value=>({paragraphs:'Абзацы',bullets:'Список',steps:'Шаги',table:'Таблица'}[value]||value)
watch(()=>state.value?.active_session?.messages?.length,()=>nextTick(scrollBottom))
watch(()=>controls.provider,(provider)=>{
  if(provider==='openrouter') controls.model='openrouter/free'
  else if(!['deepseek-flash','deepseek-v4-pro'].includes(controls.model)) controls.model='deepseek-flash'
})
onMounted(async()=>{try{sync(await api('/api/state'))}catch(e){error.value=e.message}})
</script>
