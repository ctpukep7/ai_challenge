// Те же вопросы, что в Day26: подстановка текста, без отправки модели.
export const PROMPT_PRESETS = Object.freeze([
  Object.freeze({ shortcut: '/1', title: 'Четыре свечи', prompt: 'В комнате горело 4 свечи. Две из них погасли. Сколько свечей осталось?' }),
  Object.freeze({ shortcut: '/2', title: 'Переправа животных', prompt: 'Хозяин хочет перевести через автодорогу своих домашних кошку, собаку и морскую свинку. Но он может взять в руки только двоих за один раз. Если кошка и собака останутся без присмотра на одной стороне, собака нападёт на кошку. Если кошка и морская свинка останутся без присмотра на одной стороне, кошка съест морскую свинку. Как хозяину переправить их всех через дорогу, никого не потеряв?' }),
  Object.freeze({ shortcut: '/3', title: 'Запрет буквы «а»', prompt: 'Напиши два предложения про зимний пейзаж за окном, выбирая слова так, чтобы ни разу не использовать букву «а».' }),
  Object.freeze({ shortcut: '/4', title: 'Алфавитный порядок', prompt: 'В лесу родилась ёлочка,\nВ лесу она росла.\nЗимой и летом стройная,\nЗелёная была.\n\nВыпиши все слова из этого текста в алфавитном порядке.' }),
]);

export function shortcutForKey(event, draft) {
  if (!['Tab', 'Enter'].includes(event.key) || event.shiftKey || event.ctrlKey
    || event.altKey || event.metaKey || event.isComposing || event.keyCode === 229) return null;
  const shortcut = draft.trim();
  return PROMPT_PRESETS.some((preset) => preset.shortcut === shortcut) ? shortcut : null;
}

export function insertPreset(state, shortcut) {
  if (state.pending) return state;
  const preset = PROMPT_PRESETS.find((item) => item.shortcut === shortcut);
  return preset ? { ...state, draft: preset.prompt } : state;
}
