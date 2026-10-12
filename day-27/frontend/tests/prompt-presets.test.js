import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { createChatState, utf8Bytes } from '../src/chat-state.js';
import { PROMPT_PRESETS, insertPreset, shortcutForKey } from '../src/prompt-presets.js';

// SHA-256 точных UTF-8 текстов day-26/prompt_presets.py, включая переводы строк.
const day26Hashes = {
  '/1': '03f02f83e7eba4c6e712a7938138a1e3aa11d29f0ccd3418abc16fac8c1cfa83',
  '/2': '5173247b3d2c79db5d2abe4c90d511ff4760f8fed4416c5ee39aa8a649c4af71',
  '/3': '0147ea6fb064756b6c1907c4c59a4ec52bb90dff0108613bf96b13493458fbd2',
  '/4': '7b262ef2b010a9fe2b3ff74778ca012ac80a36c3b3914ce641e246201844fca3',
};

test('все четыре вопроса точно совпадают с Day26 и укладываются в лимит', () => {
  assert.deepEqual(PROMPT_PRESETS.map((preset) => preset.shortcut), Object.keys(day26Hashes));
  for (const preset of PROMPT_PRESETS) {
    assert.equal(createHash('sha256').update(preset.prompt, 'utf8').digest('hex'), day26Hashes[preset.shortcut]);
    assert.ok(utf8Bytes(preset.prompt) <= 3000);
  }
  assert.equal(PROMPT_PRESETS[3].prompt.split('\n').length, 6);
});

test('подстановка заменяет только редактируемый черновик, не историю и сообщения', () => {
  const original = {
    ...createChatState(), draft: 'Мой черновик', error: 'Прежняя ошибка', notice: 'Прежняя заметка',
    history: [{ role: 'user', content: 'Предыдущий вопрос' }, { role: 'assistant', content: 'Ответ' }],
    messages: [{ role: 'user', content: 'Предыдущий вопрос' }, { role: 'assistant', content: 'Ответ' }],
  };
  for (const preset of PROMPT_PRESETS) {
    const next = insertPreset(original, preset.shortcut);
    assert.deepEqual(next, { ...original, draft: preset.prompt });
    assert.equal(next.history, original.history);
    assert.equal(next.messages, original.messages);
    assert.equal(next.pending, null);
  }
  assert.equal(original.draft, 'Мой черновик');
});

test('подстановка запрещена во время генерации, даже при прямом вызове', () => {
  const state = { ...createChatState(), pending: 'Текущий запрос' };
  for (const preset of PROMPT_PRESETS) assert.equal(insertPreset(state, preset.shortcut), state);
});

test('Tab или Enter раскрывают только точный код всего поля', () => {
  for (const key of ['Tab', 'Enter']) {
    for (const preset of PROMPT_PRESETS) {
      assert.equal(shortcutForKey({ key }, preset.shortcut), preset.shortcut);
      assert.equal(shortcutForKey({ key }, `  ${preset.shortcut}\n`), preset.shortcut);
      assert.equal(shortcutForKey({ key }, preset.prompt), null);
    }
    for (const draft of ['', '/5', '/10', 'Объясни /1', '/1 затем /2', '/1\nвопрос']) {
      assert.equal(shortcutForKey({ key }, draft), null);
    }
  }
});

test('обычный Tab, Shift+Tab, IME и модификаторы не раскрывают вопросы', () => {
  assert.equal(shortcutForKey({ key: 'Tab' }, 'Обычный вопрос'), null);
  for (const key of ['Tab', 'Enter']) {
    for (const modifier of ['shiftKey', 'ctrlKey', 'altKey', 'metaKey', 'isComposing']) {
      assert.equal(shortcutForKey({ key, [modifier]: true }, '/1'), null);
    }
    assert.equal(shortcutForKey({ key, keyCode: 229 }, '/1'), null);
  }
  assert.equal(shortcutForKey({ key: 'a' }, '/1'), null);
});

test('неизвестный код не меняет черновик', () => {
  const state = { ...createChatState(), draft: 'Оставить этот текст' };
  assert.equal(insertPreset(state, '/unknown'), state);
});
