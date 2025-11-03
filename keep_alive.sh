#!/bin/bash

# Скрипт для поддержания работы торговой системы
# Проверяет каждые 30 секунд и перезапускает при необходимости

echo "🔄 Запуск системы мониторинга торговой системы..."

while true; do
    # Проверяем API сервер
    if ! curl -s http://localhost:8001/health > /dev/null 2>&1; then
        echo "❌ API сервер не отвечает, перезапускаем..."
        pkill -f uvicorn
        sleep 2
        cd intraday-levels-taapi
        source ../venv/bin/activate
        python -m uvicorn app.main_v2:app --host 0.0.0.0 --port 8001 &
        cd ..
        echo "✅ API сервер перезапущен"
    fi
    
    # Проверяем Telegram бота
    if ! pgrep -f "python userbot.py" > /dev/null; then
        echo "❌ Telegram бот не работает, перезапускаем..."
        pkill -f userbot.py
        sleep 2
        source venv/bin/activate
        python userbot.py &
        echo "✅ Telegram бот перезапущен"
    fi
    
    # Ждем 30 секунд до следующей проверки
    sleep 30
done

