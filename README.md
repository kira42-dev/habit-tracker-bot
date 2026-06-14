# Habit Tracker Bot

Telegram bot for tracking habits. Helps track daily and weekly habits with reminders and streak counting.

Built on python-telegram-bot v20+, SQLite.

## Что умеет

- Создание активностей с произвольным периодом (каждый день, раз в 3 дня, раз в неделю и т.д.)
- Отметка выполнения через inline-кнопки
- Расчет текущей и максимальной серии (streak)
- Напоминания с настраиваемым временем
- Автоматическое перепланирование напоминаний после отметки
- Хранение всей истории отметок

## Как запустить

```
pip install python-telegram-bot python-dotenv
```

Создать файл `.env`:

```
BOT_TOKEN=your_token_here
```

Запустить:

```
python habit_bot/bot.py
```

## Структура

```
habit_bot/
  bot.py          - основной код
  habits.db       - база данных (создается автоматически)
  start_bot.bat   - батник для запуска на Windows
```

## Планы развития

- Статистика за неделю/месяц с графиками
- Экспорт/импорт данных
- Возможность редактировать активность после создания
- Кастомные напоминания (несколько в день)
- Нормальный логгер вместо принтов
