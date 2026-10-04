<script setup>
defineProps({settings:Object,disabled:Boolean})
</script>
<template><div class="settings"><label>Разбиение<select v-model="settings.strategy" :disabled="disabled"><option value="structure">По структуре</option><option value="fixed">Фиксированный размер</option></select></label><label>До фильтра<input v-model.number="settings.top_k_before" type="number" min="1" max="30" :disabled="disabled"></label><label>После фильтра<input v-model.number="settings.top_k_after" type="number" min="1" :max="settings.top_k_before" :disabled="disabled"></label><label>Порог cosine<input v-model.number="settings.threshold" type="number" min="-1" max="1" step="0.01" :disabled="disabled"></label></div></template>
