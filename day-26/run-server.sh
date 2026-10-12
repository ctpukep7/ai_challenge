#!/bin/sh
# Только foreground: Ctrl+C останавливает наш процесс, автозапуск не создаётся.
set -eu
DAY_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(dirname "$DAY_DIR")
OLLAMA_EXEC=""
if [ -n "${OLLAMA_BIN:-}" ]; then
  if [ ! -x "$OLLAMA_BIN" ]; then
    echo "Ошибка: OLLAMA_BIN не указывает на исполняемый файл." >&2
    exit 1
  fi
  OLLAMA_EXEC=$OLLAMA_BIN
elif command -v ollama >/dev/null 2>&1; then
  OLLAMA_EXEC=$(command -v ollama)
else
  for candidate in "$PROJECT_DIR/.local-llm/runtime/bin/ollama" "$PROJECT_DIR/.local-llm/runtime/ollama" "/Applications/Ollama.app/Contents/Resources/ollama" "$HOME/Applications/Ollama.app/Contents/Resources/ollama" "$HOME/.local/bin/ollama"; do
    if [ -x "$candidate" ]; then
      OLLAMA_EXEC=$candidate
      break
    fi
  done
fi
if [ -z "$OLLAMA_EXEC" ]; then
  echo "Ошибка: Ollama не найдена. Установите официальный runtime или задайте OLLAMA_BIN." >&2
  exit 1
fi
export OLLAMA_MODELS="$PROJECT_DIR/.local-llm/models"
export OLLAMA_HOST=127.0.0.1:11434
export OLLAMA_NO_CLOUD=1
export OLLAMA_MAX_LOADED_MODELS=1
export OLLAMA_NUM_PARALLEL=1
export OLLAMA_CONTEXT_LENGTH=4096
export OLLAMA_KEEP_ALIVE=1m
# При занятом порте Ollama завершится с ошибкой; чужой сервер не останавливаем.
exec "$OLLAMA_EXEC" serve
