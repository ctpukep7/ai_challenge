import {renderDiff} from './diff.js';

export const DiffView = {
  props: {patch: {type: String, required: true}, filename: {type: String, default: ''}},
  data: () => ({mode: 'line-by-line'}),
  computed: {rendered() {return renderDiff(this.patch, this.mode)}},
  template: `<div class="visual-diff">
    <div class="diff-controls" role="group" aria-label="Режим сравнения изменений">
      <button type="button" class="secondary" :aria-pressed="mode==='line-by-line'" @click="mode='line-by-line'">Построчно</button>
      <button type="button" class="secondary" :aria-pressed="mode==='side-by-side'" @click="mode='side-by-side'">Рядом</button>
    </div>
    <p class="diff-legend"><span class="diff-removed">− Удалено</span> · <span class="diff-added">+ Добавлено</span><span v-if="mode==='side-by-side'"> · Слева — до, справа — после</span></p>
    <div v-if="rendered.html" class="diff-scroll" tabindex="0" role="region" :aria-label="'Изменения файла '+filename+': номера строк, удаления и добавления'" v-html="rendered.html"></div>
    <template v-else><p v-if="rendered.error" role="status">{{rendered.error}}</p><pre class="diff">{{patch}}</pre></template>
    <details class="raw-patch"><summary>Исходный diff</summary><pre class="diff">{{patch}}</pre></details>
  </div>`,
};
