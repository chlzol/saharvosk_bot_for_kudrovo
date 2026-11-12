import asyncio
import logging
from aiogram import Bot, Dispatcher
from config import BOT_TOKEN
from database.db import init_db
from services import yclients_api
from services.scheduler import scheduler, restore_all_reminders
from handlers.common import router as common_router
from handlers.booking import router as booking_router
from handlers.admin import router as admin_router


async def main():
    logging.basicConfig(level=logging.INFO)

    # 1. Инициализируем базу данных SQLite
    await init_db()

    # Если используете session для прокси, оставьте её здесь
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    dp.include_router(admin_router)
    dp.include_router(common_router)
    dp.include_router(booking_router)

    # 2. Запускаем планировщик задач и восстанавливаем таймеры из БД
    scheduler.start()
    await restore_all_reminders(bot)

    print("Админ-панель и планировщик активны. Бот слушает команды...")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        await yclients_api.close_http_session()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Бот остановлен.")