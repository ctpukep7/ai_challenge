<script setup>
import { computed } from 'vue'
import { renderMarkdown } from '../markdown'

const props = defineProps({ content: { type: String, default: '' } })
const html = computed(() => renderMarkdown(props.content))
</script>

<template>
  <!-- Only the parser output is HTML; raw HTML and automatic images are disabled. -->
  <div class="markdown-content" v-html="html"></div>
</template>
