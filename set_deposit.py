#!/usr/bin/env python3
"""
Скрипт для изменения размера депозита в userbot.py
"""
import sys
import re

def update_deposit(new_amount):
    """Обновляет размер депозита в userbot.py"""
    try:
        # Читаем файл
        with open('userbot.py', 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Заменяем значение депозита
        pattern = r'DEPOSIT_AMOUNT = \d+\.?\d*'
        replacement = f'DEPOSIT_AMOUNT = {new_amount}'
        
        if re.search(pattern, content):
            new_content = re.sub(pattern, replacement, content)
            
            # Записываем обратно
            with open('userbot.py', 'w', encoding='utf-8') as f:
                f.write(new_content)
            
            print(f"✅ Депозит успешно изменен на {new_amount} USDT")
            return True
        else:
            print("❌ Не найдена переменная DEPOSIT_AMOUNT в userbot.py")
            return False
            
    except Exception as e:
        print(f"❌ Ошибка при изменении депозита: {e}")
        return False

def get_current_deposit():
    """Получает текущий размер депозита"""
    try:
        with open('userbot.py', 'r', encoding='utf-8') as f:
            content = f.read()
        
        pattern = r'DEPOSIT_AMOUNT = (\d+\.?\d*)'
        match = re.search(pattern, content)
        
        if match:
            return float(match.group(1))
        else:
            return None
    except Exception as e:
        print(f"❌ Ошибка при чтении депозита: {e}")
        return None

def main():
    if len(sys.argv) != 2:
        current = get_current_deposit()
        if current:
            print(f"📊 Текущий депозит: {current} USDT")
        print("\n💡 Использование:")
        print("  python set_deposit.py <сумма>")
        print("  Пример: python set_deposit.py 2000")
        print("  Пример: python set_deposit.py 500.5")
        return
    
    try:
        new_amount = float(sys.argv[1])
        if new_amount <= 0:
            print("❌ Сумма депозита должна быть больше 0")
            return
        
        if update_deposit(new_amount):
            print(f"🔄 Перезапустите бота для применения изменений:")
            print("  pkill -f userbot.py")
            print("  source venv/bin/activate && python userbot.py")
        
    except ValueError:
        print("❌ Неверный формат суммы. Используйте числа (например: 2000 или 500.5)")

if __name__ == "__main__":
    main()
