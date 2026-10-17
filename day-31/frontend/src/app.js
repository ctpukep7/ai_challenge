import {nextHistory, groupSources, toolSteps, argumentsLabel, resultSummary} from './session.js';

export const App = {
  data: () => ({question: '', messages: [], history: [], index: null, pending: false, operation: '', error: ''}),
  mounted() { this.refresh(); },
  methods: {
    groupSources, toolSteps, argumentsLabel, resultSummary,
    catalog(trace = []) { return trace.find(item => item.method === 'tools/list')?.tools || []; },
    newConversation() {
      if (this.pending) return;
      this.question = ''; this.messages = []; this.history = []; this.error = '';
    },
    suggest(question) { if (!this.pending) this.question = question; },
    async request(path, body) {
      const response = await fetch(path, body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
      const data = await response.json();
      if (!response.ok) throw Error(data.detail || 'Ошибка запроса');
      return data;
    },
    async refresh() {
      try { this.index = await this.request('/api/index'); }
      catch (error) { this.error = error.message; }
    },
    async build() {
      if (this.pending) return;
      this.pending = true; this.operation = 'index'; this.error = '';
      try { this.index = await this.request('/api/index/build', {}); }
      catch (error) { this.error = error.message; }
      finally { this.pending = false; this.operation = ''; }
    },
    async send() {
      if (this.pending || !this.question.trim()) return;
      const question = this.question.trim();
      this.pending = true; this.operation = 'answer'; this.error = ''; this.question = '';
      this.messages.push({role: 'user', answer: question});
      try {
        const data = await this.request('/api/help', {question, history: this.history});
        this.messages.push({role: 'assistant', ...data});
        if (data.status !== 'error' && !data.partial) this.history = nextHistory(this.history, question, data.answer);
      } catch (error) { this.error = error.message; this.question = question; }
      finally { this.pending = false; this.operation = ''; }
    },
  },
  template: `<div class="shell">
    <header><div class="eyebrow">AI CHALLENGE · PIPELINE / DAY 31</div><h1>Ассистент разработчика</h1><p>Документация проекта и актуальный контекст Git в одном диалоге.</p></header>
    <section class="toolbar"><div><strong>База знаний</strong><p v-if="index">{{index.documents}} документов · {{index.chunks}} фрагментов · {{!index.ready?'не построена':index.stale?'требует обновления':'актуальна'}}</p><p v-else>Проверка локальной модели…</p></div><button @click="build" :disabled="pending">{{index?.ready?'Обновить индекс':'Построить индекс'}}</button></section>
    <div class="conversation-bar"><span>Диалог · последние 4 пары в контексте</span><button class="secondary" @click="newConversation" :disabled="pending || !messages.length">Новая беседа</button></div>
    <main aria-live="polite" :aria-busy="pending">
      <div v-if="!messages.length" class="empty"><h2>С чего начнём?</h2><p>Введите /help для обзора или спросите: «Где находится backend Day31?»</p><p>Модель сама выбирает инструменты MCP: поиск README/docs, текущий Git, чтение публичных исходников и diff. Все операции — только чтение.</p></div>
      <article v-for="(message,i) in messages" :key="i" :class="message.role">
        <div class="label">{{message.role==='user'?'Вы':'Ассистент'}}</div>
        <p v-if="message.status==='unknown'" class="warning">Недостаточно данных</p>
        <p v-if="message.status==='error'" class="error" role="alert">Ошибка выполнения</p>
        <p v-if="message.validation?.repaired" class="success">Цитирование исправлено после проверки · дополнительный шаг модели</p>
        <p v-else-if="message.validation?.repair_attempted" class="warning">Выполнена попытка исправления цитирования</p>
        <p v-if="message.partial" class="warning">Частичный ответ · неподтверждённые части не показаны</p>
        <p class="answer">{{message.answer}}</p>
        <p v-if="message.metrics" class="metrics">{{message.metrics.total_seconds}} с · LLM: {{message.metrics.llm_calls}} · MCP: {{message.metrics.tool_calls}} · Токены: {{message.metrics.total_tokens ?? 'нет данных'}}<span v-if="message.metrics.total_tokens!=null"> (вход {{message.metrics.prompt_tokens ?? '?'}} / выход {{message.metrics.completion_tokens ?? '?'}})</span></p>
        <p v-for="warning in message.warnings" :key="warning" class="warning">{{warning}}</p>
        <section v-if="message.suggestions?.length" class="suggestions"><strong>Можно спросить · подсказки интерфейса</strong><button v-for="question in message.suggestions" :key="question" class="suggestion" :disabled="pending" @click="suggest(question)">{{question}}</button></section>
        <section v-if="message.context?.git_branch" class="context"><strong>Текущий Git · MCP</strong><p>{{message.context.git_branch.detached?'Detached HEAD':message.context.git_branch.branch}} · {{message.context.git_branch.sha?.slice(0,10)}}</p></section>
        <details v-if="message.sources?.length" class="sources"><summary>Подтверждённые источники · {{groupSources(message.sources).length}}</summary>
          <div v-for="source in groupSources(message.sources)" :key="source.groupKey" class="source"><strong>{{({documentation:'Документация',code:'Код',git:'Git',diff:'Diff'})[source.kind] || 'Источник'}} · {{source.id}}</strong>
            <p v-if="source.path">{{source.path}}<span v-if="source.start_line">:{{source.start_line}}–{{source.end_line}}</span></p><p>{{source.section}}<span v-if="source.retrieval_role==='neighbor'"> · соседний фрагмент</span></p>
            <blockquote v-for="quote in source.quotes" :key="quote">{{quote}}</blockquote>
            <details><summary>Полный фрагмент</summary><pre>{{source.text}}</pre></details>
          </div>
        </details>
        <section v-if="message.trace?.length" class="tools"><h3>Работа с проектом</h3>
          <ol v-if="toolSteps(message.trace).length" class="tool-steps"><li v-for="step in toolSteps(message.trace)" :key="step.tool_call_id" class="tool-step">
            <div class="tool-heading"><strong>{{step.label}}</strong><span :class="step.failed?'warning':'success'">{{step.rejected?'Отклонён до MCP':step.failed?'Ошибка':'Выполнен'}} · {{step.seconds}} с</span></div>
            <code class="tool-name">{{step.tool}}</code><p class="tool-arguments">{{argumentsLabel(step.arguments)}}</p>
            <details><summary>Результат инструмента</summary><p :class="{warning:step.failed}">{{resultSummary(step.result)}}</p>
              <p v-if="step.result?.scope">Область: {{step.result.scope}}<span v-if="step.result.day"> · день {{step.result.day}}</span></p>
              <ul v-if="step.result?.files" class="file-list"><li v-for="file in step.result.files" :key="file">{{file}}</li></ul>
              <div v-for="source in step.result?.evidence" :key="source.id" class="tool-evidence"><strong>{{source.id}}<span v-if="source.path"> · {{source.path}}:{{source.start_line}}–{{source.end_line}}</span></strong><pre>{{source.text}}</pre></div>
            </details>
          </li></ol>
          <details v-if="catalog(message.trace).length"><summary>Каталог MCP · {{catalog(message.trace).length}} инструментов</summary><p>Discovery: доступные инструменты. Это не выполнение инструмента.</p><ul><li v-for="tool in catalog(message.trace)" :key="tool.function.name"><code>{{tool.function.name}}</code></li></ul></details>
          <details class="technical"><summary>Технический JSON трассы</summary><pre>{{JSON.stringify(message.trace,null,2)}}</pre></details>
        </section>
      </article>
      <p v-if="pending" class="pending" role="status">{{operation==='index'?'Обновляем локальный индекс…':'Ассистент обрабатывает вопрос и при необходимости вызывает инструменты…'}}</p>
    </main>
    <p v-if="error" class="error" role="alert">{{error}}</p>
    <form @submit.prevent="send"><label for="question">Вопрос о проекте</label><div class="composer"><textarea id="question" v-model="question" placeholder="/help Как устроен RAG?" maxlength="4000" :disabled="pending" @keydown.enter="event=>{if(!event.shiftKey){event.preventDefault();send()}}"></textarea><button :disabled="pending||!question.trim()">Отправить</button></div><small>Enter — отправить · Shift+Enter — новая строка · История хранится только в этой вкладке · DeepSeek получает вопрос, 4 пары истории и результаты выбранных инструментов</small></form>
  </div>`,
};
