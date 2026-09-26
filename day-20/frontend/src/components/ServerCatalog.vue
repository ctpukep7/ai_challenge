<script setup>
defineProps({ servers: { type: Array, default: () => [] } })
const labels = { weather: 'Погода', calendar: 'Календарь', plans: 'Планы' }
const descriptions = { weather: 'Open-Meteo · почасовой прогноз', calendar: 'Яндекс · свободные интервалы', plans: 'SQLite · сохранённые проекты' }
</script>

<template>
  <section class="panel server-panel" aria-labelledby="servers-heading">
    <div class="section-heading">
      <h2 id="servers-heading">MCP-серверы</h2>
      <span class="subtle">{{ servers.length }} подключения</span>
    </div>
    <p v-if="!servers.length" class="small empty-copy">Каталог появится после подключения серверов.</p>
    <article v-for="server in servers" :key="server.id" class="server-card">
      <header>
        <div class="server-icon" :class="server.id">{{ server.id === 'weather' ? '☀' : server.id === 'calendar' ? '▦' : '▤' }}</div>
        <div><h3>{{ labels[server.id] || server.name || server.id }}</h3><p>{{ descriptions[server.id] || server.description }}</p></div>
        <span class="connection-dot" :class="server.status || 'ready'" :title="server.status === 'ready' ? 'MCP подключён' : 'Состояние MCP: ' + server.status"></span>
      </header>
      <ul class="tool-list">
        <li v-for="tool in server.tools || []" :key="tool.alias || tool.name">
          <code>{{ tool.alias || `${server.id}__${tool.name}` }}</code>
          <span v-if="tool.confirmation_only || tool.name === 'create_trip_event'" class="approval-tag">После подтверждения</span>
        </li>
      </ul>
      <p v-if="server.error" class="inline-error">{{ server.error }}</p>
    </article>
    <p class="catalog-note">Агент выбирает инструмент. Каждый вызов проходит через соответствующий сервер.</p>
  </section>
</template>
