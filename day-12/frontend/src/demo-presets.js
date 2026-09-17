export const personalizationQuestions = [
  {
    label: 'Объяснить RAG',
    prompt: 'Объясни, что такое RAG, когда он нужен и какие у него риски.',
  },
  {
    label: 'Выбрать подход',
    prompt: 'Для базы знаний поддержки на 100 000 документов, которая обновляется ежедневно, выбрать RAG, fine-tuning или поиск без LLM? Обоснуй решение, риски и первый шаг.',
  },
]

export const profileTemplates = {
  expert: {
    name: 'Эксперт',
    role_context: 'Я технический специалист и знаком с архитектурой AI-систем.',
    language: 'Русский',
    tone: 'neutral',
    detail_level: 'deep',
    response_format: 'paragraphs',
    constraints: 'Явно называй допущения, компромиссы, ограничения и edge cases.',
  },
  manager: {
    name: 'Руководитель',
    role_context: 'Я принимаю продуктовые и технические решения.',
    language: 'Русский',
    tone: 'business',
    detail_level: 'brief',
    response_format: 'bullets',
    constraints: 'Начни с вывода; выдели риски, решение и следующие шаги.',
  },
}
