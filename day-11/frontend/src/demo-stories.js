export const storyList = [
  { id: "game_of_thrones", label: "Задача 1 · «Игра престолов»" },
  { id: "the_witcher", label: "Задача 2 · «Ведьмак»" },
];

export const longTermSuggestions = [
  {
    label: "Эксперт по сериалам",
    content: "Ты эксперт по телевизионным сериалам и отвечаешь по экранному канону.",
  },
  {
    label: "Эксперт по книгам",
    content: "Ты эксперт по книгам и отвечаешь по книжному канону.",
  },
];

export const stories = {
  game_of_thrones: {
    label: "Задача 1 · «Игра престолов»",
    goal: "Тема разговора — «Игра престолов»",
    hard_constraints: "",
    task_data: "",
    open_questions: "",
  },
  the_witcher: {
    label: "Задача 2 · «Ведьмак»",
    goal: "Тема разговора — «Ведьмак»",
    hard_constraints: "",
    task_data: "",
    open_questions: "",
  },
};

export const dialogs = {
  game_of_thrones: [
    {
      text: "Перескажи концовку.",
      hint: "Игра престолов · вопрос 1/3 · концовка",
    },
    {
      text: "Как звали сестру Теона Грейджоя?",
      hint: "Игра престолов · вопрос 2/3 · сестра Теона",
    },
    {
      text: "Что стало с Кейтлин Старк?",
      hint: "Игра престолов · вопрос 3/3 · Кейтлин Старк",
    },
  ],
  the_witcher: [
    {
      text: "Перескажи концовку.",
      before: "Сначала создайте новую сессию: диалог об «Игре престолов» исчезнет из контекста, а долговременная память и новая тема останутся.",
      hint: "Ведьмак · вопрос 1/3 · концовка",
      requiresFreshSession: true,
    },
    {
      text: "Кем Цири приходится Геральту?",
      hint: "Ведьмак · вопрос 2/3 · Цири и Геральт",
    },
    {
      text: "Что стало с Йеннифэр?",
      hint: "Ведьмак · вопрос 3/3 · Йеннифэр",
    },
  ],
};

export function dialogStepsForStory(storyId) {
  return (dialogs[storyId] || []).map((step, index, steps) => ({
    ...step,
    stepNumber: index + 1,
    stepsInDialog: steps.length,
  }));
}
