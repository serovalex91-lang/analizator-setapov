#!/bin/bash

# Скрипт для запуска торговой системы
# Останавливает существующие процессы и запускает новые

echo "🔄 Остановка существующих процессов..."
pkill -f userbot.py
pkill -f uvicorn
sleep 3

# Устанавливаем свежесть сетапов из Setup Screener: 12 часов
export SETUP_MAX_AGE_HOURS=12

# Выбор интерпретатора Python: приоритет venv (жёстко), иначе macOS=python, Ubuntu/Linux=python3
ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$ROOT_DIR/venv"
if [ -d "$VENV_DIR" ]; then
    # Экспортируем окружение venv, чтобы все модули брались из него
    export VIRTUAL_ENV="$VENV_DIR"
    export PATH="$VENV_DIR/bin:$PATH"
    if [ -f "$VENV_DIR/bin/python" ]; then
        PY_BIN="$VENV_DIR/bin/python"
    elif [ -f "$VENV_DIR/bin/python3" ]; then
        PY_BIN="$VENV_DIR/bin/python3"
    else
        # На крайний случай, fallback по ОС
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

# Настройки вебхука Short (go short)
export SIGNAL_WEBHOOK_URL="https://hook.finandy.com/H8Dk_TPVeHfn0uHrrlUK"
export SIGNAL_WEBHOOK_SECRET="rf2kfxtfmm"
export SIGNAL_HOOK_NAME="go short "

# Включаем исполнение GO SHORT
export GO_SHORT_ENABLED=1

# Включаем исполнение GO LONG (вебхук для лонга вы пришлёте отдельно; логика уже активна)
export GO_LONG_ENABLED=1

# Настройки LONG вебхука для GO LONG
export SIGNAL_LONG_WEBHOOK_URL="https://hook.finandy.com/YdXLjVIuKkeOUsrrrlUK"
export SIGNAL_LONG_WEBHOOK_SECRET="oxyd9co5n5i"
export SIGNAL_LONG_HOOK_NAME="GO LONG"

echo "🚀 Запуск API сервера..."
cd intraday-levels-taapi
"$PY_BIN" -m uvicorn app.main_v2:app --host 0.0.0.0 --port 8001 &
API_PID=$!
echo "API PID: $API_PID"

# Ждем запуска API
echo "⏳ Ожидание запуска API..."
sleep 5

# Проверяем что API работает
for i in {1..10}; do
    if curl -s http://localhost:8001/health > /dev/null; then
        echo "✅ API сервер запущен"
        break
    else
        echo "⏳ Попытка $i/10: API еще не готов..."
        sleep 2
    fi
done

echo "🤖 Запуск Telegram бота..."
cd ..
"$PY_BIN" userbot.py &
BOT_PID=$!
echo "Bot PID: $BOT_PID"

echo "✅ Торговая система запущена!"
echo "📊 API сервер: PID $API_PID"
echo "🤖 Telegram бот: PID $BOT_PID"
echo "🛑 Для остановки: pkill -f userbot.py && pkill -f uvicorn"

# Запускаем в фоне и показываем логи
tail -f messages.log