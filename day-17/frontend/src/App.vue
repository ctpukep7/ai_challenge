<template>
  <div v-if="state" class="app-shell">
    <section class="workspace">
      <aside class="sidebar left-panel">
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
          <div><span class="eyebrow">ДЕНЬ 17 · АГЕНТ С MCP</span><h1>{{ state.active_profile.name }}</h1></div>
          <div class="header-actions"><span class="profile-badge">{{ formatLabel(state.active_profile.response_format) }}</span><button class="ghost" @click="showSettings=true">⚙ Настройки</button></div>
        </header>

        <div ref="messagesEl" class="messages">
          <div v-if="!state.active_session.messages.length" class="empty-state">
            <span class="empty-icon">◎</span><h2>Новый диалог для профиля «{{ state.active_profile.name }}»</h2>
            <p>Спросите о событиях или попросите создать встречу. Агент предложит запись через MCP, а создание потребует вашего подтверждения.</p>
            <div class="empty-actions"><button v-for="(item,index) in mcpQuestions" :key="item.label" :class="index===0 ? 'primary' : 'secondary'" @click="insertTest(item.prompt)">{{ item.label }}</button></div>
          </div>
          <article v-for="message in state.active_session.messages" :key="message.id" class="message" :class="message.role">
            <div class="message-label">{{ message.role==='user' ? 'Вы' : state.active_profile.name }}</div>
            <div v-if="message.role==='assistant'" class="bubble assistant-content" v-html="message.answer_html"></div>
            <div v-else class="bubble">{{ message.content }}</div>
            <div v-if="message.role==='assistant' && message.preview?.mcp_trace" class="mcp-trace" aria-label="Ход работы с календарём">
              <div class="mcp-trace-heading"><span class="mcp-trace-icon">⌘</span><strong>MCP · календарь</strong><span class="mcp-trace-state" :class="{success: ['success','created'].includes(traceStatus(message)), warning: ['proposed','unknown'].includes(traceStatus(message))}">{{ traceBadge(message) }}</span></div>
              <div class="mcp-trace-row"><span>Агенту доступен</span><code>{{ message.preview.mcp_trace.available_tools?.join(', ') || 'нет инструментов' }}</code></div>
              <template v-if="traceSteps(message).length">
                <template v-for="(step,index) in traceSteps(message)" :key="index">
                  <div class="mcp-trace-row"><span>Шаг {{ index + 1 }} · {{ traceStepLabel(step) }}</span><code>{{ step.name }}</code></div>
                  <div class="mcp-trace-row"><span>Результат шага {{ index + 1 }}</span><span>{{ traceStepResult(message, step) }}</span></div>
                </template>
              </template>
              <p v-else>Модель ответила без обращения к инструменту.</p>
            </div>
            <button v-if="message.preview" class="preview-link" @click="preview=message.preview">Контекст ответа ↗</button>
          </article>
          <section v-if="pendingAction" class="calendar-proposal" aria-labelledby="calendar-proposal-title">
            <span class="eyebrow">ПРЕДЛОЖЕНИЕ · ЕЩЁ НЕ СОЗДАНО</span>
            <h2 id="calendar-proposal-title">Проверьте событие перед записью</h2>
            <dl class="calendar-proposal-details">
              <div><dt>Название</dt><dd>{{ pendingAction.title }}</dd></div>
              <div><dt>Начало</dt><dd>{{ displayDateTime(pendingAction.start) }}</dd></div>
              <div><dt>Конец</dt><dd>{{ displayDateTime(pendingAction.end) }}</dd></div>
            </dl>
            <label class="calendar-choice">Календарь назначения
              <select v-model="selectedCalendarId" :disabled="actionBusy" required>
                <option disabled value="">Выберите календарь</option>
                <option v-for="calendar in pendingAction.calendars" :key="calendar.id" :value="calendar.id">{{ calendar.name }}</option>
              </select>
            </label>
            <p class="calendar-proposal-note">Время указано с часовым поясом. Только кнопка «Создать событие» отправит запись в Яндекс Календарь через MCP.</p>
            <div class="calendar-proposal-actions">
              <button type="button" class="primary" :disabled="actionBusy || busy || !selectedCalendarId" @click="confirmCalendarAction">{{ actionBusy ? 'Обрабатываем…' : 'Создать событие' }}</button>
              <button type="button" class="secondary" :disabled="actionBusy || busy" @click="cancelCalendarAction">Отменить</button>
            </div>
          </section>
          <div v-if="actionNotice" class="calendar-action-notice" role="status">{{ actionNotice }}</div>
          <article v-if="busy" class="message assistant"><div class="message-label">{{ state.active_profile.name }}</div><div class="bubble typing"><i></i><i></i><i></i></div></article>
        </div>

        <div class="composer-wrap">
          <div v-if="error" class="error-banner" role="alert">{{ error }}<template v-if="failedAsk"> Этот запрос не сохранён. Ответы и карточки выше относятся к предыдущим запросам.</template></div>
          <div v-if="pendingAction" class="pending-hint">Сначала подтвердите или отмените предложенное событие.</div>
          <div class="quick-row"><button v-for="item in mcpQuestions" :key="item.label" class="chip" :disabled="busy || actionBusy || !!pendingAction" @click="insertTest(item.prompt)">＋ {{ item.label }}</button><button class="chip" :disabled="busy || actionBusy || !!pendingAction" @click="branchSession">⑂ Создать ветку</button><button class="chip danger-text" :disabled="busy || actionBusy || !!pendingAction" @click="resetSession">Очистить чат</button></div>
          <form class="composer" @submit.prevent="send">
            <textarea v-model="prompt" rows="2" maxlength="4000" :disabled="busy || actionBusy || !!pendingAction" placeholder="Какие события у меня в календаре на ближайшие 7 дней?" @keydown.enter.exact.prevent="send"></textarea>
            <button class="send" :disabled="busy || actionBusy || !!pendingAction || !prompt.trim()" aria-label="Отправить">↑</button>
          </form>
          <small>Пример подставляет вопрос, но не отправляет его · Enter — отправить</small>
        </div>
      </main>

      <aside class="sidebar right-panel">
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
      <div><span class="eyebrow">ПОСЛЕ ЧАТА</span><h2>Что доказывает демонстрация</h2><p>Агент получает описание MCP-инструментов, читает события или предлагает запись. Создание события требует отдельного подтверждения; карточка под ответом показывает этап вызова без секретов.</p></div>
      <div class="metrics"><div><b>{{ state.metrics.profiles }}</b><span>профиля</span></div><div><b>{{ state.metrics.profile_sessions }}</b><span>сессий профиля</span></div><div><b>{{ state.metrics.profile_messages }}</b><span>сообщений</span></div><div><b>{{ state.metrics.knowledge + state.metrics.decisions }}</b><span>общих записей</span></div></div>
      <div class="audit"><h3>Безопасный аудит</h3><div v-for="event in state.events.slice(0,8)" :key="event.id"><span :class="'layer '+event.layer">{{ event.layer }}</span><b>{{ event.action }}</b><small>{{ event.object_type }} #{{ event.object_id || '—' }}</small></div><p v-if="!state.events.length" class="hint">Действий пока нет. Содержимое полей в аудит не записывается.</p></div>
    </section>

    <div v-if="showCreate" class="modal-backdrop" @click.self="showCreate=false"><section class="modal"><button class="modal-close" @click="showCreate=false">×</button><span class="eyebrow">НОВЫЙ РЕЖИМ</span><h2>Создать профиль</h2><p>Можно заполнить поля шаблоном или настроить профиль вручную. Создание произойдёт только по отдельной кнопке.</p><form class="stack-form" @submit.prevent="createProfile"><div class="template-picker"><label>Шаблон<select v-model="profileTemplate"><option value="expert">Эксперт</option><option value="manager">Руководитель</option></select></label><button type="button" class="secondary" @click="fillProfileTemplate">Заполнить шаблоном</button></div><label>Название<input v-model="createForm.name" maxlength="80" required placeholder="Например, Редактор"></label><label>Роль и контекст<textarea v-model="createForm.role_context" maxlength="500" rows="2"></textarea></label><div class="triple"><label>Тон<select v-model="createForm.tone"><option value="friendly">Дружелюбный</option><option value="neutral">Нейтральный</option><option value="business">Деловой</option></select></label><label>Глубина<select v-model="createForm.detail_level"><option value="brief">Кратко</option><option value="balanced">Баланс</option><option value="deep">Глубоко</option></select></label><label>Формат<select v-model="createForm.response_format"><option value="paragraphs">Абзацы</option><option value="bullets">Список</option><option value="steps">Шаги</option><option value="table">Таблица</option></select></label></div><label>Язык<input v-model="createForm.language" maxlength="500"></label><label>Ограничения<textarea v-model="createForm.constraints" maxlength="500" rows="2"></textarea></label><button class="primary full">Создать и активировать</button></form></section></div>

    <div v-if="showSettings" class="modal-backdrop" @click.self="showSettings=false"><section class="modal settings-modal"><button class="modal-close" @click="showSettings=false">×</button><span class="eyebrow">НАСТРОЙКИ АГЕНТА</span><h2>Профиль и модель</h2>
      <div class="settings-tabs" role="tablist" aria-label="Разделы настроек"><button type="button" role="tab" :aria-selected="settingsTab==='profiles'" :class="{active:settingsTab==='profiles'}" @click="settingsTab='profiles'">Профили</button><button type="button" role="tab" :aria-selected="settingsTab==='model'" :class="{active:settingsTab==='model'}" @click="settingsTab='model'">Модель</button></div>
      <div v-if="settingsTab==='profiles'" class="settings-content"><div class="settings-heading"><div><h3>Профили ответа</h3><p>Выберите режим ответа или настройте активный профиль.</p></div><button type="button" class="secondary" :disabled="busy" @click="openCreateProfile">＋ Новый</button></div>
        <div class="profile-list light"><button v-for="profile in state.profiles" :key="profile.id" class="profile-item" :class="{active:profile.id===state.active_profile.id}" :disabled="busy" @click="activateProfile(profile.id)"><span class="avatar">{{ profile.name.slice(0,1) }}</span><span><b>{{ profile.name }}</b><small>{{ toneLabel(profile.tone) }} · {{ detailLabel(profile.detail_level) }}</small></span><i v-if="profile.is_active">●</i></button></div>
        <form class="stack-form settings-form" @submit.prevent="saveProfile"><label>Название<input v-model="profileForm.name" maxlength="80"></label><label>Роль и контекст<textarea v-model="profileForm.role_context" maxlength="500" rows="2"></textarea></label><div class="triple"><label>Тон<select v-model="profileForm.tone"><option value="friendly">Дружелюбный</option><option value="neutral">Нейтральный</option><option value="business">Деловой</option></select></label><label>Глубина<select v-model="profileForm.detail_level"><option value="brief">Кратко</option><option value="balanced">Баланс</option><option value="deep">Глубоко</option></select></label><label>Формат<select v-model="profileForm.response_format"><option value="paragraphs">Абзацы</option><option value="bullets">Список</option><option value="steps">Шаги</option><option value="table">Таблица</option></select></label></div><label>Язык<input v-model="profileForm.language" maxlength="500"></label><label>Ограничения<textarea v-model="profileForm.constraints" maxlength="500" rows="2"></textarea></label><button class="primary" :disabled="busy">Сохранить профиль</button></form><p class="hint">Меняется только явно. Профиль не может отменить safety и жёсткие ограничения задачи.</p></div>
      <div v-if="settingsTab==='model'" class="settings-content"><h3>Модель и контекст</h3><form class="stack-form" @submit.prevent="showSettings=false"><label>Провайдер<select v-model="controls.provider"><option value="deepseek">DeepSeek</option><option value="openrouter">OpenRouter</option></select></label><label>Модель<select v-model="controls.model"><template v-if="controls.provider==='deepseek'"><option value="deepseek-flash">DeepSeek Flash</option><option value="deepseek-v4-pro">DeepSeek V4 Pro</option></template><option v-else value="openrouter/free">OpenRouter Free</option></select></label><label>Режим памяти<select v-model="controls.compression_mode"><option value="full_session">Полная сессия</option><option value="sliding_window">Sliding Window</option><option value="sticky_facts">Sticky Facts</option><option value="rolling_summary">Rolling summary</option><option value="explicit_only">Только явная память</option></select></label><label v-if="['sliding_window','sticky_facts'].includes(controls.compression_mode)">Размер окна<input v-model.number="controls.window_size" type="number" min="2" max="20" step="2"></label><div v-if="controls.compression_mode==='rolling_summary'" class="triple"><label>Batch<input v-model.number="controls.batch_size" type="number" min="2" max="20" step="2"></label><label>Оставить<input v-model.number="controls.keep_recent" type="number" min="2" max="20" step="2"></label></div><label>Temperature<input v-model.number="controls.temperature" type="number" min="0" max="2" step="0.1"></label><label>Max tokens<input v-model="controls.max_tokens" type="number" min="1" max="4096" placeholder="По умолчанию"></label><button class="primary full">Готово</button></form><section class="danger-zone"><span class="eyebrow">ОПАСНАЯ ЗОНА</span><h3>Удалить все данные</h3><p>Будут удалены профили, диалоги, рабочая и долговременная память. Затем появится чистый профиль «Новичок».</p><button class="danger-button full" :disabled="busy" @click="clearAll">Удалить всё и начать заново</button></section></div>
    </section></div>

    <div v-if="preview" class="modal-backdrop" @click.self="preview=null"><section class="modal preview-modal"><button class="modal-close" @click="preview=null">×</button><span class="eyebrow">ТОЧНЫЙ SNAPSHOT</span><h2>Контекст ответа</h2><div class="snapshot"><b>{{ preview.profile_snapshot?.name }}</b><span>{{ preview.profile_snapshot?.tone }} · {{ preview.profile_snapshot?.detail_level }} · {{ preview.profile_snapshot?.response_format }}</span></div><pre>{{ JSON.stringify(preview, null, 2) }}</pre></section></div>
  </div>
  <div v-else class="loading">Загружаем агента с MCP…</div>
</template>

<script setup>
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { mcpQuestions, profileTemplates } from './demo-presets'

const state=ref(null), prompt=ref(''), busy=ref(false), error=ref(''), messagesEl=ref(null)
const failedAsk=ref(false)
const showCreate=ref(false), showSettings=ref(false), preview=ref(null), settingsTab=ref('profiles')
const memoryType=ref('knowledge'), memoryText=ref('')
const profileTemplate=ref('expert')
const blankProfile={name:'',role_context:'',language:'Русский',tone:'neutral',detail_level:'balanced',response_format:'paragraphs',constraints:''}
const createForm=reactive({...blankProfile}), profileForm=reactive({...blankProfile})
const taskForm=reactive({goal:'',hard_constraints:'',task_data:'',open_questions:''})
const controls=reactive({provider:'deepseek',model:'deepseek-flash',temperature:0,max_tokens:'',compression_mode:'full_session',window_size:6,batch_size:10,keep_recent:6})
const pendingAction=computed(()=>state.value?.pending_action || null)
const selectedCalendarId=ref(''), actionBusy=ref(false), actionNotice=ref('')
const actionStatuses=reactive({})

async function api(path,options={}){
  error.value=''
  const response=await fetch(path,{headers:{'Content-Type':'application/json'},...options})
  const data=await response.json().catch(()=>({}))
  if(!response.ok){
    const detail=data.detail
    const message=typeof detail?.message==='string' ? detail.message : typeof detail==='string' ? detail : 'Не удалось выполнить запрос'
    const stage=typeof detail?.provider_detail==='string' && /^Этап model_(first|second)_response(?:, HTTP [45]\d\d)?$/.test(detail.provider_detail) ? ` (${detail.provider_detail})` : ''
    throw new Error(`${message}${stage}`)
  }
  return data
}
function sync(next){
  failedAsk.value=false
  const previousActionId=state.value?.pending_action?.id
  state.value=next
  if(next.pending_action?.id !== previousActionId){
    const calendars=next.pending_action?.calendars || []
    selectedCalendarId.value=next.pending_action?.selected_calendar_id || (calendars.length===1 ? calendars[0].id : '')
    actionNotice.value=''
  }
  Object.assign(profileForm,Object.fromEntries(Object.keys(blankProfile).map(key=>[key,next.active_profile[key] ?? ''])))
  Object.assign(taskForm,next.task || {goal:'',hard_constraints:'',task_data:'',open_questions:''})
  nextTick(scrollBottom)
}
function scrollBottom(){if(messagesEl.value) messagesEl.value.scrollTop=messagesEl.value.scrollHeight}
async function mutate(path,method='POST',body){
  busy.value=true
  failedAsk.value=false
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
function openCreateProfile(){showSettings.value=false;showCreate.value=true}
function fillProfileTemplate(){Object.assign(createForm,profileTemplates[profileTemplate.value])}
async function saveTask(){await mutate('/api/task','PUT',taskForm)}
async function addLongTerm(){await mutate('/api/long-term','POST',{entry_type:memoryType.value,content:memoryText.value});if(!error.value)memoryText.value=''}
const deleteLongTerm=id=>mutate(`/api/long-term/${id}`,'DELETE')
function insertTest(question){prompt.value=question;nextTick(()=>document.querySelector('.composer textarea')?.focus())}
async function send(){
  if(busy.value || actionBusy.value || pendingAction.value || !prompt.value.trim())return
  const question=prompt.value;prompt.value='';busy.value=true;failedAsk.value=false;await nextTick();scrollBottom()
  try{
    const result=await api('/api/ask',{method:'POST',body:JSON.stringify({prompt:question,controls}),headers:{'Content-Type':'application/json'}})
    sync(result.state)
  }catch(e){prompt.value=question;error.value=e.message;failedAsk.value=true}finally{busy.value=false;await nextTick();scrollBottom()}
}
function displayDateTime(value){
  if(typeof value!=='string') return '—'
  const match=value.match(/^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?(Z|[+-]\d{2}:\d{2})$/)
  if(!match) return value
  return `${match[1]} · ${match[2]} · UTC${match[3]==='Z' ? '+00:00' : match[3]}`
}
function traceStatus(message){
  const step=traceSteps(message).at(-1)
  if(step?.name==='create_event' && message.id in actionStatuses) return actionStatuses[message.id]
  return step?.status
}
function traceSteps(message){
  const trace=message.preview?.mcp_trace
  if(Array.isArray(trace?.steps) && trace.steps.length) return trace.steps
  return trace?.called_tool ? [trace.called_tool] : []
}
function traceBadge(message){
  const step=traceSteps(message).at(-1)
  if(!step) return 'Без вызова'
  if(step.name==='clarify_event') return 'Уточнение · без нового MCP-вызова'
  return mcpStatusLabel(traceStatus(message))
}
function traceStepLabel(step){
  if(step.name==='list_upcoming_events') return 'MCP: чтение событий'
  if(step.name==='clarify_event') return 'Уточнение модели, без MCP'
  if(step.name==='create_event' && step.status==='proposed') return 'Проект события, без записи'
  if(step.name==='create_event') return 'MCP: запись события'
  return 'Инструмент'
}
function traceStepResult(message,step){
  if(step.name==='list_upcoming_events') return `days: ${step.arguments?.days ?? '—'} · ${mcpStatusLabel(step.status)}${step.event_count != null ? ` · событий: ${step.event_count}` : ''}`
  if(step.name==='clarify_event') return 'Агент задал вопрос; на этом шаге MCP не вызывался'
  if(step.name==='create_event') return mcpStatusLabel(step.status==='proposed' ? traceStatus(message) : step.status)
  return mcpStatusLabel(step.status)
}
async function resolveCalendarAction(kind){
  const action=pendingAction.value
  if(!action || actionBusy.value || busy.value) return
  failedAsk.value=false
  if(kind==='confirm' && !selectedCalendarId.value){error.value='Выберите календарь назначения.';return}
  actionBusy.value=true
  error.value=''
  try{
    const data=await api(`/api/calendar-actions/${encodeURIComponent(action.id)}/${kind}`,{
      method:'POST',body:JSON.stringify(kind==='confirm' ? {calendar_id:selectedCalendarId.value} : {})
    })
    const status=data.action?.status
    if(!['created','cancelled','unknown'].includes(status)){
      error.value='Операция ещё выполняется. Подождите и проверьте календарь перед повторной попыткой.'
      return
    }
    const proposal=[...state.value.active_session.messages].reverse().find(message=>message.preview?.mcp_trace?.called_tool?.status==='proposed')
    if(proposal) actionStatuses[proposal.id]=status
    sync(data.state)
    actionNotice.value=status==='created' ? 'Событие создано. Проверьте результат в Яндекс Календаре.' : status==='cancelled' ? 'Предложение отменено. Событие не создавалось.' : 'Исход записи неизвестен. Проверьте Яндекс Календарь; не подтверждайте повторно вслепую.'
  }catch(e){error.value=e.message}
  finally{actionBusy.value=false;await nextTick();scrollBottom()}
}
const confirmCalendarAction=()=>resolveCalendarAction('confirm')
const cancelCalendarAction=()=>resolveCalendarAction('cancel')
const toneLabel=value=>({friendly:'дружелюбно',neutral:'нейтрально',business:'по-деловому'}[value]||value)
const detailLabel=value=>({brief:'кратко',balanced:'баланс',deep:'глубоко'}[value]||value)
const formatLabel=value=>({paragraphs:'Абзацы',bullets:'Список',steps:'Шаги',table:'Таблица'}[value]||value)
const mcpStatusLabel=value=>({success:'Успешно',error:'Ошибка',proposed:'Предложено · без записи',created:'Создано',cancelled:'Отменено',unknown:'Исход неизвестен',executing:'Выполняется'}[value]||value||'Неизвестно')
watch(()=>state.value?.active_session?.messages?.length,()=>nextTick(scrollBottom))
watch(()=>controls.provider,(provider)=>{
  if(provider==='openrouter') controls.model='openrouter/free'
  else if(!['deepseek-flash','deepseek-v4-pro'].includes(controls.model)) controls.model='deepseek-flash'
})
onMounted(async()=>{try{sync(await api('/api/state'))}catch(e){error.value=e.message}})
</script>
