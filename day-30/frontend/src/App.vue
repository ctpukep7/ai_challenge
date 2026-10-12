<script setup>
import {ref,computed,nextTick,onBeforeUnmount} from 'vue';
import {initialSession,clearSession,requestHistory,appendAnswer,statistics,apiRequest,failureMessage,serviceLimits} from './chat-state.js';
const session=ref(initialSession());
const tokenDraft=ref('');
const prompt=ref('');
const error=ref('');
const busy=ref(false);
const elapsed=ref(0);
const conversation=ref(null);
let controller=null,timer=null,deadline=null;
const loggedIn=computed(()=>Boolean(session.value.token&&session.value.limits));
const limits=computed(()=>session.value.limits);
function stopTimers(){clearInterval(timer);clearTimeout(deadline);timer=null;deadline=null;}
function begin(){busy.value=true;error.value='';elapsed.value=0;controller=new AbortController();timer=setInterval(()=>elapsed.value++,1000);deadline=setTimeout(()=>controller?.abort(),125000);}
function resetAuth(){session.value=clearSession();tokenDraft.value='';prompt.value='';}
function fail(cause){if(cause.status===401)resetAuth();error.value=failureMessage(cause);}
async function login(){const token=tokenDraft.value.trim();if(!token||busy.value)return;begin();try{const result=await apiRequest('/api/limits',token,undefined,{signal:controller.signal});session.value={token,limits:result,history:[]};tokenDraft.value='';}catch(cause){fail(cause);}finally{stopTimers();controller=null;busy.value=false;}}
function logout(){if(busy.value)return;resetAuth();error.value='';}
function newChat(){if(busy.value)return;session.value.history=[];prompt.value='';error.value='';}
async function send(){const text=prompt.value.trim();if(!text||busy.value||!loggedIn.value)return;begin();try{const response=await apiRequest('/api/chat',session.value.token,{prompt:text,history:requestHistory(session.value.history,limits.value.max_history_pairs)},{signal:controller.signal});if(typeof response.answer!=='string'||!response.answer.trim())throw new Error('Модель вернула пустой ответ.');session.value.history=appendAnswer(session.value.history,text,response);prompt.value='';await nextTick();conversation.value?.scrollIntoView({behavior:'smooth',block:'end'});}catch(cause){fail(cause);}finally{stopTimers();controller=null;busy.value=false;}}
function keydown(event){
  if(busy.value)return;
  if(event.key==='Enter'&&!event.shiftKey&&!event.ctrlKey&&!event.altKey&&!event.metaKey&&!event.isComposing&&event.keyCode!==229){event.preventDefault();send();}
}
onBeforeUnmount(()=>{controller?.abort();stopTimers();resetAuth();});
</script>
<template>
  <main class="shell">
    <header class="masthead">
      <div class="masthead-copy">
        <p class="eyebrow">DAY 30 · ДОМАШНИЙ СЕРВЕР</p>
        <h1>Приватный AI</h1>
        <p class="subtitle">Локальная модель, доступная в вашей сети.</p>
      </div>
      <span class="badge">HTTPS · локально</span>
    </header>

    <section v-if="!loggedIn" class="login panel" aria-labelledby="login-title">
      <div class="lock" aria-hidden="true">↳</div>
      <h2 id="login-title">Вход в сервис</h2>
      <p>Введите токен доступа, созданный на сервере. Он хранится только в памяти этой вкладки.</p>
      <form @submit.prevent="login">
        <label for="token">Токен доступа</label>
        <input id="token" v-model="tokenDraft" type="password" autocomplete="off"
          autocapitalize="off" spellcheck="false" :disabled="busy" required
          placeholder="Токен из файла access-token">
        <button type="submit" :disabled="busy || !tokenDraft.trim()">
          {{busy?'Проверяем доступ…':'Войти'}}
        </button>
      </form>
      <p v-if="error" role="alert" class="error">{{error}}</p>
      <p class="hint">На другом устройстве сначала подтвердите доверие сертификату этого сервера.</p>
    </section>

    <template v-else>
      <section class="service panel" aria-label="Состояние сервиса">
        <div class="service-heading">
          <div class="service-copy">
            <span class="status-dot"></span><strong>Доступ подтверждён</strong>
            <p class="model">{{limits.model}}</p>
          </div>
          <button class="secondary" :disabled="busy" @click="logout">Выйти</button>
        </div>
        <details class="service-details">
          <summary>Ограничения сервиса</summary>
          <dl class="limits">
            <div v-for="[label,value] in serviceLimits(limits)" :key="label">
              <dt>{{label}}</dt><dd>{{value}}</dd>
            </div>
          </dl>
          <p class="hint">Байтовый лимит включает системную инструкцию и историю; это не точный подсчёт токенов. Лимит запросов общий для сервиса.</p>
        </details>
      </section>

      <section class="chat panel" aria-labelledby="chat-title">
        <div class="chat-heading">
          <div class="chat-copy">
            <h2 id="chat-title">Ваш диалог</h2>
            <p class="hint">История исчезнет при закрытии или обновлении вкладки.</p>
          </div>
          <button class="secondary" :disabled="busy" @click="newChat">Новый чат</button>
        </div>
        <div v-if="!session.history.length" class="empty">
          <span aria-hidden="true">✦</span>
          <p>Начните со своего вопроса</p>
          <small>Модель может ошибаться. Важные ответы проверяйте.</small>
        </div>
        <div ref="conversation" class="conversation" aria-live="polite">
          <article v-for="(pair,index) in session.history" :key="index" class="turn">
            <div class="user-message">
              <p class="speaker">Вы</p>
              <p class="message-text">{{pair.user}}</p>
            </div>
            <div class="assistant-message">
              <p class="speaker">{{pair.response.model}}</p>
              <p class="message-text">{{pair.assistant}}</p>
              <p v-if="pair.response.truncated" class="warning">Ответ достиг лимита длины и может быть не закончен.</p>
              <dl class="statistics">
                <div v-for="[label,value] in statistics(pair.response.metrics)" :key="label">
                  <dt>{{label}}</dt><dd>{{value}}</dd>
                </div>
              </dl>
            </div>
          </article>
        </div>
        <form class="composer" @submit.prevent="send">
          <label for="prompt">Сообщение</label>
          <textarea id="prompt" v-model="prompt" rows="4" :disabled="busy"
            placeholder="О чём поговорим?" @keydown="keydown"></textarea>
          <div class="composer-footer">
            <p class="hint">Enter — отправить · Shift + Enter — новая строка</p>
            <button :disabled="busy || !prompt.trim()">
              {{busy?`Ожидание · ${elapsed} с`:'Отправить'}}
            </button>
          </div>
          <p v-if="busy" role="status" class="hint">Модель готовит ответ. Ожидание до 125 секунд, без автоматических повторов.</p>
          <p v-if="error" role="alert" class="error">{{error}}</p>
        </form>
      </section>
    </template>

    <footer>Сообщения обрабатываются на домашнем Mac. Доступность зависит от работы сервера и режима сна.</footer>
  </main>
</template>
