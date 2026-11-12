import logging
from datetime import datetime, date, timedelta, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram import Bot

from database import db
from services import booking_service as bs
from services import yclients_api
from keyboards import inline as ikb

MSK_TZ = timezone(timedelta(hours=3))
scheduler = AsyncIOScheduler(timezone=MSK_TZ)


def now_msk() -> datetime:
    return datetime.now(MSK_TZ).replace(tzinfo=None)


async def _is_appointment_still_valid(app: dict | None, allow_completed: bool = False) -> bool:
    allowed = ("active", "completed") if allow_completed else ("active",)
    if not app or app.get("status") not in allowed:
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
    if not app:
        return

    formatted_date = date.fromisoformat(app["visit_date"]).strftime("%d.%m.%Y")
    branch = bs.get_branches().get(app["branch_id"], {})
    address = branch.get("address", app["branch_title"])

    try:
        await db.render_step_to_chat(
            bot,
            chat_id=app["telegram_id"],
            step_key="rem_24h",
            reply_markup=ikb.reminder_24h_kb(appointment_id),
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
    if not app:
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
    if not app:
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
    if not app:
        return

    days = await db.get_repeat_days(app.get("cat_id", "hair_removal"))
    try:
        await db.render_step_to_chat(
            bot,
            chat_id=app["telegram_id"],
            step_key="rem_repeat",
            reply_markup=ikb.repeat_booking_kb(),
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
    app = await db.get_appointment_by_id(appointment_id)
    if not app or app["status"] not in ("active", "completed"):
        return

    remove_appointment_reminders(appointment_id)

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
            replace_existing=True
        )


async def restore_all_reminders(bot: Bot):
    apps = await db.get_appointments_for_scheduler()
    for app in apps:
        await schedule_appointment_reminders(bot, app["id"])


restore_scheduled_jobs = restore_all_reminders