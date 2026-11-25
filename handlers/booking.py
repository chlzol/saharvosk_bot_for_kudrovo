import logging
import re
from datetime import date
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    BufferedInputFile,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest

from states.booking import BookingState, ReviewState
from services import booking_service as bs
from services import yclients_api
from services import calendar_helper as ch
from services.scheduler import (
    schedule_appointment_reminders,
    remove_appointment_reminders,
    send_24h_reminder,
    send_1h_reminder,
    send_aftercare_and_nps,
    send_repeat_reminder
)
from handlers.admin import check_admin, notify_admins, send_to_archive_group, escape_md
from handlers.common import ensure_user_consent
from keyboards.default import get_main_keyboard, get_phone_keyboard, MAIN_MENU_BUTTONS
from keyboards import inline as ikb
from database import db

router = Router()
_pending_bookings: set[int] = set()


def _resolve_service_list_step_key(cat_id: str, subcat_id: str, sub2_id: str | None = None) -> str:
    """Определяет точный ключ экрана для конкретной подкатегории или зоны депиляции."""
    if sub2_id and sub2_id != "all":
        if cat_id == "men":
            return f"s2_men_{sub2_id}"
        return f"s2_depil_{sub2_id}"
    if subcat_id and subcat_id not in ("all", "other"):
        return f"sc_{cat_id}_{subcat_id}"
    return f"cat_{cat_id}"


def _normalize_phone(raw_phone: str) -> str | None:
    raw_str = (raw_phone or "").strip()
    digits = re.sub(r"\D", "", raw_str)
    if len(digits) == 10 and not raw_str.startswith("+"):
        return f"+7{digits}"
    if len(digits) == 11 and digits[0] in ("7", "8"):
        return f"+7{digits[1:]}"
    if raw_str.startswith("+") and 11 <= len(digits) <= 15 and digits[0] not in ("7", "8"):
        return f"+{digits}"
    return None


def _is_valid_human_name(name: str | None) -> bool:
    if not name:
        return False
    clean = name.strip()
    if not re.match(r"^[А-Яа-яЁёA-Za-z\-\s]{2,35}$", clean):
        return False
    return sum(1 for ch in clean if ch.isalpha()) >= 2


def _get_name_prompt_kb(suggested_name: str | None) -> ReplyKeyboardMarkup:
    keyboard = []
    if _is_valid_human_name(suggested_name):
        keyboard.append([KeyboardButton(text=suggested_name.strip())])
    keyboard.append([KeyboardButton(text="❌ Отменить запись")])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, one_time_keyboard=True)


async def _get_main_kb_for(user_id: int | None) -> ReplyKeyboardMarkup:
    """Возвращает главную reply-клавиатуру с учётом завершённых визитов пользователя."""
    has_past = (
        await db.has_completed_appointments(user_id)
        if isinstance(user_id, int) and not isinstance(user_id, bool)
        else False
    )
    return get_main_keyboard(has_past_visit=has_past)


_confirming_appointments: set[int] = set()
_active_cart_ops: set[int] = set()
_active_repeat_ops: set[int] = set()
_pending_time_selection: set[int] = set()


def _build_booking_summary(selected_ids: list[str], data: dict | None = None) -> dict:
    """Формирует сводку услуг корзины с сохранением параметров внешней записи при переносе."""
    data = data or {}
    summary = bs.get_cart_summary(selected_ids)
    fb_title = data.get("fallback_service_title")

    if fb_title and (summary["count"] == 0 or summary["count"] < len(selected_ids)):
        fb_price = int(data.get("fallback_price") or 0)
        fb_dur = int(data.get("fallback_duration") or 30)
        summary["titles_inline"] = fb_title
        price_str = f" — {fb_price} ₽" if fb_price > 0 else ""
        summary["bullet_lines"] = f"1. **{fb_title}**{price_str} ({fb_dur} мин.)"
        summary["bullet_lines_md"] = f"1. **{escape_md(fb_title)}**{price_str} ({fb_dur} мин.)"
        summary["total_price_num"] = fb_price
        summary["total_price"] = f"{fb_price} ₽" if fb_price > 0 else "по прайсу"
        summary["total_duration"] = fb_dur
        summary["count"] = max(1, len(selected_ids))
    else:
        services = summary.get("services") or []
        if services:
            summary["bullet_lines_md"] = "\n".join(
                f"{idx}. **{escape_md(s['title'])}** — {s['price']} ₽ ({s['duration']} мин.)"
                for idx, s in enumerate(services, start=1)
            )
        else:
            summary["bullet_lines_md"] = summary["bullet_lines"]
    return summary


def _resolve_master_name(master_id: str | None, data: dict | None = None) -> str:
    """Возвращает имя специалиста с поддержкой кэша API и внешней записи."""
    mid = str(master_id or "any")
    data = data or {}
    names_map = data.get("master_names_map") or {}
    if mid in names_map and names_map[mid]:
        return names_map[mid]
    name = bs.get_master_name(mid)
    if name == "Специалист" and data.get("fallback_master_name") and mid == str(data.get("master_id", "")):
        return data["fallback_master_name"]
    return name


async def _safe_edit_message(call: CallbackQuery, text: str, reply_markup=None):
    msg = getattr(call, "message", call)
    if getattr(msg, "photo", None):
        try:
            await msg.delete()
        except Exception:
            pass
        try:
            await msg.answer(text, reply_markup=reply_markup, parse_mode="Markdown", disable_web_page_preview=True)
        except TelegramBadRequest:
            await msg.answer(text, reply_markup=reply_markup, disable_web_page_preview=True)
        return

    try:
        await msg.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown", disable_web_page_preview=True)
    except TelegramBadRequest as e:
        err_lower = str(e).lower()
        if "message is not modified" in err_lower:
            return
        if "message can't be edited" in err_lower or "message to edit not found" in err_lower:
            try:
                await msg.answer(text, reply_markup=reply_markup, parse_mode="Markdown", disable_web_page_preview=True)
                return
            except TelegramBadRequest:
                try:
                    await msg.answer(text, reply_markup=reply_markup, disable_web_page_preview=True)
                    return
                except Exception:
                    pass
        try:
            await msg.edit_text(text, reply_markup=reply_markup, disable_web_page_preview=True)
        except Exception:
            pass


async def _require_state(call: CallbackQuery, state: FSMContext, *required_keys: str) -> dict | None:
    data = await state.get_data()
    for k in required_keys:
        if not data.get(k):
            await state.clear()
            await state.set_state(BookingState.choosing_branch)
            has_past = await db.has_completed_appointments(call.from_user.id)
            await call.answer("🔄 Сессия обновилась. Пожалуйста, выберите студию заново.", show_alert=True)
            await db.render_step(
                call,
                "step_1_branch",
                reply_markup=ikb.branches_kb(bs.get_branches(), has_past_visit=has_past)
            )
            return None
    return data


async def _resolve_masters_for_services(branch_id: str, service_ids: list[str]) -> list[dict]:
    local_masters = bs.get_masters(branch_id, service_ids)
    api_masters = await yclients_api.fetch_masters_for_services(branch_id, service_ids)
    if api_masters is not None:
        if not api_masters:
            return []
        if local_masters:
            local_ids = {str(m["id"]) for m in local_masters}
            intersected = [m for m in api_masters if str(m["id"]) in local_ids]
            if intersected:
                return intersected
        return api_masters
    return local_masters


@router.message(Command("test_remind"))
async def cmd_test_remind(message: Message):
    if not await check_admin(message.from_user.id):
        await message.answer("⛔️ Команда доступна только администраторам студии **Sahar&Vosk**.", parse_mode="Markdown")
        return

    appointments = await db.get_user_active_appointments(message.from_user.id)
    if not appointments:
        await message.answer("⚠️ У вас нет активных записей. Сначала создайте любую запись, а затем вызовите `/test_remind`.", parse_mode="Markdown")
        return

    last_app_id = appointments[-1]["id"]
    await message.answer(f"🧪 Отправляю демонстрацию всех уведомлений Sahar&Vosk для записи №{last_app_id}:")
    await send_24h_reminder(message.bot, last_app_id, force=True)
    await send_1h_reminder(message.bot, last_app_id, force=True)
    await send_aftercare_and_nps(message.bot, last_app_id, force=True)
    await send_repeat_reminder(message.bot, last_app_id, force=True)


# =====================================================================
# ДОБАВЛЕНИЕ В КАЛЕНДАРЬ СМАРТФОНА (ОТПРАВКА .ICS ФАЙЛА)
# =====================================================================

@router.callback_query(F.data.startswith("cal_ics:"))
async def send_calendar_ics_file(call: CallbackQuery):
    try:
        app_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer("Запись не найдена.", show_alert=True)
        return
    app = await db.get_appointment_by_id(app_id)
    if not app or app["telegram_id"] != call.from_user.id:
        await call.answer("Запись не найдена.", show_alert=True)
        return

    branch = bs.get_branches().get(app["branch_id"], {})
    address = branch.get("address", app["branch_title"])
    duration = int(app.get("duration", 30) or 30)

    title = f"Sahar&Vosk — {app['service_title']}"
    description = (
        f"Запись №{app['id']} в студию Sahar&Vosk\n"
        f"Услуги: {app['service_title']}\n"
        f"Специалист: {app['master_name']}\n"
        f"Студия: Sahar&Vosk · {app['branch_title']}\n"
        f"Адрес: {address}\n"
        f"Телефон: +7 (812) 988-98-53"
    )

    ics_bytes = ch.generate_ics_content(
        app_id=app["id"],
        title=title,
        visit_date=app["visit_date"],
        visit_time=app["visit_time"],
        duration_min=duration,
        location=address,
        description=description
    )

    doc = BufferedInputFile(ics_bytes, filename=f"saharvosk_visit_{app_id}.ics")
    await call.message.answer_document(
        document=doc,
        caption=(
            f"📅 **Файл календаря · Sahar&Vosk (Запись №{app_id})**\n"
            f"────────────────────\n"
            "Нажмите на файл выше, чтобы добавить визит в Apple Calendar, Samsung Calendar или Outlook "
            "(включая автоматическое напоминание за 2 часа до визита)."
        ),
        parse_mode="Markdown"
    )
    await call.answer()


# =====================================================================
# УМНЫЙ СБОР ОТЗЫВОВ ПОСЛЕ ВИЗИТА (NPS-КОНТРОЛЬ 1-5 ЗВЁЗД)
# =====================================================================

@router.callback_query(F.data.startswith("nps:"))
async def handle_nps_rating(call: CallbackQuery, state: FSMContext):
    try:
        _, app_id_str, rating_str = call.data.split(":")
        app_id = int(app_id_str)
        rating = int(rating_str)
    except (ValueError, IndexError):
        await call.answer("Некорректные данные оценки.", show_alert=True)
        return

    if await db.has_review_for_appointment(app_id):
        await call.answer("Вы уже оставили оценку по этому визиту. Благодарим вас! ✨", show_alert=True)
        return

    app = await db.get_appointment_by_id(app_id)
    if not app or app["telegram_id"] != call.from_user.id:
        await call.answer("Запись не найдена.", show_alert=True)
        return

    branch_id = app["branch_id"]
    branch = bs.get_branches().get(branch_id, {})
    branch_title = branch.get("title", app["branch_title"])

    review_id = await db.save_review(
        appointment_id=app_id,
        telegram_id=call.from_user.id,
        branch_id=branch_id,
        master_name=app["master_name"],
        service_title=app["service_title"],
        rating=rating
    )

    stars_str = "⭐️" * rating

    if rating >= 4:
        await db.render_step(
            call,
            "nps_high",
            reply_markup=ikb.yandex_review_kb(branch_id),
            stars=stars_str,
            master_name=escape_md(app["master_name"]),
            branch_title=escape_md(branch_title),
            service_title=escape_md(app["service_title"])
        )
        await notify_admins(
            call.bot,
            f"🌟 **Высокая оценка визита · Sahar&Vosk: {stars_str} ({rating}/5)**\n"
            f"📍 Студия: {escape_md(branch_title)}\n"
            f"👤 Клиент: {escape_md(app['client_name'])} (`{app['phone']}`)\n"
            f"✨ Услуга: {escape_md(app['service_title'])}\n"
            f"👩‍⚕️ Специалист: {escape_md(app['master_name'])}"
        )
        await call.answer("Благодарим за вашу оценку! ✨")
        return

    await state.set_state(ReviewState.entering_feedback)
    await state.update_data(
        review_id=review_id,
        review_app_id=app_id,
        review_rating=rating
    )
    await db.render_step(
        call,
        "nps_low",
        stars=stars_str,
        master_name=escape_md(app["master_name"]),
        branch_title=escape_md(branch_title),
        service_title=escape_md(app["service_title"])
    )
    await call.answer()


@router.message(
    ReviewState.entering_feedback,
    F.photo | (F.text & ~F.text.in_(MAIN_MENU_BUTTONS) & ~F.text.startswith("/"))
)
async def process_negative_nps_feedback(message: Message, state: FSMContext):
    data = await state.get_data()
    review_id = data.get("review_id")
    app_id = data.get("review_app_id")
    rating = data.get("review_rating", 1)
    feedback_text = (message.text or message.caption or "").strip()

    if not feedback_text:
        await message.answer("Пожалуйста, опишите текстом ваши пожелания или замечания.")
        return

    if review_id:
        await db.update_review_feedback(review_id, feedback_text)

    app = await db.get_appointment_by_id(app_id) if app_id else None
    await state.clear()

    if app:
        branch_id = app["branch_id"]
        branch_title = app["branch_title"]
        stars_str = "⭐️" * rating
        alert_text = (
            f"🚨 **Контроль качества · Sahar&Vosk: {stars_str} ({rating}/5)**\n"
            f"────────────────────\n"
            f"📍 **Студия:** {escape_md(branch_title)}\n"
            f"👤 **Клиент:** {escape_md(app['client_name'])} (`{app['phone']}`)\n"
            f"✨ **Услуга:** {escape_md(app['service_title'])} (Запись №{app_id})\n"
            f"👩‍⚕️ **Специалист:** {escape_md(app['master_name'])}\n\n"
            f"💬 **Комментарий клиента:**\n{escape_md(feedback_text)}"
        )
        await notify_admins(
            message.bot,
            alert_text,
            reply_markup=ikb.admin_quick_reply_kb(message.from_user.id)
        )
        await db.save_support_message(
            telegram_id=message.from_user.id,
            client_name=app["client_name"],
            username=message.from_user.username,
            branch_id=branch_id,
            sender="client",
            sender_name=app["client_name"],
            text=f"[ОТЗЫВ {rating}/5 ⭐️]: {feedback_text}"
        )
        await send_to_archive_group(
            bot=message.bot,
            branch_id=branch_id,
            direction="from_client",
            client_name=app["client_name"],
            client_id=message.from_user.id,
            username=message.from_user.username,
            text=(
                f"🚨 [КОНТРОЛЬ КАЧЕСТВА] Оценка {rating}/5 ⭐️ · "
                f"Запись №{app_id} ({app['service_title']}, мастер: {app['master_name']}):\n"
                f"{feedback_text}"
            ),
            photo_id=message.photo[-1].file_id if message.photo else None
        )

    await db.render_step(
        message,
        "nps_feedback_saved",
        reply_markup=await _get_main_kb_for(message.from_user.id)
    )


@router.message(ReviewState.entering_feedback, ~F.photo & ~F.text)
async def process_negative_nps_unsupported(message: Message):
    await message.answer("Пожалуйста, опишите ваши пожелания текстом или прикрепите фото с комментарием.")


# =====================================================================
# РАЗДЕЛ «УСЛУГИ И ПРАЙС» (С ПОДДЕРЖКОЙ РЕДАКТОРА ШАГОВ И ФОТО)
# =====================================================================

@router.message(F.text == "💰 Услуги и прайс")
async def show_price_branches(message: Message, state: FSMContext):
    if not await ensure_user_consent(message):
        return
    await state.clear()
    branches = bs.get_branches()
    await db.render_step(
        message,
        "price_1_branch",
        reply_markup=ikb.price_branches_kb(branches)
    )


@router.callback_query(F.data == "price_back:branches")
async def back_to_price_branches(call: CallbackQuery):
    branches = bs.get_branches()
    await db.render_step(
        call,
        "price_1_branch",
        reply_markup=ikb.price_branches_kb(branches)
    )
    await call.answer()


@router.callback_query(F.data.startswith("price_br:"))
async def show_price_categories(call: CallbackQuery):
    branch_id = call.data.split(":")[1]
    branch = bs.get_branches().get(branch_id, {})
    categories = bs.get_categories_by_branch(branch_id)

    await db.render_step(
        call,
        "price_2_category",
        reply_markup=ikb.price_categories_kb(branch_id, categories, page=1),
        branch_title=branch.get("title", "Студия")
    )
    await call.answer()


@router.callback_query(F.data.startswith("price_cat_pg:"))
async def paginate_price_categories(call: CallbackQuery):
    try:
        _, branch_id, page_str = call.data.split(":")
        page = int(page_str)
    except (ValueError, IndexError):
        await call.answer("Некорректная страница.", show_alert=True)
        return
    branch = bs.get_branches().get(branch_id, {})
    categories = bs.get_categories_by_branch(branch_id)

    await db.render_step(
        call,
        "price_2_category",
        reply_markup=ikb.price_categories_kb(branch_id, categories, page=page),
        branch_title=branch.get("title", "Студия")
    )
    await call.answer()


@router.callback_query(F.data.startswith("pr_cat:"))
async def show_price_subcategories(call: CallbackQuery):
    parts = call.data.split(":")
    if len(parts) < 3:
        await call.answer("Некорректная категория.", show_alert=True)
        return
    _, branch_id, main_cat_id = parts[:3]
    branch = bs.get_branches().get(branch_id, {})
    cat_title = bs.get_main_category_title(main_cat_id)
    subcategories = bs.get_subcategories_by_category(branch_id, main_cat_id)

    await db.render_step(
        call,
        "price_3_subcategory",
        fallback_key=f"cat_{main_cat_id}",
        reply_markup=ikb.price_subcategories_kb(branch_id, main_cat_id, subcategories),
        branch_title=escape_md(branch.get("title", "Студия")),
        cat_title=escape_md(cat_title)
    )
    await call.answer()


@router.callback_query(F.data.startswith("pr_sub:") | F.data.startswith("ps:"))
async def show_category_services_price(call: CallbackQuery):
    parts = call.data.split(":")
    if len(parts) < 5:
        await call.answer("Некорректные параметры прайс-листа.", show_alert=True)
        return
    _, raw_branch, main_cat_id, subcat_id, page_str = parts[:5]
    branch_id = {"pr": "prazhskaya", "ln": "leningradskaya"}.get(raw_branch, raw_branch)
    sb = ikb._short_branch(branch_id)
    sub2_id = parts[5] if len(parts) > 5 else None
    try:
        page = int(page_str)
    except ValueError:
        await call.answer("Некорректная страница.", show_alert=True)
        return

    if not sub2_id and not bs.is_subcategory_direct(main_cat_id, subcat_id):
        sub2_options = bs.get_sub2_options(branch_id, main_cat_id, subcat_id)
        if sub2_options:
            branch_title = bs.get_branches().get(branch_id, {}).get("title", "")
            subcat_title = bs.get_subcategory_title(main_cat_id, subcat_id)
            buttons = [
                [InlineKeyboardButton(text=title, callback_data=f"ps:{sb}:{main_cat_id}:{subcat_id}:1:{s2_id}")]
                for s2_id, title in sub2_options.items()
            ]
            buttons.append([InlineKeyboardButton(text="‹ Назад к категориям", callback_data=f"pr_cat:{branch_id}:{main_cat_id}")])
            await db.render_step(
                call,
                f"sc_{main_cat_id}_{subcat_id}",
                fallback_key="price_3_subcategory",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
                branch_title=escape_md(branch_title),
                cat_title=escape_md(subcat_title),
                subcat_title=escape_md(subcat_title)
            )
            await call.answer()
            return

    page_size = 12
    services = bs.get_services_by_subcategory(branch_id, main_cat_id, subcat_id, sub2_id=sub2_id)
    cat_title = bs.get_main_category_title(main_cat_id)
    subcat_title = bs.get_subcategory_title(main_cat_id, subcat_id, sub2_id=sub2_id)
    branch_title = bs.get_branches().get(branch_id, {}).get("title", "")

    total_pages = max(1, (len(services) + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    current_services = services[start:start + page_size]

    srv_lines = []
    for s in current_services:
        price_str = f"**{s['price']} ₽**" if s['price'] > 0 else "по прайсу"
        srv_lines.append(f"· {escape_md(s['title'])} *({s['duration']} мин.)* — {price_str}")
    services_list_text = "\n".join(srv_lines) if srv_lines else "· Услуги в данной категории уточняются"

    if sub2_id:
        buttons = []
        if total_pages > 1:
            nav = []
            if page > 1:
                nav.append(InlineKeyboardButton(text="‹", callback_data=f"ps:{sb}:{main_cat_id}:{subcat_id}:{page - 1}:{sub2_id}"))
            nav.append(InlineKeyboardButton(text=f"{page} / {total_pages}", callback_data="ignore"))
            if page < total_pages:
                nav.append(InlineKeyboardButton(text="›", callback_data=f"ps:{sb}:{main_cat_id}:{subcat_id}:{page + 1}:{sub2_id}"))
            buttons.append(nav)
        buttons.append([InlineKeyboardButton(text="✨ Записаться онлайн", callback_data="start_new_booking")])
        buttons.append([InlineKeyboardButton(text="‹ Назад к разделам", callback_data=f"ps:{sb}:{main_cat_id}:{subcat_id}:1")])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    else:
        kb = ikb.price_services_view_kb(branch_id, main_cat_id, subcat_id, page, total_pages)

    specific_key = _resolve_service_list_step_key(main_cat_id, subcat_id, sub2_id)
    await db.render_step(
        call,
        "price_4_services",
        fallback_key=specific_key,
        reply_markup=kb,
        branch_title=escape_md(branch_title),
        cat_title=escape_md(cat_title),
        subcat_title=escape_md(subcat_title),
        page=page,
        total_pages=total_pages,
        services_list=services_list_text
    )
    await call.answer()


# =====================================================================
# РАЗДЕЛ «МОИ ЗАПИСИ» (С ПОДДЕРЖКОЙ РЕДАКТОРА ШАГОВ И ФОТО)
# =====================================================================

@router.message(F.text == "📋 Мои записи")
async def show_my_appointments(message: Message, state: FSMContext):
    if not await ensure_user_consent(message):
        return
    await state.clear()
    appointments = await db.get_user_active_appointments(message.from_user.id)
    if not appointments:
        has_past = await db.has_completed_appointments(message.from_user.id)
        await db.render_step(
            message,
            "my_apps_empty",
            reply_markup=ikb.repeat_booking_kb(has_past_visit=has_past)
        )
        return

    for app in appointments:
        formatted_date = date.fromisoformat(app["visit_date"]).strftime("%d.%m.%Y")
        status_badge = "✓ Подтверждена" if app.get("confirmed") else "⏳ Ожидает визита"

        branch = bs.get_branches().get(app["branch_id"], {})
        address = branch.get("address", app["branch_title"])
        duration = int(app.get("duration") or 30)
        price_val = int(app.get("price") or 0)
        price_display = f"{price_val} ₽" if price_val > 0 else "по прайсу"

        google_url = ch.generate_google_calendar_url(
            title=f"Sahar&Vosk — {app['service_title']}",
            visit_date=app["visit_date"],
            visit_time=app["visit_time"],
            duration_min=duration,
            location=address,
            description=f"Специалист: {app['master_name']}\nСтудия: Sahar&Vosk · {app['branch_title']}\nАдрес: {address}\nТелефон: +7 (812) 988-98-53"
        )

        kb = ikb.appointment_manage_kb(
            app["id"],
            branch_id=app["branch_id"],
            google_cal_url=google_url
        )
        await db.render_step(
            message,
            "my_apps_card",
            reply_markup=kb,
            appointment_id=app["id"],
            status_badge=status_badge,
            branch_title=escape_md(app["branch_title"]),
            service_title=escape_md(app["service_title"]),
            master_name=escape_md(app["master_name"]),
            formatted_date=formatted_date,
            visit_time=app["visit_time"],
            price=price_display
        )


@router.callback_query(F.data.startswith("confirm_visit:"))
async def confirm_user_visit(call: CallbackQuery):
    try:
        app_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer("Запись не найдена.", show_alert=True)
        return
    if app_id in _confirming_appointments:
        await call.answer("Запрос обрабатывается...", show_alert=False)
        return
    _confirming_appointments.add(app_id)
    try:
        app = await db.get_appointment_by_id(app_id)
        if not app:
            await call.answer("Запись не найдена или уже была отменена.", show_alert=True)
            return
        if app["telegram_id"] != call.from_user.id:
            await call.answer("⛔️ Вы не можете подтвердить чужую запись.", show_alert=True)
            return

        now_str = db.now_msk().strftime("%Y-%m-%d %H:%M")
        norm_vt = db.normalize_time_str(app.get("visit_time")) or str(app.get("visit_time") or "")
        app_dt_str = f"{app['visit_date']} {norm_vt}"
        if app_dt_str < now_str or app.get("status") != "active":
            await call.answer("Запись уже неактуальна или время визита уже прошло.", show_alert=True)
            return

        if app.get("confirmed") == 1:
            await call.answer("Визит уже был подтверждён ранее ✨")
            return

        # Синхронизация с YCLIENTS CRM
        yc_status, yc_err = await yclients_api.confirm_yclients_record(
            branch_id=app["branch_id"],
            yclients_record_id=app.get("yclients_record_id"),
            expected_date=app["visit_date"],
            expected_time=app["visit_time"]
        )

        if yc_status in ("cancelled", "rescheduled"):
            if yc_status == "rescheduled":
                await db.admin_reschedule_appointment(app_id)
            else:
                await db.admin_cancel_appointment(app_id)
            remove_appointment_reminders(app_id)
            reason_text = "была перенесена" if yc_status == "rescheduled" else "была отменена"
            await db.render_step(
                call,
                "confirm_record_outdated",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💅 Записаться онлайн", callback_data="start_new_booking")]
                ]),
                appointment_id=app_id,
                reason_text=reason_text,
                branch_title=app.get("branch_title", "")
            )
            await call.answer("Запись уже неактуальна.", show_alert=True)
            return

        success = await db.confirm_appointment_visit(app_id, call.from_user.id)
        if not success:
            await call.answer("Запись не найдена или уже была отменена.", show_alert=True)
            return

        # Для гостя подтверждение всегда проходит успешно и без технических ошибок
        await _safe_edit_message(
            call,
            f"✨ **Ваш визит №{app_id} подтверждён!**\n"
            "────────────────────\n"
            "С нетерпением ждём вас в студии **Sahar&Vosk** 🤍"
        )
        await call.answer("Визит подтверждён! ✨")

        # Формируем отчёт для администраторов в зависимости от ответа YCLIENTS
        if yc_status == "api_error":
            crm_note = (
                "\n\n⚠️ **Внимание администраторам:** сбой связи с сервером YCLIENTS.\n"
                "Пожалуйста, проверьте и проставьте статус «Подтверждён» в журнале CRM вручную!"
            )
        else:
            crm_note = "\n✅ Статус в CRM-журнале YCLIENTS: подтверждён (автоматически)"

        await notify_admins(
            call.bot,
            f"✓ **Клиент подтвердил визит · Sahar&Vosk**\n"
            f"Запись №{app_id} — {escape_md(app['client_name'])} (`{app['phone']}`)\n"
            f"📍 {escape_md(app['branch_title'])} · {app['visit_date']} в {app['visit_time']}"
            f"{crm_note}"
        )
    finally:
        _confirming_appointments.discard(app_id)


@router.callback_query(F.data.startswith("cancel_app:"))
async def cancel_user_appointment(call: CallbackQuery):
    try:
        app_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer("Запись не найдена.", show_alert=True)
        return
    app = await db.get_appointment_by_id(app_id)
    if not app:
        await call.answer("Запись уже была отменена или не найдена.", show_alert=True)
        return
    if app["telegram_id"] != call.from_user.id:
        await call.answer("⛔️ Вы не можете отменить чужую запись.", show_alert=True)
        return

    success = await db.cancel_appointment(app_id, call.from_user.id)
    if success:
        remove_appointment_reminders(app_id)
        try:
            yc_deleted = await yclients_api.delete_yclients_record(app["branch_id"], app.get("yclients_record_id"))
        except Exception:
            yc_deleted = False
        formatted_date = date.fromisoformat(app["visit_date"]).strftime("%d.%m.%Y")
        crm_warn = "" if yc_deleted else "\n\n⚠️ **ВНИМАНИЕ:** Не удалось отменить запись в YCLIENTS CRM — отмените её вручную!"
        await notify_admins(
            call.bot,
            f"✕ **Отмена записи клиентом (№{app_id}) · Sahar&Vosk**\n"
            f"────────────────────\n"
            f"📍 Студия: {escape_md(app['branch_title'])}\n"
            f"👤 Клиент: {escape_md(app['client_name'])} (`{app['phone']}`)\n"
            f"✨ Услуги: {escape_md(app['service_title'])}\n"
            f"👩‍⚕️ Специалист: {escape_md(app['master_name'])}\n"
            f"🗓 Дата и время: {formatted_date} в {app['visit_time']}{crm_warn}"
        )
        await _safe_edit_message(
            call,
            f"✕ **Запись №{app_id} отменена.**\n"
            "Будем рады видеть вас в **Sahar&Vosk** в другой удобный день!"
        )
        await call.answer("Запись отменена")
    else:
        await call.answer("Запись уже была отменена или не найдена.", show_alert=True)



async def process_repeat_last_visit(target: Message | CallbackQuery, state: FSMContext, user_id: int):
    """Сценарий записи в 2 клика ('Повторить прошлый визит')."""
    if user_id in _active_repeat_ops:
        if isinstance(target, CallbackQuery):
            await target.answer("Загружаем данные...", show_alert=False)
        return
    _active_repeat_ops.add(user_id)
    try:
        last_app = await db.get_last_completed_appointment(user_id)
        if not last_app:
            err_msg = "У вас пока нет завершённых визитов для повтора. Выберите удобную студию для новой записи:"
            kb = ikb.branches_kb(bs.get_branches())
            if isinstance(target, CallbackQuery) or (hasattr(target, "message") and hasattr(target, "data")):
                await target.answer()
                await _safe_edit_message(target, err_msg, reply_markup=kb)
            else:
                await target.answer(err_msg, reply_markup=kb)
            return

        branch_id = last_app["branch_id"]
        branch = bs.get_branches().get(branch_id, {})
        branch_title = branch.get("title", last_app.get("branch_title", branch_id))
        raw_services = str(last_app.get("service_id", "")).split(",")
        service_ids = [s.strip() for s in raw_services if s.strip()]
        master_id = str(last_app.get("master_id", "any"))
        master_name = last_app.get("master_name", "")
        phone = last_app.get("phone")
        client_name = last_app.get("client_name")

        await state.clear()

        # 1. Проверяем, доступны ли все услуги из прошлого визита в каталоге студии
        valid_services = []
        for sid in service_ids:
            srv = bs.get_service_by_id(sid)
            if srv and bs._service_in_branch(srv, branch_id):
                valid_services.append(srv)

        if len(valid_services) != len(service_ids) or not valid_services:
            await state.set_state(BookingState.choosing_category)
            await state.update_data(branch_id=branch_id, cat_page=1, selected_services=[], is_repeat=True)
            categories = bs.get_categories_by_branch(branch_id)
            kb = ikb.categories_kb(categories, page=1, selected_count=0)
            if isinstance(target, CallbackQuery) or (hasattr(target, "message") and hasattr(target, "data")):
                await target.answer()
            await db.render_step(
                target,
                "repeat_services_updated",
                reply_markup=kb,
                branch_title=escape_md(branch_title)
            )
            return

        selected_ids = [str(s["id"]) for s in valid_services]
        summary = bs.get_cart_summary(selected_ids)

        # 2. Проверяем мастера
        is_master_specific = (
            master_id != "any"
            and master_name
            and not any(w in master_name.lower() for w in ("любой", "свободный", "специалист"))
        )

        available_masters = await _resolve_masters_for_services(branch_id, selected_ids)
        if not available_masters:
            await state.set_state(BookingState.choosing_category)
            await state.update_data(branch_id=branch_id, cat_page=1, selected_services=[], is_repeat=True)
            categories = bs.get_categories_by_branch(branch_id)
            kb = ikb.categories_kb(categories, page=1, selected_count=0)
            if isinstance(target, CallbackQuery) or (hasattr(target, "message") and hasattr(target, "data")):
                await target.answer()
            await db.render_step(
                target,
                "repeat_services_updated",
                reply_markup=kb,
                branch_title=escape_md(branch_title)
            )
            return

        master_in_staff = any(str(m["id"]) == str(master_id) for m in available_masters) if is_master_specific else False
        main_cat_id = next(iter(bs._match_service_to_main_categories(valid_services[0])), "other")

        # 3. Проверяем свободные окна на ближайшие 2 месяца
        has_open_slots = False
        api_dates = None
        if is_master_specific and master_in_staff:
            api_dates = await yclients_api.fetch_available_dates(branch_id, master_id, selected_ids)
            if api_dates is not None:
                has_open_slots = len(api_dates) > 0
            else:
                today = db.now_msk().date()
                cur_m = bs.get_available_dates_in_month(today.year, today.month)
                next_m_val = (today.month % 12) + 1
                next_y_val = today.year + (1 if today.month == 12 else 0)
                next_m = bs.get_available_dates_in_month(next_y_val, next_m_val)
                has_open_slots = (len(cur_m) + len(next_m)) > 0

        if not is_master_specific or not master_in_staff or not has_open_slots:
            await state.set_state(BookingState.choosing_master)
            await state.update_data(
                branch_id=branch_id,
                cat_id=main_cat_id,
                selected_services=selected_ids,
                service_id=selected_ids[0],
                master_names_map={str(m["id"]): m["name"] for m in available_masters},
                client_name=client_name,
                phone=phone,
                is_repeat=True,
                mst_page=1
            )
            if is_master_specific and not master_in_staff:
                reason = f"Специалист **{escape_md(master_name)}** больше не выполняет данный набор услуг в этой студии."
            elif is_master_specific and not has_open_slots:
                reason = f"У специалиста **{escape_md(master_name)}** сейчас нет свободных окон для записи на ближайшие 2 месяца."
            else:
                reason = "Для повтора визита выберите удобного специалиста:"

            kb = ikb.masters_kb(available_masters, page=1)
            if isinstance(target, CallbackQuery) or (hasattr(target, "message") and hasattr(target, "data")):
                await target.answer()
            await db.render_step(
                target,
                "repeat_master_unavailable",
                reply_markup=kb,
                branch_title=escape_md(branch_title),
                reason=reason,
                master_name=escape_md(master_name),
                services_list=escape_md(summary["titles_inline"]),
                titles_inline=escape_md(summary["titles_inline"]),
                total_price=summary["total_price"],
                total_duration=summary["total_duration"]
            )
            return

        # 4. Штатный сценарий повтора: открываем календарь
        await state.set_state(BookingState.choosing_date)
        await state.update_data(
            branch_id=branch_id,
            cat_id=main_cat_id,
            selected_services=selected_ids,
            service_id=selected_ids[0],
            master_id=master_id,
            master_name=master_name,
            master_names_map={str(m["id"]): m["name"] for m in available_masters},
            client_name=client_name,
            phone=phone,
            api_available_dates=api_dates,
            is_repeat=True
        )

        today = db.now_msk().date()
        cal_kb = ikb.calendar_kb(today.year, today.month, api_dates=api_dates)

        if isinstance(target, CallbackQuery) or (hasattr(target, "message") and hasattr(target, "data")):
            await target.answer()
        await db.render_step(
            target,
            "repeat_visit_summary",
            reply_markup=cal_kb,
            branch_title=escape_md(branch_title),
            services_list=escape_md(summary["titles_inline"]),
            titles_inline=escape_md(summary["titles_inline"]),
            master_name=escape_md(master_name),
            total_duration=summary["total_duration"],
            total_price=summary["total_price"]
        )
    finally:
        _active_repeat_ops.discard(user_id)


@router.message(F.text == "🔄 Повторить прошлый визит")
async def msg_repeat_last_visit(message: Message, state: FSMContext):
    if not await ensure_user_consent(message):
        return
    await process_repeat_last_visit(message, state, message.from_user.id)


@router.callback_query(F.data == "repeat_last_visit")
async def cb_repeat_last_visit(call: CallbackQuery, state: FSMContext):
    await process_repeat_last_visit(call, state, call.from_user.id)


@router.callback_query(F.data.startswith("reschedule_app:"))
async def reschedule_user_appointment(call: CallbackQuery, state: FSMContext):
    try:
        app_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer("Запись не найдена.", show_alert=True)
        return
    app = await db.get_appointment_by_id(app_id)

    if not app or app["telegram_id"] != call.from_user.id or app["status"] != "active":
        await call.answer("Запись не найдена или уже отменена.", show_alert=True)
        return

    raw_srv_list = [s.strip() for s in str(app.get("service_id") or "").split(",") if s.strip()]
    yc_rec_id = str(app.get("yclients_record_id") or "")
    # Если вебхук сохранил record_id как заглушку service_id при пустых услугах — не передаем его как service_id в API
    if yc_rec_id and len(raw_srv_list) == 1 and raw_srv_list[0] == yc_rec_id and not bs.get_service_by_id(raw_srv_list[0]):
        srv_list = []
    else:
        srv_list = raw_srv_list

    master_id = str(app.get("master_id") or "any")
    api_dates = await yclients_api.fetch_available_dates(app["branch_id"], master_id, srv_list)

    await state.clear()
    await state.update_data(
        reschedule_app_id=app_id,
        branch_id=app["branch_id"],
        cat_id=app.get("cat_id") or "other",
        service_id=srv_list[0] if srv_list else "",
        selected_services=srv_list,
        master_id=master_id,
        phone=app["phone"],
        client_name=app["client_name"],
        api_available_dates=api_dates,
        fallback_service_title=app.get("service_title") or "Услуга",
        fallback_price=int(app.get("price") or 0),
        fallback_duration=int(app.get("duration") or 30),
        fallback_master_name=app.get("master_name") or "Специалист",
    )
    await state.set_state(BookingState.choosing_date)

    today = db.now_msk().date()
    await db.render_step(
        call,
        "step_5_date",
        reply_markup=ikb.calendar_kb(today.year, today.month, api_dates=api_dates),
        master_name=escape_md(app.get("master_name") or "Специалист")
    )
    await call.answer("Выберите новую дату визита (текущая запись сохранится до подтверждения)")


# =====================================================================
# ПОШАГОВАЯ ОНЛАЙН-ЗАПИСЬ (С ИНДИВИДУАЛЬНЫМИ ЭКРАНАМИ КАТЕГОРИЙ И ПОДКАТЕГОРИЙ)
# =====================================================================

@router.message(F.text == "💅 Записаться онлайн")
async def start_booking(message: Message, state: FSMContext):
    if not await ensure_user_consent(message):
        return
    await state.clear()
    has_past = await db.has_completed_appointments(message.from_user.id)
    await state.set_state(BookingState.choosing_branch)
    await db.render_step(
        message,
        "step_1_branch",
        reply_markup=ikb.branches_kb(bs.get_branches(), has_past_visit=has_past)
    )


@router.callback_query(F.data == "start_new_booking")
async def start_booking_inline(call: CallbackQuery, state: FSMContext):
    await state.clear()
    has_past = await db.has_completed_appointments(call.from_user.id)
    await state.set_state(BookingState.choosing_branch)
    await db.render_step(
        call,
        "step_1_branch",
        reply_markup=ikb.branches_kb(bs.get_branches(), has_past_visit=has_past)
    )
    await call.answer()


@router.message(F.text == "❌ Отменить запись")
async def cancel_booking_text(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Бронирование отменено. Вы вернулись в главное меню **Sahar&Vosk**.",
        reply_markup=await _get_main_kb_for(message.from_user.id),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "ignore")
async def ignore_callback(call: CallbackQuery):
    await call.answer()


@router.callback_query(F.data.startswith("branch:"))
async def select_branch(call: CallbackQuery, state: FSMContext):
    branch_id = call.data.split(":")[1]
    await state.clear()
    await state.update_data(branch_id=branch_id, cat_page=1, selected_services=[], reschedule_app_id=None)
    await state.set_state(BookingState.choosing_category)

    branch = bs.get_branches().get(branch_id, {})
    categories = bs.get_categories_by_branch(branch_id)
    await db.render_step(
        call,
        "step_2_category",
        reply_markup=ikb.categories_kb(categories, page=1, selected_count=0),
        branch_title=branch.get("title", branch_id)
    )
    await call.answer()


@router.callback_query(F.data.startswith("cat_pg:"))
async def paginate_categories(call: CallbackQuery, state: FSMContext):
    data = await _require_state(call, state, "branch_id")
    if not data:
        return
    try:
        page = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer("Некорректная страница.", show_alert=True)
        return
    await state.update_data(cat_page=page)
    categories = bs.get_categories_by_branch(data["branch_id"])
    branch = bs.get_branches().get(data["branch_id"], {})
    sel_cnt = len(data.get("selected_services", []))

    await db.render_step(
        call,
        "step_2_category",
        reply_markup=ikb.categories_kb(categories, page=page, selected_count=sel_cnt),
        branch_title=branch.get("title", data["branch_id"])
    )
    await call.answer()


@router.callback_query(F.data == "back:branches")
async def back_to_branches(call: CallbackQuery, state: FSMContext):
    await state.set_state(BookingState.choosing_branch)
    has_past = await db.has_completed_appointments(call.from_user.id)
    await db.render_step(
        call,
        "step_1_branch",
        reply_markup=ikb.branches_kb(bs.get_branches(), has_past_visit=has_past)
    )
    await call.answer()


@router.callback_query(F.data.startswith("cat:"))
async def select_category(call: CallbackQuery, state: FSMContext):
    data = await _require_state(call, state, "branch_id")
    if not data:
        return

    cat_id = call.data.split(":")[1]
    branch_id = data["branch_id"]
    branch = bs.get_branches().get(branch_id, {})
    cat_title = bs.get_main_category_title(cat_id)
    subcategories = bs.get_subcategories_by_category(branch_id, cat_id)
    sel_cnt = len(data.get("selected_services", []))

    await state.update_data(cat_id=cat_id, subcat_page=1, sub2_id=None)
    await state.set_state(BookingState.choosing_subcategory)

    await db.render_step(
        call,
        f"cat_{cat_id}",
        fallback_key="step_2_5_subcategory",
        reply_markup=ikb.subcategories_kb(subcategories, page=1, selected_count=sel_cnt),
        branch_title=branch.get("title", branch_id),
        cat_title=cat_title
    )
    await call.answer()


@router.callback_query(F.data.startswith("subcat_pg:"))
async def paginate_subcategories(call: CallbackQuery, state: FSMContext):
    data = await _require_state(call, state, "branch_id", "cat_id")
    if not data:
        return

    try:
        page = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer("Некорректная страница.", show_alert=True)
        return
    await state.update_data(subcat_page=page)
    branch_id = data["branch_id"]
    cat_id = data["cat_id"]
    branch = bs.get_branches().get(branch_id, {})
    cat_title = bs.get_main_category_title(cat_id)
    subcategories = bs.get_subcategories_by_category(branch_id, cat_id)
    sel_cnt = len(data.get("selected_services", []))

    await db.render_step(
        call,
        f"cat_{cat_id}",
        fallback_key="step_2_5_subcategory",
        reply_markup=ikb.subcategories_kb(subcategories, page=page, selected_count=sel_cnt),
        branch_title=branch.get("title", branch_id),
        cat_title=cat_title
    )
    await call.answer()


@router.callback_query(F.data == "back:categories")
async def back_to_categories(call: CallbackQuery, state: FSMContext, answer_cb: bool = True):
    data = await _require_state(call, state, "branch_id")
    if not data:
        return

    page = data.get("cat_page", 1)
    branch = bs.get_branches().get(data["branch_id"], {})
    categories = bs.get_categories_by_branch(data["branch_id"])
    sel_cnt = len(data.get("selected_services", []))

    await state.set_state(BookingState.choosing_category)
    await db.render_step(
        call,
        "step_2_category",
        reply_markup=ikb.categories_kb(categories, page=page, selected_count=sel_cnt),
        branch_title=branch.get("title", data["branch_id"])
    )
    if answer_cb:
        await call.answer()


@router.callback_query(F.data.startswith("subcat:"))
async def select_subcategory(call: CallbackQuery, state: FSMContext):
    data = await _require_state(call, state, "branch_id", "cat_id")
    if not data:
        return

    subcat_id = call.data.split(":")[1]
    branch_id = data["branch_id"]
    cat_id = data["cat_id"]
    selected_ids = data.get("selected_services", [])

    if not bs.is_subcategory_direct(cat_id, subcat_id):
        sub2_options = bs.get_sub2_options(branch_id, cat_id, subcat_id)
        if sub2_options:
            await state.update_data(subcat_id=subcat_id, sub2_id=None)
            await state.set_state(BookingState.choosing_sub2)
            branch = bs.get_branches().get(branch_id, {})
            subcat_title = bs.get_subcategory_title(cat_id, subcat_id)
            await db.render_step(
                call,
                f"sc_{cat_id}_{subcat_id}",
                fallback_key="step_2_5_subcategory",
                reply_markup=ikb.sub2_kb(sub2_options, selected_count=len(selected_ids)),
                branch_title=branch.get("title", branch_id),
                cat_title=subcat_title,
                subcat_title=subcat_title
            )
            await call.answer()
            return

    await state.update_data(subcat_id=subcat_id, sub2_id=None, srv_page=1)
    await state.set_state(BookingState.choosing_service)

    services = bs.get_services_by_subcategory(branch_id, cat_id, subcat_id)
    cat_title = bs.get_main_category_title(cat_id)
    subcat_title = bs.get_subcategory_title(cat_id, subcat_id)
    branch_title = bs.get_branches().get(branch_id, {}).get("title", branch_id)
    step_key = _resolve_service_list_step_key(cat_id, subcat_id, None)

    await db.render_step(
        call,
        step_key,
        fallback_key="step_3_service",
        reply_markup=ikb.services_kb(services, page=1, selected_ids=selected_ids, has_sub2=False),
        branch_title=branch_title,
        cat_title=cat_title,
        subcat_title=subcat_title
    )
    await call.answer()


@router.callback_query(F.data.startswith("sub2:"))
async def select_sub2(call: CallbackQuery, state: FSMContext):
    data = await _require_state(call, state, "branch_id", "cat_id", "subcat_id")
    if not data:
        return

    sub2_id = call.data.split(":")[1]
    branch_id = data["branch_id"]
    cat_id = data["cat_id"]
    subcat_id = data["subcat_id"]

    await state.update_data(sub2_id=sub2_id, srv_page=1)
    await state.set_state(BookingState.choosing_service)

    services = bs.get_services_by_subcategory(branch_id, cat_id, subcat_id, sub2_id=sub2_id)
    cat_title = bs.get_main_category_title(cat_id)
    subcat_title = bs.get_subcategory_title(cat_id, subcat_id, sub2_id=sub2_id)
    branch_title = bs.get_branches().get(branch_id, {}).get("title", branch_id)
    selected_ids = data.get("selected_services", [])
    step_key = _resolve_service_list_step_key(cat_id, subcat_id, sub2_id)

    await db.render_step(
        call,
        step_key,
        fallback_key=f"sc_{cat_id}_{subcat_id}",
        reply_markup=ikb.services_kb(services, page=1, selected_ids=selected_ids, has_sub2=True),
        branch_title=branch_title,
        cat_title=cat_title,
        subcat_title=subcat_title
    )
    await call.answer()


@router.callback_query(F.data == "back:sub2")
async def back_to_sub2(call: CallbackQuery, state: FSMContext):
    data = await _require_state(call, state, "branch_id", "cat_id", "subcat_id")
    if not data:
        return

    branch_id = data["branch_id"]
    cat_id = data["cat_id"]
    subcat_id = data["subcat_id"]
    sub2_options = bs.get_sub2_options(branch_id, cat_id, subcat_id)
    sel_cnt = len(data.get("selected_services", []))

    await state.set_state(BookingState.choosing_sub2)
    branch = bs.get_branches().get(branch_id, {})
    subcat_title = bs.get_subcategory_title(cat_id, subcat_id)
    await db.render_step(
        call,
        f"sc_{cat_id}_{subcat_id}",
        fallback_key="step_2_5_subcategory",
        reply_markup=ikb.sub2_kb(sub2_options, selected_count=sel_cnt),
        branch_title=branch.get("title", branch_id),
        cat_title=subcat_title,
        subcat_title=subcat_title
    )
    await call.answer()


@router.callback_query(F.data.startswith("srv_pg:"))
async def paginate_services(call: CallbackQuery, state: FSMContext):
    data = await _require_state(call, state, "branch_id", "cat_id")
    if not data:
        return

    try:
        page = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer("Некорректная страница.", show_alert=True)
        return
    await state.update_data(srv_page=page)
    branch_id = data["branch_id"]
    cat_id = data["cat_id"]
    subcat_id = data.get("subcat_id", "all")
    sub2_id = data.get("sub2_id")

    services = bs.get_services_by_subcategory(branch_id, cat_id, subcat_id, sub2_id=sub2_id)
    cat_title = bs.get_main_category_title(cat_id)
    subcat_title = bs.get_subcategory_title(cat_id, subcat_id, sub2_id=sub2_id)
    branch_title = bs.get_branches().get(branch_id, {}).get("title", branch_id)
    selected_ids = data.get("selected_services", [])
    step_key = _resolve_service_list_step_key(cat_id, subcat_id, sub2_id)

    await db.render_step(
        call,
        step_key,
        fallback_key="step_3_service",
        reply_markup=ikb.services_kb(
            services,
            page=page,
            selected_ids=selected_ids,
            has_sub2=bool(sub2_id)
        ),
        branch_title=branch_title,
        cat_title=cat_title,
        subcat_title=subcat_title
    )
    await call.answer()


@router.callback_query(F.data == "back:subcategories")
async def back_to_subcategories(call: CallbackQuery, state: FSMContext):
    data = await _require_state(call, state, "branch_id", "cat_id")
    if not data:
        return

    branch_id = data["branch_id"]
    cat_id = data["cat_id"]
    page = data.get("subcat_page", 1)
    sel_cnt = len(data.get("selected_services", []))

    branch = bs.get_branches().get(branch_id, {})
    cat_title = bs.get_main_category_title(cat_id)
    subcategories = bs.get_subcategories_by_category(branch_id, cat_id)

    await state.set_state(BookingState.choosing_subcategory)
    await db.render_step(
        call,
        f"cat_{cat_id}",
        fallback_key="step_2_5_subcategory",
        reply_markup=ikb.subcategories_kb(subcategories, page=page, selected_count=sel_cnt),
        branch_title=branch.get("title", branch_id),
        cat_title=cat_title
    )
    await call.answer()


# =====================================================================
# КОРЗИНА УСЛУГ
# =====================================================================

async def _render_cart_screen(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    branch_id = data["branch_id"]
    selected_ids = data.get("selected_services", [])
    branch = bs.get_branches().get(branch_id, {})
    summary = _build_booking_summary(selected_ids, data)

    # Умные допродажи (Upsell): подбор 1-2 подходящих сопутствующих услуг
    upsell_services = bs.get_upsell_recommendations(branch_id, selected_ids, max_count=2)
    upsell_block_str = ""
    if upsell_services:
        upsell_step = await db.get_step_content("step_cart_upsell")
        u_raw = upsell_step["custom_text"] or db.DEFAULT_STEPS.get("step_cart_upsell", {}).get("text", "")
        u_formatted = db._format_step_text(u_raw, {
            "branch_title": escape_md(branch.get("title", branch_id)),
            "total_price": summary["total_price"],
            "total_duration": summary["total_duration"]
        })
        if u_formatted:
            upsell_block_str = f"{u_formatted.strip()}\n\n"

    await state.set_state(BookingState.managing_cart)
    await db.render_step(
        call,
        "step_cart",
        reply_markup=ikb.cart_kb(summary["services"], upsell_services=upsell_services),
        branch_title=escape_md(branch.get("title", branch_id)),
        count=summary["count"],
        services_list=summary["bullet_lines_md"],
        total_price=summary["total_price"],
        total_duration=summary["total_duration"],
        upsell_block=upsell_block_str
    )


@router.callback_query(F.data.startswith("upsell_add:"))
async def upsell_add_service(call: CallbackQuery, state: FSMContext):
    """Быстрое добавление рекомендованной сопутствующей услуги в 1 клик."""
    user_id = getattr(getattr(call, "from_user", None), "id", getattr(call, "user_id", 0))
    if user_id in _active_cart_ops:
        await call.answer()
        return
    _active_cart_ops.add(user_id)
    try:
        data = await _require_state(call, state, "branch_id")
        if not data:
            return

        service_id = call.data.split(":")[1]
        branch_id = data["branch_id"]
        selected_ids = list(data.get("selected_services", []))

        if service_id in selected_ids:
            await call.answer("Эта процедура уже добавлена в ваш визит ✨", show_alert=False)
            await _render_cart_screen(call, state)
            return

        ok, reason = bs.can_combine_services(branch_id, selected_ids, service_id)
        if not ok:
            await call.answer(reason, show_alert=True)
            return

        candidate_list = selected_ids + [service_id]
        masters = await _resolve_masters_for_services(branch_id, candidate_list)
        if not masters:
            await call.answer(
                "⚠️ Эту услугу выполняет другой специалист, поэтому объединить их в один визит сейчас нельзя.",
                show_alert=True
            )
            return

        selected_ids.append(service_id)
        await state.update_data(
            service_id=selected_ids[0],
            selected_services=selected_ids,
            mst_page=1
        )
        await call.answer("Добавлено к визиту ✨")
        await _render_cart_screen(call, state)
    finally:
        _active_cart_ops.discard(user_id)


@router.callback_query(F.data.startswith("srv:"))
async def select_service(call: CallbackQuery, state: FSMContext):
    user_id = getattr(getattr(call, "from_user", None), "id", getattr(call, "user_id", 0))
    if user_id in _active_cart_ops:
        await call.answer()
        return
    _active_cart_ops.add(user_id)
    try:
        data = await _require_state(call, state, "branch_id", "cat_id")
        if not data:
            return

        service_id = call.data.split(":")[1]
        branch_id = data["branch_id"]
        selected_ids = list(data.get("selected_services", []))

        if service_id in selected_ids:
            await call.answer("Эта процедура уже добавлена в ваш список.", show_alert=False)
            await _render_cart_screen(call, state)
            return

        ok, reason = bs.can_combine_services(branch_id, selected_ids, service_id)
        if not ok:
            await call.answer(reason, show_alert=True)
            return

        candidate_list = selected_ids + [service_id]
        if len(candidate_list) > 1:
            masters = await _resolve_masters_for_services(branch_id, candidate_list)
            if not masters:
                await call.answer(
                    "⚠️ Эту услугу выполняет другой профильный специалист, поэтому объединить их в один сеанс нельзя.\n\n"
                    "Завершите текущую запись, а затем оформите вторую процедуру отдельно.",
                    show_alert=True
                )
                return

        selected_ids.append(service_id)
        await state.update_data(
            service_id=selected_ids[0],
            selected_services=selected_ids,
            mst_page=1
        )
        await _render_cart_screen(call, state)
        await call.answer("Процедура добавлена ✨")
    finally:
        _active_cart_ops.discard(user_id)


@router.callback_query(F.data == "cart:open")
async def open_cart_callback(call: CallbackQuery, state: FSMContext):
    data = await _require_state(call, state, "branch_id")
    if not data:
        return
    if not data.get("selected_services"):
        await call.answer("Ваш список выбранных процедур пока пуст. Выберите хотя бы одну услугу из списка.", show_alert=True)
        return
    await _render_cart_screen(call, state)
    await call.answer()


@router.callback_query(F.data == "cart:add_more")
async def cart_add_more_same_category(call: CallbackQuery, state: FSMContext, answer_cb: bool = True):
    data = await _require_state(call, state, "branch_id")
    if not data:
        return

    cat_id = data.get("cat_id")
    if not cat_id or cat_id not in bs.MAIN_CATEGORIES_CONFIG:
        await cart_add_from_other_category(call, state, answer_cb=answer_cb)
        return

    branch_id = data["branch_id"]
    subcat_id = data.get("subcat_id", "all")
    sub2_id = data.get("sub2_id")
    page = data.get("srv_page", 1)
    selected_ids = data.get("selected_services", [])

    services = bs.get_services_by_subcategory(branch_id, cat_id, subcat_id, sub2_id=sub2_id)
    cat_title = bs.get_main_category_title(cat_id)
    subcat_title = bs.get_subcategory_title(cat_id, subcat_id, sub2_id=sub2_id)
    branch_title = bs.get_branches().get(branch_id, {}).get("title", branch_id)
    step_key = _resolve_service_list_step_key(cat_id, subcat_id, sub2_id)

    await state.set_state(BookingState.choosing_service)
    await db.render_step(
        call,
        step_key,
        fallback_key="step_3_service",
        reply_markup=ikb.services_kb(
            services,
            page=page,
            selected_ids=selected_ids,
            has_sub2=bool(sub2_id)
        ),
        branch_title=branch_title,
        cat_title=cat_title,
        subcat_title=subcat_title
    )
    if answer_cb:
        await call.answer("Выберите дополнительную процедуру")


@router.callback_query(F.data == "cart:add_other_cat")
async def cart_add_from_other_category(call: CallbackQuery, state: FSMContext, answer_cb: bool = True):
    data = await _require_state(call, state, "branch_id")
    if not data:
        return

    branch_id = data["branch_id"]
    branch = bs.get_branches().get(branch_id, {})
    categories = bs.get_categories_by_branch(branch_id)
    sel_cnt = len(data.get("selected_services", []))

    await state.set_state(BookingState.choosing_category)
    await db.render_step(
        call,
        "step_2_category",
        reply_markup=ikb.categories_kb(categories, page=1, selected_count=sel_cnt),
        branch_title=branch.get("title", branch_id)
    )
    if answer_cb:
        await call.answer("Выберите направление для добавления процедуры")


@router.callback_query(F.data.startswith("cart_del:"))
async def cart_delete_item(call: CallbackQuery, state: FSMContext):
    user_id = getattr(getattr(call, "from_user", None), "id", getattr(call, "user_id", 0))
    if user_id in _active_cart_ops:
        await call.answer()
        return
    _active_cart_ops.add(user_id)
    try:
        data = await _require_state(call, state, "branch_id")
        if not data:
            return

        del_id = call.data.split(":")[1]
        selected_ids = [sid for sid in data.get("selected_services", []) if str(sid) != str(del_id)]
        if selected_ids and all(bs.is_standalone_addon_service(bs.get_service_by_id(sid) or {}) for sid in selected_ids):
            await call.answer(
                "⚠️ Доп. услуга выполняется только вместе с основной процедурой. Сначала удалите доп. услугу или очистите выбор.",
                show_alert=True
            )
            return
        await state.update_data(
            selected_services=selected_ids,
            service_id=selected_ids[0] if selected_ids else ""
        )

        if not selected_ids:
            await call.answer("Список очищен")
            await cart_add_more_same_category(call, state, answer_cb=False)
            return

        await call.answer("Процедура убрана из списка")
        await _render_cart_screen(call, state)
    finally:
        _active_cart_ops.discard(user_id)


@router.callback_query(F.data == "cart:clear")
async def cart_clear_all(call: CallbackQuery, state: FSMContext):
    data = await _require_state(call, state, "branch_id")
    if not data:
        return
    await state.update_data(selected_services=[], service_id="")
    await call.answer("Выбор очищен")
    await back_to_categories(call, state, answer_cb=False)


# --- ВЫБОР МАСТЕРА, ДАТЫ И ВРЕМЕНИ ---

@router.callback_query(F.data == "cart:proceed")
async def proceed_from_cart_to_masters(call: CallbackQuery, state: FSMContext):
    data = await _require_state(call, state, "branch_id")
    if not data:
        return

    selected_ids = data.get("selected_services", [])
    if not selected_ids:
        await call.answer("Сначала выберите хотя бы одну процедуру.", show_alert=True)
        return

    masters = await _resolve_masters_for_services(data["branch_id"], selected_ids)
    if not masters:
        await call.answer(
            "⚠️ Нет доступных специалистов для выбранного сочетания процедур. Попробуйте изменить состав визита.",
            show_alert=True
        )
        return

    summary = _build_booking_summary(selected_ids, data)
    await state.update_data(
        mst_page=1,
        master_names_map={str(m["id"]): m["name"] for m in masters}
    )
    await state.set_state(BookingState.choosing_master)

    await db.render_step(
        call,
        "step_4_master",
        reply_markup=ikb.masters_kb(masters, page=1),
        service_title=escape_md(summary["titles_inline"]),
        price=summary["total_price"]
    )
    await call.answer()


@router.callback_query(BookingState.choosing_master, F.data.startswith("mst_pg:"))
async def paginate_masters(call: CallbackQuery, state: FSMContext):
    data = await _require_state(call, state, "branch_id")
    if not data:
        return

    try:
        page = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer("Некорректная страница.", show_alert=True)
        return
    await state.update_data(mst_page=page)
    selected_ids = data.get("selected_services") or ([data["service_id"]] if data.get("service_id") else [])
    summary = _build_booking_summary(selected_ids, data)
    masters = await _resolve_masters_for_services(data["branch_id"], selected_ids)
    await state.update_data(master_names_map={str(m["id"]): m["name"] for m in masters})

    await db.render_step(
        call,
        "step_4_master",
        reply_markup=ikb.masters_kb(masters, page=page),
        service_title=escape_md(summary["titles_inline"]),
        price=summary["total_price"]
    )
    await call.answer()


@router.callback_query(F.data == "back:services")
async def back_to_services(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await cart_add_more_same_category(call, state, answer_cb=False)


@router.callback_query(F.data.startswith("master:"))
async def select_master(call: CallbackQuery, state: FSMContext):
    data = await _require_state(call, state, "branch_id")
    if not data:
        return

    master_id = call.data.split(":")[1]
    selected_ids = data.get("selected_services") or ([data["service_id"]] if data.get("service_id") else [])

    api_dates = await yclients_api.fetch_available_dates(
        branch_id=data["branch_id"],
        master_id=master_id,
        service_ids=selected_ids
    )

    await state.update_data(master_id=master_id, api_available_dates=api_dates)
    await state.set_state(BookingState.choosing_date)

    today = db.now_msk().date()
    master_name = _resolve_master_name(master_id, data)

    await db.render_step(
        call,
        "step_5_date",
        reply_markup=ikb.calendar_kb(today.year, today.month, api_dates=api_dates),
        master_name=escape_md(master_name)
    )
    await call.answer()


@router.callback_query(F.data.startswith("cal:"))
async def switch_calendar_month(call: CallbackQuery, state: FSMContext):
    data = await _require_state(call, state, "branch_id", "master_id")
    if not data:
        return
    api_dates = data.get("api_available_dates")
    try:
        _, year_str, month_str = call.data.split(":")
        year, month = int(year_str), int(month_str)
        if not (1 <= month <= 12):
            raise ValueError("month out of range")
    except (ValueError, IndexError):
        await call.answer("Некорректный месяц.", show_alert=True)
        return
    try:
        await call.message.edit_reply_markup(reply_markup=ikb.calendar_kb(year, month, api_dates=api_dates))
    except TelegramBadRequest as e:
        err_l = str(e).lower()
        if "message is not modified" not in err_l and "message can't be edited" not in err_l and "message to edit not found" not in err_l:
            pass
    await call.answer()


@router.callback_query(F.data == "back:masters")
async def back_to_masters(call: CallbackQuery, state: FSMContext):
    data = await _require_state(call, state, "branch_id")
    if not data:
        return

    page = data.get("mst_page", 1)
    selected_ids = data.get("selected_services") or ([data["service_id"]] if data.get("service_id") else [])
    summary = _build_booking_summary(selected_ids, data)
    masters = await _resolve_masters_for_services(data["branch_id"], selected_ids)
    await state.update_data(master_names_map={str(m["id"]): m["name"] for m in masters})

    await state.set_state(BookingState.choosing_master)
    await db.render_step(
        call,
        "step_4_master",
        reply_markup=ikb.masters_kb(masters, page=page),
        service_title=escape_md(summary["titles_inline"]),
        price=summary["total_price"]
    )
    await call.answer()


@router.callback_query(F.data.startswith("date:"))
async def select_date(call: CallbackQuery, state: FSMContext):
    data = await _require_state(call, state, "branch_id", "master_id")
    if not data:
        return

    parts = call.data.split(":")
    date_str = parts[1] if len(parts) > 1 else ""
    try:
        formatted_date = date.fromisoformat(date_str).strftime("%d.%m.%Y")
    except ValueError:
        await call.answer("Некорректная дата.", show_alert=True)
        return
    selected_ids = data.get("selected_services") or ([data["service_id"]] if data.get("service_id") else [])

    times = await yclients_api.fetch_available_times(
        branch_id=data["branch_id"],
        master_id=data["master_id"],
        service_ids=selected_ids,
        date_str=date_str
    )

    if not times and not yclients_api.USE_YCLIENTS_API:
        booked_slots = await db.get_booked_slots(
            branch_id=data["branch_id"],
            master_id=data["master_id"],
            visit_date=date_str
        )
        times = bs.get_available_times(date_str, booked_slots=booked_slots)

    if not times:
        await call.answer("На выбранную дату свободных окон не осталось. Пожалуйста, выберите другой день.", show_alert=True)
        return

    await state.update_data(visit_date=date_str)
    await state.set_state(BookingState.choosing_time)

    await db.render_step(
        call,
        "step_6_time",
        reply_markup=ikb.times_kb(times),
        formatted_date=formatted_date
    )
    await call.answer()


@router.callback_query(F.data == "back:dates")
async def back_to_dates(call: CallbackQuery, state: FSMContext):
    data = await _require_state(call, state, "branch_id", "master_id")
    if not data:
        return

    master_name = _resolve_master_name(data["master_id"], data)
    api_dates = data.get("api_available_dates")
    today = db.now_msk().date()
    await state.set_state(BookingState.choosing_date)
    await db.render_step(
        call,
        "step_5_date",
        reply_markup=ikb.calendar_kb(today.year, today.month, api_dates=api_dates),
        master_name=escape_md(master_name)
    )
    await call.answer()


@router.callback_query(F.data.startswith("time:"))
async def select_time(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    if uid in _pending_time_selection:
        await call.answer("⏳ Проверяем выбранное время...")
        return
    _pending_time_selection.add(uid)
    try:
        data = await _require_state(call, state, "branch_id", "master_id", "visit_date")
        if not data:
            return

        visit_time = call.data.split(":", 1)[1]
        await state.update_data(visit_time=visit_time)

        if (
            (data.get("reschedule_app_id") or data.get("is_repeat"))
            and _normalize_phone(data.get("phone"))
            and data.get("client_name")
        ):
            try:
                await call.message.delete()
            except Exception:
                pass
            await _verify_slot_and_confirm(call.message, state)
            await call.answer()
            return

        await state.set_state(BookingState.entering_phone)

        try:
            await call.message.delete()
        except Exception:
            pass
        await db.render_step(call.message, "step_phone", reply_markup=get_phone_keyboard())
        await call.answer()
    finally:
        _pending_time_selection.discard(uid)


@router.message(
    BookingState.entering_phone,
    F.contact | (F.text & ~F.text.in_(MAIN_MENU_BUTTONS) & ~F.text.startswith("/"))
)
async def process_phone(message: Message, state: FSMContext):
    raw_phone = message.contact.phone_number if message.contact else (message.text or "").strip()
    phone = _normalize_phone(raw_phone)
    if not phone:
        await message.answer("⚠️ Пожалуйста, введите корректный номер телефона (например, `+79991234567`) или нажмите кнопку ниже.", parse_mode="Markdown")
        return

    saved_real_name = await db.get_user_real_name(message.from_user.id)
    await state.update_data(phone=phone)

    if not saved_real_name:
        await state.set_state(BookingState.entering_name)
        tg_first = message.from_user.first_name
        hint = " (или выберите кнопку ниже)" if _is_valid_human_name(tg_first) else ""
        await db.render_step(
            message,
            "step_name",
            reply_markup=_get_name_prompt_kb(tg_first),
            hint=hint
        )
        return

    await state.update_data(client_name=saved_real_name)
    await _verify_slot_and_confirm(message, state)


@router.message(
    BookingState.entering_name,
    F.text & ~F.text.in_(MAIN_MENU_BUTTONS) & ~F.text.startswith("/")
)
async def process_client_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not _is_valid_human_name(name):
        await message.answer(
            "⚠️ Пожалуйста, введите корректное имя (от 2 до 35 букв, без цифр и спецсимволов)."
        )
        return

    await db.set_user_real_name(message.from_user.id, name)
    await state.update_data(client_name=name)
    await _verify_slot_and_confirm(message, state)


@router.message(BookingState.entering_phone, ~F.contact & ~F.text)
async def process_phone_unsupported(message: Message):
    await message.answer(
        "⚠️ Пожалуйста, отправьте номер телефона текстом (например, `+79991234567`) или нажмите кнопку «📱 Поделиться контактом».",
        parse_mode="Markdown"
    )


@router.message(BookingState.entering_name, ~F.text)
async def process_client_name_unsupported(message: Message):
    await message.answer("⚠️ Пожалуйста, введите ваше имя текстом (от 2 до 35 букв).")


async def _verify_slot_and_confirm(message: Message, state: FSMContext):
    data = await state.get_data()
    uid = getattr(getattr(message, "from_user", None), "id", None)
    if not data.get("branch_id") or not data.get("visit_date") or not data.get("visit_time"):
        await state.clear()
        await message.answer("🔄 Сессия обновилась. Пожалуйста, начните запись заново.", reply_markup=await _get_main_kb_for(uid))
        return

    selected_ids = data.get("selected_services") or ([data["service_id"]] if data.get("service_id") else [])

    is_free, fresh_times, err_msg = await yclients_api.check_slot_available(
        branch_id=data["branch_id"],
        master_id=data["master_id"],
        service_ids=selected_ids,
        date_str=data["visit_date"],
        visit_time=data["visit_time"]
    )
    booked_local = [] if yclients_api.USE_YCLIENTS_API else await db.get_booked_slots(data["branch_id"], data["master_id"], data["visit_date"])
    if not is_free or (booked_local and data["visit_time"] in booked_local):
        await message.answer(
            f"⚠️ **К сожалению, пока вы вводили контакты, время {data['visit_time']} уже забронировали.**\n"
            "Пожалуйста, выберите другое удобное время:",
            reply_markup=await _get_main_kb_for(uid),
            parse_mode="Markdown"
        )
        if not fresh_times and not yclients_api.USE_YCLIENTS_API:
            fresh_times = bs.get_available_times(data["visit_date"], booked_slots=booked_local)

        if fresh_times:
            await state.set_state(BookingState.choosing_time)
            formatted_date = date.fromisoformat(data["visit_date"]).strftime("%d.%m.%Y")
            await db.render_step(
                message,
                "step_6_time",
                reply_markup=ikb.times_kb(fresh_times),
                formatted_date=formatted_date
            )
        else:
            await state.set_state(BookingState.choosing_date)
            api_dates = data.get("api_available_dates")
            if isinstance(api_dates, list) and data["visit_date"] in api_dates:
                api_dates = [d for d in api_dates if d != data["visit_date"]]
                await state.update_data(api_available_dates=api_dates)
            today = db.now_msk().date()
            await db.render_step(
                message,
                "step_5_date",
                reply_markup=ikb.calendar_kb(today.year, today.month, api_dates=api_dates),
                master_name=escape_md(_resolve_master_name(data["master_id"], data))
            )
        return

    await show_confirmation(message, state)


async def show_confirmation(message: Message, state: FSMContext):
    data = await state.get_data()
    branch = bs.get_branches().get(data["branch_id"], {})
    selected_ids = data.get("selected_services") or ([data["service_id"]] if data.get("service_id") else [])
    summary = _build_booking_summary(selected_ids, data)
    master_name = _resolve_master_name(data["master_id"], data)
    formatted_date = date.fromisoformat(data["visit_date"]).strftime("%d.%m.%Y")

    await state.set_state(BookingState.confirming)
    uid = getattr(getattr(message, "from_user", None), "id", None)
    await message.answer("✨ Проверьте параметры вашего визита:", reply_markup=await _get_main_kb_for(uid))
    await db.render_step(
        message,
        "step_confirm",
        reply_markup=ikb.confirm_kb(),
        branch_title=escape_md(branch.get("title", data["branch_id"])),
        service_title=summary["bullet_lines_md"],
        master_name=escape_md(master_name),
        formatted_date=formatted_date,
        visit_time=data["visit_time"],
        price=summary["total_price"],
        client_name=escape_md(data["client_name"]),
        phone=data["phone"]
    )


@router.callback_query(BookingState.confirming, F.data.startswith("confirm:"))
async def finalize_booking(call: CallbackQuery, state: FSMContext):
    parts = call.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "cancel":
        await state.clear()
        try:
            await call.message.delete()
        except Exception:
            pass
        await call.message.answer("✕ Бронирование отменено.", reply_markup=await _get_main_kb_for(call.from_user.id))
        await call.answer()
        return

    if action == "restart":
        await state.clear()
        await state.set_state(BookingState.choosing_branch)
        has_past = await db.has_completed_appointments(call.from_user.id)
        await db.render_step(call, "step_1_branch", reply_markup=ikb.branches_kb(bs.get_branches(), has_past_visit=has_past))
        await call.answer()
        return

    if action == "yes":
        uid = call.from_user.id
        if uid in _pending_bookings:
            await call.answer("⏳ Оформляем запись, пожалуйста подождите...")
            return

        _pending_bookings.add(uid)
        answered = False
        try:
            data = await _require_state(call, state, "branch_id", "master_id", "visit_date", "visit_time", "client_name", "phone")
            if not data:
                answered = True
                return

            old_app_id = data.get("reschedule_app_id")
            old_app = None
            if old_app_id:
                old_app = await db.get_appointment_by_id(old_app_id)
                if not old_app or old_app["status"] != "active":
                    await state.clear()
                    await call.answer("⚠️ Исходная запись уже была изменена или отменена.", show_alert=True)
                    answered = True
                    await _safe_edit_message(call, "⚠️ Исходная запись уже неактуальна. Вы можете оформить новый визит через меню.")
                    return

            branch = bs.get_branches().get(data["branch_id"], {})
            selected_ids = data.get("selected_services") or ([data["service_id"]] if data.get("service_id") else [])
            summary = _build_booking_summary(selected_ids, data)
            master_id = data["master_id"]
            master_name = _resolve_master_name(master_id, data)
            formatted_date = date.fromisoformat(data["visit_date"]).strftime("%d.%m.%Y")

            is_free, fresh_times, slot_err = await yclients_api.check_slot_available(
                branch_id=data["branch_id"],
                master_id=master_id,
                service_ids=selected_ids,
                date_str=data["visit_date"],
                visit_time=data["visit_time"]
            )
            booked_local = [] if yclients_api.USE_YCLIENTS_API else await db.get_booked_slots(data["branch_id"], master_id, data["visit_date"])

            if not is_free or (booked_local and data["visit_time"] in booked_local):
                await call.answer(
                    f"⚠️ Пока вы подтверждали запись, время {data['visit_time']} уже заняли! Выберите другое свободное окно."[:200],
                    show_alert=True
                )
                answered = True
                if not fresh_times and not yclients_api.USE_YCLIENTS_API:
                    fresh_times = bs.get_available_times(data["visit_date"], booked_slots=booked_local)

                if fresh_times:
                    await state.set_state(BookingState.choosing_time)
                    await db.render_step(
                        call,
                        "step_6_time",
                        reply_markup=ikb.times_kb(fresh_times),
                        formatted_date=formatted_date
                    )
                else:
                    await state.set_state(BookingState.choosing_date)
                    api_dates = data.get("api_available_dates")
                    if isinstance(api_dates, list) and data["visit_date"] in api_dates:
                        api_dates = [d for d in api_dates if d != data["visit_date"]]
                        await state.update_data(api_available_dates=api_dates)
                    today = db.now_msk().date()
                    await db.render_step(
                        call,
                        "step_5_date",
                        reply_markup=ikb.calendar_kb(today.year, today.month, api_dates=api_dates),
                        master_name=escape_md(master_name)
                    )
                return

            yclients_rec_id, err_msg, assigned_staff_id = await yclients_api.create_yclients_record({
                "branch_id": data["branch_id"],
                "service_ids": selected_ids,
                "master_id": master_id,
                "visit_date": data["visit_date"],
                "visit_time": data["visit_time"],
                "phone": data["phone"],
                "client_name": data["client_name"]
            })

            if not yclients_rec_id:
                await call.answer(
                    f"⚠️ Не удалось забронировать {data['visit_time']}: {err_msg or 'время уже занято'}. Выберите другой слот."[:200],
                    show_alert=True
                )
                answered = True
                fresh_times = await yclients_api.fetch_available_times(
                    data["branch_id"], master_id, selected_ids, data["visit_date"]
                )
                if fresh_times:
                    await state.set_state(BookingState.choosing_time)
                    await db.render_step(
                        call,
                        "step_6_time",
                        reply_markup=ikb.times_kb(fresh_times),
                        formatted_date=formatted_date
                    )
                else:
                    await state.set_state(BookingState.choosing_date)
                    api_dates = data.get("api_available_dates")
                    if isinstance(api_dates, list) and data["visit_date"] in api_dates:
                        api_dates = [d for d in api_dates if d != data["visit_date"]]
                        await state.update_data(api_available_dates=api_dates)
                    today = db.now_msk().date()
                    await db.render_step(
                        call,
                        "step_5_date",
                        reply_markup=ikb.calendar_kb(today.year, today.month, api_dates=api_dates),
                        master_name=escape_md(master_name)
                    )
                return

            if master_id == "any" and assigned_staff_id:
                master_id = assigned_staff_id
                master_name = _resolve_master_name(assigned_staff_id, data)

            try:
                appointment_id = await db.reschedule_and_create_appointment(
                    old_app_id=old_app_id if (old_app and old_app["status"] == "active") else None,
                    telegram_id=uid,
                    new_data={
                        "telegram_id": uid,
                        "client_name": data["client_name"],
                        "phone": data["phone"],
                        "client_phone": db.normalize_client_phone(data.get("phone")) or None,
                        "branch_id": data["branch_id"],
                        "branch_title": branch.get("title", data["branch_id"]),
                        "cat_id": data.get("cat_id", "other"),
                        "service_id": ",".join(str(x) for x in selected_ids) or str(yclients_rec_id),
                        "service_title": summary["titles_inline"],
                        "price": summary["total_price_num"],
                        "duration": summary["total_duration"],
                        "master_id": master_id,
                        "master_name": master_name,
                        "visit_date": data["visit_date"],
                        "visit_time": data["visit_time"],
                        "yclients_record_id": yclients_rec_id
                    }
                )
            except Exception as db_err:
                logging.exception(f"Ошибка сохранения записи в БД после создания YCLIENTS #{yclients_rec_id}: {db_err}")
                try:
                    rolled_back = await yclients_api.delete_yclients_record(data["branch_id"], yclients_rec_id)
                except Exception:
                    rolled_back = False
                await state.clear()
                rollback_note = (
                    "Запись в YCLIENTS автоматически отменена (откат выполнен)."
                    if rolled_back
                    else f"⚠️ **КРИТИЧНО:** не удалось откатить запись YCLIENTS `#{yclients_rec_id}` — проверьте CRM вручную!"
                )
                try:
                    await notify_admins(
                        call.bot,
                        f"🚨 **Сбой БД при сохранении записи · Sahar&Vosk**\n"
                        f"Клиент: {escape_md(data['client_name'])} (`{data['phone']}`)\n"
                        f"Студия: {escape_md(branch.get('title', data['branch_id']))}, {formatted_date} в {data['visit_time']}\n"
                        f"{rollback_note}"
                    )
                except Exception:
                    pass
                await call.answer(
                    "⚠️ Технический сбой при сохранении записи. Пожалуйста, попробуйте ещё раз или свяжитесь с администратором.",
                    show_alert=True
                )
                answered = True
                await _safe_edit_message(
                    call,
                    "⚠️ Не удалось завершить оформление из-за технического сбоя. Пожалуйста, выберите время заново или напишите администратору."
                )
                return

            # Сбрасываем FSM сразу после фиксации записи в БД, чтобы повторный клик не дублировал визит
            await state.clear()

            old_deleted = True
            if old_app_id and old_app and old_app["status"] == "active":
                remove_appointment_reminders(old_app_id)
                try:
                    old_deleted = await yclients_api.delete_yclients_record(old_app["branch_id"], old_app.get("yclients_record_id"))
                except Exception:
                    old_deleted = False

            try:
                await schedule_appointment_reminders(call.bot, appointment_id)
            except Exception as sched_err:
                logging.warning(f"Не удалось запланировать напоминания для записи №{appointment_id}: {sched_err}")

            user_link = f"@{escape_md(call.from_user.username)}" if call.from_user.username else "без username"
            crm_badge = f"(YCLIENTS #{yclients_rec_id})" if yclients_rec_id and yclients_rec_id != "mock_record_id" else ""
            header_title = f"↺ **Перенос визита (новая №{appointment_id}) · Sahar&Vosk**" if old_app_id else f"✨ **Новое бронирование №{appointment_id} · Sahar&Vosk**"
            crm_warn = (
                "\n\n⚠️ **ВНИМАНИЕ:** Не удалось удалить старую запись в YCLIENTS — проверьте и отмените её вручную!"
                if (old_app_id and not old_deleted)
                else ""
            )

            try:
                await notify_admins(
                    call.bot,
                    f"{header_title} {crm_badge}\n"
                    f"────────────────────\n"
                    f"📍 **Студия:** {escape_md(branch.get('title', data['branch_id']))}\n"
                    f"👤 **Клиент:** {escape_md(data['client_name'])} ({user_link})\n"
                    f"📞 **Телефон:** `{data['phone']}`\n"
                    f"✨ **Услуги ({summary['count']}):** {escape_md(summary['titles_inline'])} ({summary['total_price']})\n"
                    f"👩‍⚕️ **Специалист:** {escape_md(master_name)}\n"
                    f"🗓 **Дата и время:** {formatted_date} в **{data['visit_time']}**{crm_warn}"
                )
            except Exception as adm_err:
                logging.warning(f"Не удалось уведомить админов о записи №{appointment_id}: {adm_err}")

            address = branch.get("address", "г. Кудрово")
            google_cal_url = ch.generate_google_calendar_url(
                title=f"Sahar&Vosk — {summary['titles_inline']}",
                visit_date=data["visit_date"],
                visit_time=data["visit_time"],
                duration_min=summary["total_duration"],
                location=address,
                description=f"Специалист: {master_name}\nСтудия: Sahar&Vosk · {branch.get('title', data['branch_id'])}\nАдрес: {address}\nТелефон: +7 (812) 988-98-53"
            )

            try:
                await db.render_step(
                    call,
                    "step_success",
                    reply_markup=ikb.post_booking_kb(
                        appointment_id=appointment_id,
                        branch_id=data["branch_id"],
                        google_cal_url=google_cal_url
                    ),
                    appointment_id=appointment_id,
                    address=escape_md(address),
                    service_title=escape_md(summary["titles_inline"]),
                    price=summary["total_price"],
                    master_name=escape_md(master_name),
                    formatted_date=formatted_date,
                    visit_time=data["visit_time"]
                )
            except Exception as render_err:
                logging.warning(f"Ошибка отрисовки step_success для записи №{appointment_id}: {render_err}")
                try:
                    await call.message.answer(
                        f"✨ **Запись №{appointment_id} подтверждена · Sahar&Vosk**\n"
                        f"Ждём вас {formatted_date} в **{data['visit_time']}**!",
                        reply_markup=await _get_main_kb_for(uid),
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass

            await call.answer("Бронирование подтверждено ✨")
            answered = True
        finally:
            _pending_bookings.discard(uid)
            if not answered:
                try:
                    await call.answer()
                except Exception:
                    pass
        return

    await call.answer()


@router.callback_query(F.data.startswith(("confirm:", "mst_pg:")))
async def handle_stale_booking_callbacks(call: CallbackQuery, state: FSMContext):
    """Перехватывает устаревшие inline-кнопки после перезапуска бота или завершения сессии."""
    await state.clear()
    await state.set_state(BookingState.choosing_branch)
    has_past = await db.has_completed_appointments(call.from_user.id)
    await call.answer("🔄 Сессия обновилась. Пожалуйста, выберите студию заново.", show_alert=True)
    await db.render_step(
        call,
        "step_1_branch",
        reply_markup=ikb.branches_kb(bs.get_branches(), has_past_visit=has_past)
    )