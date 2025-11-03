#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "🔄 Остановка предыдущего экземпляра newbot..."
pkill -f newbot.py || true
sleep 1

export SETUP_MAX_AGE_HOURS="${SETUP_MAX_AGE_HOURS:-12}"

# Определяем Python интерпретатор: venv приоритетно и жёстко, иначе macOS=python, Linux=python3
ROOT_DIR="$(pwd)"
VENV_DIR="$ROOT_DIR/venv"
if [ -d "$VENV_DIR" ]; then
  export VIRTUAL_ENV="$VENV_DIR"
  export PATH="$VENV_DIR/bin:$PATH"
  if [ -x "$VENV_DIR/bin/python" ]; then
    PY_BIN="$VENV_DIR/bin/python"
  elif [ -x "$VENV_DIR/bin/python3" ]; then
    PY_BIN="$VENV_DIR/bin/python3"
  else
    case "$(uname -s)" in
      Darwin)
        PY_BIN="python"
        ;;
      *)
        PY_BIN="python3"
        ;;
    esac
  fi
else
  case "$(uname -s)" in
    Darwin)
      PY_BIN="python"
      ;;
    *)
      PY_BIN="python3"
      ;;
  esac
fi

echo "🚀 Запуск newbot..."
"$PY_BIN" newbot.py &
echo "✅ newbot запущен в фоне"


