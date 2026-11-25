import asyncio
import hmac
import logging
from aiohttp import web
from aiogram import Bot, Dispatcher
import config
from database.db import init_db, now_msk
from services import yclients_api, webhook_handler, max_sender, health_monitor
from services.scheduler import scheduler, restore_all_reminders
from handlers.common import router as common_router
from handlers.booking import router as booking_router
from handlers.admin import router as admin_router

BOT_STARTED_AT = now_msk()
health_monitor.set_bot_started_at(BOT_STARTED_AT)
_background_tasks: set[asyncio.Task] = set()
webhook_handler.register_background_tasks_ref(_background_tasks)


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
        webhook_handler.record_webhook_auth_rejected()
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


async def shutdown_resources(bot: Bot | None, web_runner: web.AppRunner | None = None) -> None:
    """
    Корректный упорядоченный shutdown всех подсистем (SIGTERM / Ctrl+C):
    1) остановка приёма входящих вебхуков (poll updates уже остановлен к этому моменту);
    2) ожидание фоновых webhook-задач с таймаутом и последующей отменой;
    3) остановка планировщика APScheduler;
    4) независимое закрытие aiohttp-сессии YCLIENTS, клиента MAX и сессии Telegram Bot.
    Ошибка закрытия одной подсистемы не прерывает закрытие остальных.
    """
    if web_runner is not None:
        try:
            await asyncio.wait_for(web_runner.cleanup(), timeout=3.0)
        except (Exception, asyncio.CancelledError) as e:
            logging.warning("Ошибка/таймаут остановки webhook-сервера: %s", e)

    pending_tasks = [t for t in list(_background_tasks) if not t.done()]
    if pending_tasks:
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending_tasks, return_exceptions=True),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            logging.warning("Таймаут ожидания фоновых задач при остановке бота — отменяем оставшиеся задачи")
            still_running = [t for t in pending_tasks if not t.done()]
            for t in still_running:
                t.cancel()
            if still_running:
                try:
                    await asyncio.wait(still_running, timeout=1.5)
                except (Exception, asyncio.CancelledError):
                    pass
        except (Exception, asyncio.CancelledError) as e:
            logging.warning("Ошибка ожидания фоновых задач при остановке: %s", e)

    try:
        if scheduler.running:
            scheduler.shutdown(wait=False)
    except (Exception, asyncio.CancelledError) as e:
        logging.warning("Ошибка остановки планировщика: %s", e)

    try:
        await asyncio.wait_for(yclients_api.close_http_session(), timeout=3.0)
    except (Exception, asyncio.CancelledError) as e:
        logging.warning("Ошибка закрытия HTTP-сессии YCLIENTS: %s", e)

    try:
        await asyncio.wait_for(max_sender.close_max_client(), timeout=6.0)
    except (Exception, asyncio.CancelledError) as e:
        logging.warning("Ошибка закрытия клиента MAX: %s", e)
        try:
            max_sender._sync_cleanup_client(max_sender._client)
        except Exception:
            pass

    if bot is not None:
        try:
            session = getattr(bot, "session", None)
            if session is not None:
                await asyncio.wait_for(session.close(), timeout=3.0)
        except (Exception, asyncio.CancelledError) as e:
            logging.warning("Ошибка закрытия сессии Telegram-бота: %s", e)


async def main():
    global BOT_STARTED_AT
    logging.basicConfig(level=logging.INFO)
    health_monitor.install_error_handler()
    BOT_STARTED_AT = now_msk()
    health_monitor.set_bot_started_at(BOT_STARTED_AT)

    # 1. Инициализируем базу данных SQLite
    await init_db()

    bot = Bot(token=config.BOT_TOKEN)
    max_sender.register_bot(bot)
    dp = Dispatcher()

    dp.include_router(admin_router)
    dp.include_router(common_router)
    dp.include_router(booking_router)

    web_runner: web.AppRunner | None = None
    try:
        # 2. Запускаем планировщик задач и восстанавливаем таймеры из БД
        scheduler.start()
        await restore_all_reminders(bot)

        # 3. Поднимаем встроенный HTTP-сервер для вебхуков YCLIENTS
        if config.YCLIENTS_WEBHOOK_ENABLED:
            app = web.Application(client_max_size=2 * 1024 * 1024)
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
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await shutdown_resources(bot, web_runner)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Бот остановлен.")