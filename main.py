import asyncio
import hmac
import logging
from aiohttp import web
from aiogram import Bot, Dispatcher
import config
from database.db import init_db
from services import yclients_api, webhook_handler
from services.scheduler import scheduler, restore_all_reminders
from handlers.common import router as common_router
from handlers.booking import router as booking_router
from handlers.admin import router as admin_router

_background_tasks: set[asyncio.Task] = set()


async def health_check_handler(request: web.Request) -> web.Response:
    """Эндпоинт проверки работоспособности сервиса."""
    return web.json_response({"status": "ok"})


async def _safe_process_webhook(payload: dict, bot: Bot):
    try:
        await webhook_handler.handle_webhook_event(payload, bot)
    except Exception as e:
        logging.exception("Необработанная ошибка при обработке вебхука YCLIENTS: %s", e)


async def yclients_webhook_handler(request: web.Request) -> web.Response:
    """
    Приём вебхуков от YCLIENTS.
    Проверяет секретный токен через hmac.compare_digest, моментально возвращает 200 OK
    и запускает обработку события в фоновом таске с защитой от сборщика мусора.
    """
    secret = (
        request.match_info.get("secret")
        or request.query.get("secret")
        or request.headers.get("X-Hook-Secret")
    )
    expected_secret = config.YCLIENTS_WEBHOOK_SECRET
    if not expected_secret or not secret or not hmac.compare_digest(str(secret), str(expected_secret)):
        return web.Response(status=403, text="Forbidden")

    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            return web.Response(status=400, text="Bad Request")
    except Exception:
        try:
            post_data = await request.post()
            if not post_data:
                return web.Response(status=400, text="Bad Request")
            payload = dict(post_data)
        except Exception:
            return web.Response(status=400, text="Bad Request")

    bot: Bot = request.app["bot"]
    task = asyncio.create_task(_safe_process_webhook(payload, bot))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return web.json_response({"status": "ok"})


async def main():
    logging.basicConfig(level=logging.INFO)

    # 1. Инициализируем базу данных SQLite
    await init_db()

    bot = Bot(token=config.BOT_TOKEN)
    dp = Dispatcher()

    dp.include_router(admin_router)
    dp.include_router(common_router)
    dp.include_router(booking_router)

    # 2. Запускаем планировщик задач и восстанавливаем таймеры из БД
    scheduler.start()
    await restore_all_reminders(bot)

    # 3. Поднимаем встроенный HTTP-сервер для вебхуков YCLIENTS
    web_runner: web.AppRunner | None = None
    if config.YCLIENTS_WEBHOOK_ENABLED:
        app = web.Application()
        app["bot"] = bot
        app.router.add_get("/health", health_check_handler)
        app.router.add_post("/yclients/webhook", yclients_webhook_handler)
        app.router.add_post("/yclients/webhook/{secret}", yclients_webhook_handler)

        web_runner = web.AppRunner(app)
        await web_runner.setup()
        site = web.TCPSite(web_runner, host=config.YCLIENTS_WEBHOOK_HOST, port=config.YCLIENTS_WEBHOOK_PORT)
        await site.start()
        print(f"HTTP-сервер вебхуков YCLIENTS запущен на http://{config.YCLIENTS_WEBHOOK_HOST}:{config.YCLIENTS_WEBHOOK_PORT}")

    print("Админ-панель и планировщик активны. Бот слушает команды...")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        if _background_tasks:
            await asyncio.gather(*_background_tasks, return_exceptions=True)
        if web_runner:
            await web_runner.cleanup()
        if scheduler.running:
            scheduler.shutdown(wait=False)
        await yclients_api.close_http_session()
        try:
            await bot.session.close()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Бот остановлен.")