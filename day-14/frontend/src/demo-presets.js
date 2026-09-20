export const backendDemoTask = {
  goal: 'Улучшить поддерживаемость backend учебного приложения.',
  hard_constraints: 'Сохранить существующие API и данные. Не переписывать frontend.',
  task_data: 'Приложение использует FastAPI и SQLite. По мере роста проекта API-обработчики содержат всё больше бизнес-логики и обращений к хранилищу.',
  open_questions: 'Как разделить API, service layer и storage? Какие изменения внести первыми, не ломая текущие контракты?',
}

export const demoScenarios = {
  stack: {
    label: 'Стек FastAPI',
    title: 'Python/FastAPI остаётся',
    description: 'Правило стека блокирует переход на другой backend-фреймворк.',
    invariant: {
      category: 'stack', field: 'backend_framework', operator: 'equals', expected_value: 'fastapi',
      description: 'Backend остаётся на Python/FastAPI',
      reason: 'Сохраняем принятый стек, совместимость и учебную архитектуру проекта.',
      compatible_alternative: 'fastapi', active: true,
    },
    candidates: {
      conflict: {
        button: '1. Подставить конфликт Node.js', field: 'backend_framework', value: 'nodejs',
        proposal: 'Переписать backend на Node.js и Express.',
        prompt: 'Оцени предложение переписать backend на Node.js/Express и составь план миграции.',
      },
      compatible: {
        button: '2. Подставить совместимый FastAPI', field: 'backend_framework', value: 'fastapi',
        proposal: 'Улучшить архитектуру FastAPI: выделить service layer и сохранить текущий стек.',
        prompt: 'Предложи улучшение архитектуры FastAPI через service layer без смены текущего стека.',
      },
    },
    steps: [
      ['Сохраните контекст', 'Задача описывает backend, но сама не запрещает смену стека.'],
      ['Сохраните правило', 'backend_framework должен оставаться fastapi.'],
      ['Отправьте Node.js', 'Получите deterministic refusal до вызова модели.'],
      ['Отправьте FastAPI', 'Совместимое улучшение дойдёт до модели.'],
      ['Проверьте snapshot', 'Затем отключите правило и повторите конфликт.'],
    ],
  },
  privacy: {
    label: 'Приватность данных',
    title: 'Данные остаются внутри',
    description: 'Правило приватности блокирует передачу пользовательских данных наружу.',
    invariant: {
      category: 'business_rule', field: 'customer_data_destination', operator: 'not_one_of',
      expected_value: 'external_ai_service|personal_email',
      description: 'Пользовательские данные остаются во внутреннем контуре',
      reason: 'Персональные данные нельзя передавать внешним сервисам или по личной почте.',
      compatible_alternative: 'internal_storage', active: true,
    },
    candidates: {
      conflict: {
        button: '1. Подставить внешний AI-сервис', field: 'customer_data_destination', value: 'external_ai_service',
        proposal: 'Передать пользовательские данные во внешний AI-сервис для обработки.',
        prompt: 'Оцени предложение отправить пользовательские данные во внешний AI-сервис для анализа.',
      },
      compatible: {
        button: '2. Подставить локальную обработку', field: 'customer_data_destination', value: 'internal_storage',
        proposal: 'Обрабатывать пользовательские данные локально и хранить их во внутреннем контуре.',
        prompt: 'Предложи вариант локальной обработки пользовательских данных во внутреннем контуре.',
      },
    },
    steps: [
      ['Сохраните контекст', 'Рабочая задача остаётся общей и не подменяет правило приватности.'],
      ['Сохраните правило', 'Внешний AI-сервис и личная почта запрещены.'],
      ['Отправьте внешний сервис', 'Получите deterministic refusal до вызова модели.'],
      ['Отправьте локальную обработку', 'Внутреннее хранилище пройдёт preflight.'],
      ['Проверьте snapshot', 'Затем отключите правило и повторите конфликт.'],
    ],
  },
}
