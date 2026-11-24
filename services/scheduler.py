import logging
from datetime import datetime, date, timedelta, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from config import ADMIN_IDS
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

from database import db
from services import booking_service as bs
from services import yclients_api
from keyboards import inline as ikb

MSK_TZ = timezone(timedelta(hours=3))
scheduler = AsyncIOScheduler(timezone=MSK_TZ)


def now_msk() -> datetime:
    return datetime.now(MSK_TZ).replace(tzinfo=None)


def _has_valid_telegram_id(app: dict | None) -> bool:
    if not app:
        return False
    tid = app.get("telegram_id")
    try:
        return tid is not None and int(tid) > 0
    except (ValueError, TypeError):
        return False


async def _is_appointment_still_valid(app: dict | None, allow_completed: bool = False) -> bool:
    allowed = ("active", "completed") if allow_completed else ("active",)
    if not _has_valid_telegram_id(app) or app.get("status") not in allowed:
        return False

    yc_active = await yclients_api.is_yclients_record_active(
        app["branch_id"],
        app.get("yclients_record_id")
    )
    if not yc_active:
        await db.admin_cancel_appointment(app["id"])
        return False

    return True


async def send_24h_reminder(bot: Bot, appointment_id: int, force: bool = False):
    app = await db.get_appointment_by_id(appointment_id)
    if not force and not await _is_appointment_still_valid(app):
        return
    if not _has_valid_telegram_id(app):
        return

    time_parts = str(app["visit_time"]).split(":")
    norm_time = f"{int(time_parts[0]):02d}:{int(time_parts[1]):02d}"
    visit_dt = datetime.fromisoformat(f"{app['visit_date']}T{norm_time}:00")
    now = now_msk()
    if not force and now >= visit_dt:
        return

    formatted_date = date.fromisoformat(app["visit_date"]).strftime("%d.%m.%Y")
    branch = bs.get_branches().get(app["branch_id"], {})
    address = branch.get("address", app["branch_title"])
    is_confirmed = bool(app.get("confirmed") == 1)

    try:
        await db.render_step_to_chat(
            bot,
            chat_id=app["telegram_id"],
            step_key="rem_24h",
            reply_markup=ikb.reminder_24h_kb(appointment_id, is_confirmed=is_confirmed),
            address=address,
            service_title=app["service_title"],
            master_name=app["master_name"],
            formatted_date=formatted_date,
            visit_time=app["visit_time"],
            branch_title=app["branch_title"]
        )
    except Exception as e:
        logging.error(f"Ошибка отправки 24ч напоминания #{appointment_id}: {e}")


async def send_1h_reminder(bot: Bot, appointment_id: int, force: bool = False):
    app = await db.get_appointment_by_id(appointment_id)
    if not force and not await _is_appointment_still_valid(app):
        return
    if not _has_valid_telegram_id(app):
        return

    time_parts = str(app["visit_time"]).split(":")
    norm_time = f"{int(time_parts[0]):02d}:{int(time_parts[1]):02d}"
    visit_dt = datetime.fromisoformat(f"{app['visit_date']}T{norm_time}:00")
    now = now_msk()
    if not force and (now >= visit_dt or (visit_dt - now).total_seconds() < 900):
        logging.info(f"Визит #{appointment_id} уже начался или до него осталось менее 15 мин — 1ч напоминание пропущено.")
        return

    branch = bs.get_branches().get(app["branch_id"], {})
    address = branch.get("address", app["branch_title"])

    try:
        await db.render_step_to_chat(
            bot,
            chat_id=app["telegram_id"],
            step_key="rem_1h",
            reply_markup=ikb.branch_route_kb(app["branch_id"]),
            address=address,
            service_title=app["service_title"],
            master_name=app["master_name"],
            visit_time=app["visit_time"],
            branch_title=app["branch_title"]
        )
    except Exception as e:
        logging.error(f"Ошибка отправки 1ч напоминания #{appointment_id}: {e}")


async def send_aftercare_and_nps(bot: Bot, appointment_id: int, force: bool = False):
    app = await db.get_appointment_by_id(appointment_id)
    if not force and not await _is_appointment_still_valid(app, allow_completed=True):
        logging.info(f"Запись #{appointment_id} была отменена — запрос отзыва НЕ отправляется.")
        return
    if not _has_valid_telegram_id(app):
        return

    if not await db.has_review_for_appointment(appointment_id):
        try:
            await db.render_step_to_chat(
                bot,
                chat_id=app["telegram_id"],
                step_key="nps_request",
                reply_markup=ikb.nps_rating_kb(appointment_id),
                branch_title=app["branch_title"],
                service_title=app["service_title"],
                master_name=app["master_name"]
            )
        except Exception as e:
            logging.error(f"Ошибка отправки NPS-опроса #{appointment_id}: {e}")


async def send_repeat_reminder(bot: Bot, appointment_id: int, force: bool = False):
    app = await db.get_appointment_by_id(appointment_id)
    if not force and not await _is_appointment_still_valid(app, allow_completed=True):
        return
    if not _has_valid_telegram_id(app):
        return

    if not force:
        active_apps = await db.get_user_active_appointments(app["telegram_id"])
        if active_apps:
            logging.info(f"У клиента {app['telegram_id']} уже есть активная запись — повторное напоминание #{appointment_id} пропущено.")
            return

        last_comp = await db.get_last_completed_appointment(app["telegram_id"])
        if last_comp and last_comp.get("id") != appointment_id:
            last_dt = f"{last_comp['visit_date']} {db.normalize_time_str(last_comp['visit_time'])}"
            curr_dt = f"{app['visit_date']} {db.normalize_time_str(app['visit_time'])}"
            if last_dt > curr_dt:
                logging.info(f"У клиента {app['telegram_id']} уже был более свежий визит #{last_comp['id']} — повторное напоминание #{appointment_id} пропущено.")
                return

    days = await db.get_repeat_days(app.get("cat_id", "hair_removal"))
    try:
        await db.render_step_to_chat(
            bot,
            chat_id=app["telegram_id"],
            step_key="rem_repeat",
            reply_markup=ikb.repeat_booking_kb(has_past_visit=True),
            days=days,
            service_title=app["service_title"],
            master_name=app["master_name"],
            branch_title=app["branch_title"]
        )
    except Exception as e:
        logging.error(f"Ошибка отправки повторного напоминания #{appointment_id}: {e}")


def remove_appointment_reminders(appointment_id: int):
    for prefix in ("rem_24h_", "rem_1h_", "rem_after_", "rem_rep_"):
        job_id = f"{prefix}{appointment_id}"
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)


async def schedule_appointment_reminders(bot: Bot, appointment_id: int):
    remove_appointment_reminders(appointment_id)

    app = await db.get_appointment_by_id(appointment_id)
    if not _has_valid_telegram_id(app) or app["status"] not in ("active", "completed"):
        return

    time_parts = str(app["visit_time"]).split(":")
    norm_time = f"{int(time_parts[0]):02d}:{int(time_parts[1]):02d}"
    visit_dt = datetime.fromisoformat(f"{app['visit_date']}T{norm_time}:00")
    now = now_msk()
    duration_min = int(app.get("duration", 30) or 30)

    dt_24h = visit_dt - timedelta(hours=24)
    if dt_24h > now:
        scheduler.add_job(
            send_24h_reminder,
            trigger="date",
            run_date=dt_24h,
            args=[bot, appointment_id],
            id=f"rem_24h_{appointment_id}",
            misfire_grace_time=1800,
            replace_existing=True
        )

    dt_1h = visit_dt - timedelta(hours=1)
    if dt_1h > now:
        scheduler.add_job(
            send_1h_reminder,
            trigger="date",
            run_date=dt_1h,
            args=[bot, appointment_id],
            id=f"rem_1h_{appointment_id}",
            misfire_grace_time=1800,
            replace_existing=True
        )

    dt_after = visit_dt + timedelta(minutes=duration_min)
    if dt_after > now:
        scheduler.add_job(
            send_aftercare_and_nps,
            trigger="date",
            run_date=dt_after,
            args=[bot, appointment_id],
            id=f"rem_after_{appointment_id}",
            misfire_grace_time=1800,
            replace_existing=True
        )

    repeat_days = await db.get_repeat_days(app.get("cat_id", "hair_removal"))
    dt_repeat = visit_dt + timedelta(days=repeat_days)
    if dt_repeat > now:
        scheduler.add_job(
            send_repeat_reminder,
            trigger="date",
            run_date=dt_repeat,
            args=[bot, appointment_id],
            id=f"rem_rep_{appointment_id}",
            misfire_grace_time=3600,
            replace_existing=True
        )


async def send_weekly_digest_job(bot: Bot):
    """Автоматическая рассылка еженедельного дайджеста по понедельникам в 10:00 МСК."""
    logging.info("Формирование и рассылка еженедельного управленческого дайджеста Sahar&Vosk...")
    now = now_msk()
    days_since_monday = now.weekday() + 7
    prev_monday = (now - timedelta(days=days_since_monday)).date()
    prev_sunday = prev_monday + timedelta(days=6)

    start_date = prev_monday.isoformat()
    end_date = prev_sunday.isoformat()

    text = await db.build_management_digest_text(start_date=start_date, end_date=end_date, is_weekly=True)
    all_admins = await db.get_all_admin_ids(ADMIN_IDS)

    for admin_id in all_admins:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=text[:4096],
                parse_mode="Markdown"
            )
        except TelegramBadRequest:
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=text[:4096]
                )
            except Exception as e:
                logging.warning(f"Не удалось доставить еженедельный дайджест администратору {admin_id}: {e}")
        except Exception as e:
            logging.warning(f"Не удалось доставить еженедельный дайджест администратору {admin_id}: {e}")


def schedule_weekly_digest_cron(bot: Bot):
    scheduler.add_job(
        send_weekly_digest_job,
        trigger=CronTrigger(day_of_week="mon", hour=10, minute=0, timezone=MSK_TZ),
        args=[bot],
        id="weekly_management_digest",
        misfire_grace_time=3600,
        replace_existing=True
    )


async def restore_all_reminders(bot: Bot):
    schedule_weekly_digest_cron(bot)
    apps = await db.get_appointments_for_scheduler()
    for app in apps:
        await schedule_appointment_reminders(bot, app["id"])


restore_scheduled_jobs = restore_all_reminders