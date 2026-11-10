# Sahar&Vosk Telegram Bot Project Rules

## Стек и архитектура
- Стек: Python 3.11+, aiogram 3.x, aiosqlite, APScheduler, aiohttp (YCLIENTS API v1).
- База данных: локальный SQLite (saharvosk.db) с таблицами users, appointments, reviews, step_content, support_messages.
- Все текстовые экраны и картинки управляются через таблицу step_content (функция render_step).

## Критические ограничения проекта
1. Бренд: строго "Sahar&Vosk" (не использовать "Сахар & Воск").
2. Часовой пояс: строго Европа/Москва (UTC+3). Использовать функцию now_msk() или таймзону Europe/Moscow, никогда не полагаться на системный datetime.now().
3. Запрещены синхронные блокирующие вызовы (time.sleep, requests, sqlite3) — только asyncio, aiohttp, aiosqlite.
4. YCLIENTS API: слоты проверяются асинхронно, отмена записей очищает задачи в APScheduler.
5. Не перезаписывать step_content в init_db при перезапуске, если там уже есть кастомный текст.