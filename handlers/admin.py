import asyncio
import html
import re
import logging
from datetime import date, timedelta
from aiogram import Router, F, Bot
from aiogram.filters import Command, StateFilter
from aiogram.types import Message, CallbackQuery, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter, TelegramForbiddenError

from config import ADMIN_IDS
from states.admin import AdminState
from database import db
from services import booking_service as bs
from services import health_monitor
from services import yclients_api
from services.scheduler import scheduler, remove_appointment_reminders
from sync_yclients import run_yclients_sync, is_sync_running
from keyboards.default import get_main_keyboard, get_admin_chat_kb, MAIN_MENU_BUTTONS
from keyboards import inline as ikb

router = Router()
_ADMIN_MSG_FILTER = (F.text == None) | (~F.text.in_(MAIN_MENU_BUTTONS) & ~F.text.startswith("/"))
_pending_broadcasts: set[int] = set()
_pending_health_checks: set[int] = set()
_sync_in_progress: bool = False


escape_md = db.escape_md


def _clip_html_preview(raw_text: str | None, max_escaped_len: int = 2800) -> str:
    txt = raw_text or ""
    escaped = html.escape(txt)
    if len(escaped) <= max_escaped_len and db._utf16_len(escaped) <= max_escaped_len:
        return escaped
    suffix = "\n... [текст сокращён для предпросмотра]"
    cut = min(len(txt), max_escaped_len)
    while cut > 0:
        escaped = html.escape(txt[:cut] + suffix)
        if len(escaped) <= max_escaped_len and db._utf16_len(escaped) <= max_escaped_len:
            return escaped
        cut -= 200
    return html.escape(suffix.strip())


def _is_photo_message(message: Message | None) -> bool:
    if not message:
        return False
    raw_photo = getattr(message, "photo", None)
    return isinstance(raw_photo, (list, tuple, bool)) and bool(raw_photo)


async def _safe_admin_edit_md(message: Message | None, text: str, reply_markup=None):
    if not message:
        return
    clipped = db._clip_utf16(text, 4096)
    if _is_photo_message(message):
        try:
            await message.delete()
        except Exception:
            pass
        try:
            await message.answer(clipped, reply_markup=reply_markup, parse_mode="Markdown")
            return
        except TelegramBadRequest:
            try:
                await message.answer(clipped, reply_markup=reply_markup)
            except Exception:
                pass
            return

    try:
        await message.edit_text(clipped, reply_markup=reply_markup, parse_mode="Markdown")
    except TelegramBadRequest as e:
        err_low = str(e).lower()
        if "message is not modified" in err_low:
            return
        if "there is no text" in err_low or "message can't be edited" in err_low or "message to edit not found" in err_low:
            try:
                await message.delete()
            except Exception:
                pass
            try:
                await message.answer(clipped, reply_markup=reply_markup, parse_mode="Markdown")
                return
            except TelegramBadRequest:
                try:
                    await message.answer(clipped, reply_markup=reply_markup)
                except Exception:
                    pass
                return
        try:
            await message.edit_text(clipped, reply_markup=reply_markup)
        except Exception:
            pass


async def _safe_admin_edit_html(message: Message | None, text: str, reply_markup=None):
    if not message:
        return
    clipped = health_monitor.clip_html_report(text, 4096)

    if _is_photo_message(message):
        try:
            await message.delete()
        except Exception:
            pass
        try:
            await message.answer(clipped, reply_markup=reply_markup, parse_mode="HTML", disable_web_page_preview=True)
            return
        except TelegramBadRequest:
            plain = db._clip_utf16(re.sub(r"<[^>]+>", "", clipped), 4096)
            await message.answer(plain, reply_markup=reply_markup, disable_web_page_preview=True)
            return

    try:
        await message.edit_text(clipped, reply_markup=reply_markup, parse_mode="HTML", disable_web_page_preview=True)
    except TelegramBadRequest as e:
        err_low = str(e).lower()
        if "message is not modified" in err_low:
            return
        if "there is no text" in err_low or "message can't be edited" in err_low or "message to edit not found" in err_low:
            try:
                await message.delete()
            except Exception:
                pass
            try:
                await message.answer(clipped, reply_markup=reply_markup, parse_mode="HTML", disable_web_page_preview=True)
                return
            except TelegramBadRequest:
                plain = db._clip_utf16(re.sub(r"<[^>]+>", "", clipped), 4096)
                await message.answer(plain, reply_markup=reply_markup, disable_web_page_preview=True)
                return
        try:
            plain = db._clip_utf16(re.sub(r"<[^>]+>", "", clipped), 4096)
            await message.edit_text(plain, reply_markup=reply_markup, disable_web_page_preview=True)
        except Exception:
            pass

SEGMENT_TITLES = {
    "all": "Все гости студий Sahar&Vosk",
    "branch_prazhskaya": "Гости студии Sahar&Vosk (Пражская, 3)",
    "branch_leningradskaya": "Гости студии Sahar&Vosk (Ленинградская, 7)",
    "inactive_30": "Не посещали более 30 дней"
}

REPEAT_CATEGORIES = {
    "hair_removal": "✨ Удаление волос (Лазер / Шугаринг / Воск)",
    "manicure": "💅 Маникюр",
    "pedicure": "🦶 Педикюр",
    "brows": "🤨 Оформление бровей",
    "lashes": "👁 Ресницы",
    "massage": "💆‍♀️ Массаж и SPA",
    "cosmetology": "🌸 Эстетическая косметология",
    "men": "🧔 Мужской зал"
}


async def check_admin(user_id: int) -> bool:
    return await db.is_user_admin(user_id, ADMIN_IDS)


async def _is_admin_callback(call: CallbackQuery, state: FSMContext | None = None) -> bool:
    if await check_admin(call.from_user.id):
        return True
    if state is not None:
        await state.clear()
    await call.answer("⛔ Доступ ограничен.", show_alert=True)
    return False


async def _get_admin_main_kb(user_id: int | None):
    has_past = (
        await db.has_completed_appointments(user_id)
        if isinstance(user_id, int) and not isinstance(user_id, bool)
        else False
    )
    return get_main_keyboard(has_past_visit=has_past)


async def notify_admins(bot: Bot, text: str, reply_markup=None, photo_id: str | None = None):
    all_admins = await db.get_all_admin_ids(ADMIN_IDS)
    for admin_id in all_admins:
        try:
            if photo_id:
                cap = db._clip_utf16(text, 1024)
                try:
                    await bot.send_photo(chat_id=admin_id, photo=photo_id, caption=cap, reply_markup=reply_markup, parse_mode="Markdown")
                except TelegramBadRequest:
                    await bot.send_photo(chat_id=admin_id, photo=photo_id, caption=cap, reply_markup=reply_markup)
            else:
                msg_txt = db._clip_utf16(text, 4096)
                try:
                    await bot.send_message(chat_id=admin_id, text=msg_txt, reply_markup=reply_markup, parse_mode="Markdown")
                except TelegramBadRequest:
                    await bot.send_message(chat_id=admin_id, text=msg_txt, reply_markup=reply_markup)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 0.1)
            try:
                if photo_id:
                    await bot.send_photo(chat_id=admin_id, photo=photo_id, caption=db._clip_utf16(text, 1024), reply_markup=reply_markup)
                else:
                    await bot.send_message(chat_id=admin_id, text=db._clip_utf16(text, 4096), reply_markup=reply_markup)
            except Exception:
                pass
        except Exception:
            pass


async def send_to_archive_group(
    bot: Bot,
    branch_id: str,
    direction: str,
    client_name: str,
    client_id: int,
    username: str | None,
    text: str,
    photo_id: str | None = None,
    admin_name: str | None = None
):
    group_id = await db.get_archive_group_id(branch_id)
    if not group_id:
        return

    branch_title = bs.get_branches().get(branch_id, {}).get("title", branch_id)
    user_tag = f" (@{username})" if username else ""
    phone = await db.get_user_phone(client_id)
    phone_str = f" · 📞 <code>{html.escape(phone)}</code>" if phone else ""
    raw_body = text or "[Изображение / Медиафайл]"

    if direction == "from_client":
        header_html = (
            f"📥 <b>КЛИЕНТ ➡️ Sahar&Vosk ({html.escape(branch_title)})</b>\n"
            f"🔎 Профиль: #id{client_id}\n"
            f"👤 <b>{html.escape(client_name)}</b>{html.escape(user_tag)}{phone_str}\n"
            f"──────────────\n"
            f"🆔 <code>{client_id}</code> · <b>{html.escape(client_name)}:</b> "
        )
    else:
        header_html = (
            f"📤 <b>АДМИНИСТРАТОР ({html.escape(admin_name or 'Администратор')}) ➡️ КЛИЕНТУ</b>\n"
            f"📍 Студия: Sahar&Vosk ({html.escape(branch_title)})\n"
            f"🔎 Профиль: #id{client_id}\n"
            f"👤 Кому: <b>{html.escape(client_name)}</b>{html.escape(user_tag)}{phone_str}\n"
            f"──────────────\n"
            f"🆔 <code>{client_id}</code> · <b>Ответ:</b> "
        )

    max_len = 1024 if photo_id else 4096
    max_body_len = max(0, max_len - db._utf16_len(header_html))
    body_html = _clip_html_preview(raw_body, max_escaped_len=max_body_len) if max_body_len > 0 else ""
    full_msg = header_html + body_html
    parse_mode_arg: str | None = "HTML"
    if db._utf16_len(full_msg) > max_len:
        full_msg = db._clip_utf16(re.sub(r"<[^>]+>", "", full_msg), max_len)
        parse_mode_arg = None

    try:
        bot_me = await bot.get_me()
        kb = ikb.archive_jump_to_chat_kb(bot_me.username, client_id)
        if photo_id:
            await bot.send_photo(
                chat_id=group_id,
                photo=photo_id,
                caption=full_msg,
                reply_markup=kb,
                parse_mode=parse_mode_arg
            )
        else:
            await bot.send_message(
                chat_id=group_id,
                text=full_msg,
                reply_markup=kb,
                parse_mode=parse_mode_arg
            )
    except TelegramBadRequest:
        try:
            clean_text = re.sub(r"<[^>]+>", "", full_msg)
            if photo_id:
                await bot.send_photo(
                    chat_id=group_id,
                    photo=photo_id,
                    caption=clean_text[:1024],
                    reply_markup=kb
                )
            else:
                await bot.send_message(
                    chat_id=group_id,
                    text=clean_text[:4096],
                    reply_markup=kb
                )
        except Exception as e:
            logging.warning(f"Ошибка отправки в архивную группу {group_id}: {e}")
    except Exception as e:
        logging.warning(f"Ошибка отправки в архивную группу {group_id}: {e}")


@router.message(Command("bind_prazhskaya"))
async def cmd_bind_prazhskaya(message: Message):
    if not await check_admin(message.from_user.id):
        return
    await db.set_archive_group_id("prazhskaya", message.chat.id)
    await message.answer(
        f"✓ **Группа `{message.chat.id}` привязана как архив для филиала Sahar&Vosk (Пражская, 3)!**",
        parse_mode="Markdown"
    )


@router.message(Command("bind_leningradskaya"))
async def cmd_bind_leningradskaya(message: Message):
    if not await check_admin(message.from_user.id):
        return
    await db.set_archive_group_id("leningradskaya", message.chat.id)
    await message.answer(
        f"✓ **Группа `{message.chat.id}` привязана как архив для филиала Sahar&Vosk (Ленинградская, 7)!**",
        parse_mode="Markdown"
    )


@router.message(Command("my_id"))
async def cmd_my_id(message: Message):
    await message.answer(
        f"👤 Личный Telegram ID: `{message.from_user.id}`\n"
        f"💬 ID текущего чата: `{message.chat.id}`",
        parse_mode="Markdown"
    )


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if not await check_admin(message.from_user.id):
        await state.clear()
        await message.answer(f"⛔ Доступ ограничен. Ваш ID: `{message.from_user.id}`.", parse_mode="Markdown")
        return

    await state.clear()
    text, unread = await build_admin_dashboard_text()
    await message.answer("✨ Панель управления открыта:", reply_markup=await _get_admin_main_kb(message.from_user.id))
    await message.answer(text, reply_markup=ikb.admin_main_kb(unread), parse_mode="Markdown")


@router.callback_query(F.data.in_(("adm:home", "adm_back")))
async def cb_admin_home(call: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin_callback(call, state):
        return
    if state is not None:
        await state.clear()
    if not call.message:
        await call.answer()
        return
    text, unread = await build_admin_dashboard_text()
    await _safe_admin_edit_md(call.message, text, reply_markup=ikb.admin_main_kb(unread))
    await call.answer()


@router.callback_query(F.data.in_(("adm_health", "adm_health:refresh", "adm_health:max_ping")))
async def cb_admin_system_health(call: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin_callback(call, state):
        return
    if state is not None:
        await state.clear()
    if not call.message:
        await call.answer()
        return

    uid = call.from_user.id
    if uid in _pending_health_checks:
        await call.answer("⏳ Проверка систем уже выполняется...", show_alert=False)
        return

    _pending_health_checks.add(uid)
    try:
        if call.data in ("adm_health:refresh", "adm_health:max_ping"):
            await call.answer("Проверяю системы...")
        else:
            await call.answer()

        check_max_live = call.data == "adm_health:max_ping"
        status_html = await health_monitor.collect_system_status(
            call.bot,
            include_network_ping=True,
            check_max_live=check_max_live,
        )
        err_count = health_monitor.get_error_handler().buffered_len()
        await _safe_admin_edit_html(
            call.message,
            status_html,
            reply_markup=ikb.admin_health_kb(errors_count=err_count),
        )
    finally:
        _pending_health_checks.discard(uid)


@router.callback_query(F.data == "adm_health:errors")
async def cb_admin_health_errors(call: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin_callback(call, state):
        return
    if state is not None:
        await state.clear()
    if not call.message:
        await call.answer()
        return
    errors_html = health_monitor.get_recent_errors_text()
    await _safe_admin_edit_html(
        call.message,
        errors_html,
        reply_markup=ikb.admin_health_errors_kb(),
    )
    await call.answer()


@router.callback_query(F.data == "adm_health:clear_errors")
async def cb_admin_health_clear_errors(call: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin_callback(call, state):
        return
    if state is not None:
        await state.clear()
    if not call.message:
        await call.answer()
        return
    health_monitor.clear_error_buffer()
    errors_html = health_monitor.get_recent_errors_text()
    await _safe_admin_edit_html(
        call.message,
        errors_html,
        reply_markup=ikb.admin_health_errors_kb(),
    )
    await call.answer("Буфер ошибок очищен ✅")


async def build_admin_dashboard_text() -> tuple[str, int]:
    stats = await db.get_admin_stats()
    all_admins = await db.get_all_admin_ids(ADMIN_IDS)
    grp_pr = await db.get_archive_group_id("prazhskaya")
    grp_len = await db.get_archive_group_id("leningradskaya")

    text = (
        "✨ **Панель управления · Sahar&Vosk**\n"
        "──────────────\n"
        f"👥 **Клиентов в базе:** {stats['total_users']}\n"
        f"💬 **Ожидают ответа:** {stats['unread_chats']}\n"
        f"🛡 **Администраторов:** {len(all_admins)}\n"
        f"📅 **Активных записей:** {stats['active_apps']}\n"
        f"  ├ 📍 Пражская, 3: {stats['prazhskaya_apps']} *(архив: {'✓' if grp_pr else '✕ не привязан'})*\n"
        f"  └ 📍 Ленинградская, 7: {stats['leningradskaya_apps']} *(архив: {'✓' if grp_len else '✕ не привязан'})*\n"
        "──────────────\n"
        "Выберите действие для управления студией:"
    )
    return text, stats["unread_chats"]


@router.callback_query(F.data == "adm:sync")
async def cb_admin_sync_yclients(call: CallbackQuery, state: FSMContext | None = None):
    global _sync_in_progress
    if not await _is_admin_callback(call, state):
        return
    if state is not None:
        await state.clear()

    if _sync_in_progress or is_sync_running():
        await call.answer("⏳ Синхронизация каталога уже выполняется, подождите...", show_alert=False)
        return

    _sync_in_progress = True
    try:
        await call.answer("Запускаю синхронизацию...")
        await _safe_admin_edit_md(
            call.message,
            "🔄 **Синхронизация каталога Sahar&Vosk с YCLIENTS...**\n"
            "──────────────\n"
            "Обновляю услуги, цены, реальную длительность и привязки специалистов по обоим филиалам.\n\n"
            "⏳ Пожалуйста, подождите несколько секунд…"
        )

        try:
            sync_stats = await run_yclients_sync()
            dash_text, unread = await build_admin_dashboard_text()
            result_text = (
                "✨ **Синхронизация успешно завершена!**\n"
                "──────────────\n"
                f"📍 **Пражская, 3:** {sync_stats['prazh_services']} услуг · {sync_stats['prazh_masters']} специалистов\n"
                f"📍 **Ленинградская, 7:** {sync_stats['len_services']} услуг · {sync_stats['len_masters']} специалистов\n"
                f"📂 **Всего в каталоге:** {sync_stats['total_services']} процедур и {sync_stats['total_masters']} специалистов.\n\n"
                "──────────────\n" + dash_text
            )
            await _safe_admin_edit_md(
                call.message,
                result_text,
                reply_markup=ikb.admin_main_kb(unread)
            )
        except Exception as e:
            dash_text, unread = await build_admin_dashboard_text()
            err_safe = str(e).replace("`", "'")[:300]
            await _safe_admin_edit_md(
                call.message,
                f"✕ **Ошибка синхронизации:** `{err_safe}`\n\n" + dash_text,
                reply_markup=ikb.admin_main_kb(unread)
            )
    finally:
        _sync_in_progress = False


@router.callback_query(F.data == "adm:digest")
async def cb_admin_digest(call: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin_callback(call, state):
        return
    if state is not None:
        await state.clear()

    now = db.now_msk()
    end_date = now.date().isoformat()
    start_date = (now.date() - timedelta(days=7)).isoformat()

    text = await db.build_management_digest_text(start_date=start_date, end_date=end_date, is_weekly=False)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить сводку", callback_data="adm:digest")],
        [InlineKeyboardButton(text="◀️ В панель управления", callback_data="adm:home")]
    ])
    await _safe_admin_edit_md(call.message, text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data == "adm:export_catalog")
async def cb_admin_export_catalog(call: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin_callback(call, state):
        return
    if state is not None:
        await state.clear()

    await call.answer("Формирую каталог...")
    report_text = await asyncio.to_thread(bs.generate_catalog_report)
    file_bytes = report_text.encode("utf-8")
    doc = BufferedInputFile(file_bytes, filename="saharvosk_catalog.txt")

    await call.message.answer_document(
        document=doc,
        caption=(
            "📄 **Полный каталог процедур сети Sahar&Vosk**\n"
            "──────────────\n"
            "Выгрузка содержит перечень всех услуг, цен, длительностей и привязанных специалистов по обеим студиям."
        ),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "adm:support")
async def cb_admin_support_menu(call: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin_callback(call, state):
        return
    if state is not None:
        await state.clear()
    unread_count = await db.get_unread_threads_count()
    await _safe_admin_edit_md(
        call.message,
        "💬 **Центр сообщений и диалогов · Sahar&Vosk**\n"
        "──────────────\n"
        f"🔴 **Ожидают ответа:** {unread_count}\n\n"
        "Выберите нужную категорию переписок:",
        reply_markup=ikb.admin_support_menu_kb(unread_count)
    )
    await call.answer()


@router.callback_query(F.data.startswith("adm_sup:"))
async def cb_admin_threads_list(call: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin_callback(call, state):
        return
    if state is not None:
        await state.clear()
    try:
        _, mode, page_str = call.data.split(":")
        page = int(page_str)
    except (ValueError, IndexError):
        await call.answer("Некорректные параметры списка.", show_alert=True)
        return

    only_unread = (mode == "unread")
    threads = await db.get_support_threads(only_unread=only_unread)

    if not threads:
        msg = "Все сообщения обработаны!" if only_unread else "История диалогов пуста."
        await call.answer(msg, show_alert=True)
        return

    title = "🔴 **Неотвеченные сообщения гостей:**" if only_unread else "🗂 **Все диалоги с гостями студий:**"
    await _safe_admin_edit_md(
        call.message,
        f"{title}\n\nНажмите на карточку диалога для ответа:",
        reply_markup=ikb.admin_threads_list_kb(threads, mode=mode, page=page),
    )
    await call.answer()


async def open_client_chat_for_admin(target_message: Message, state: FSMContext, client_id: int):
    thread = await db.get_thread_info(client_id)
    if not thread:
        await state.clear()
        await target_message.answer("⚠️ Переписка с данным гостем не найдена в базе данных.")
        return

    await db.mark_thread_read(client_id)
    messages = await db.get_thread_messages(client_id, limit=12)

    branch_title = bs.get_branches().get(thread["branch_id"], {}).get("title", thread["branch_id"])
    user_tag = f" (@{thread['username']})" if thread.get("username") else ""
    phone = await db.get_user_phone(client_id)
    phone_line = f" · 📞 <code>{html.escape(phone)}</code>" if phone else ""

    history_lines = [
        f"💬 <b>Диалог: {html.escape(thread['client_name'] or 'Гость')}{html.escape(user_tag)}</b>",
        f"📍 <b>Студия:</b> Sahar&Vosk ({html.escape(branch_title)}) · ID: <code>{client_id}</code>{phone_line}",
        "──────────────"
    ]

    photos_to_duplicate = []

    if not messages:
        history_lines.append("<i>История сообщений пуста.</i>")
    else:
        msg_blocks = []
        for m in messages:
            time_short = m["created_at"][5:16] if m.get("created_at") else ""
            role = f"👤 {html.escape(thread['client_name'] or 'Гость')}" if m["sender"] == "client" else f"🛡 {html.escape(m['sender_name'] or 'Администратор')}"
            content = _clip_html_preview(m["text"] or "", max_escaped_len=240)
            if m.get("photo_id"):
                content = f"🖼 [Изображение] {content}"
                if m["sender"] == "client":
                    photos_to_duplicate.append((m["photo_id"], m["text"] or "", time_short))
            msg_blocks.append(f"<b>{role}</b> <i>({time_short})</i>:\n{content}\n")
        footer_reserve = 320
        while msg_blocks and len("\n".join(history_lines + msg_blocks)) + footer_reserve > 4096:
            msg_blocks.pop(0)
        history_lines.extend(msg_blocks)

    history_lines.append("──────────────")
    history_lines.append(
        "✨ <b>Прямое подключение к диалогу</b>\n"
        "Отправленные вами сообщения и фотографии поступают клиенту в личные сообщения бота.\n"
        "Для завершения нажмите <b>«🚪 Выйти из диалога»</b>."
    )

    await state.set_state(AdminState.chatting_with_client)
    await state.update_data(
        active_client_id=client_id,
        active_client_name=thread["client_name"] or "Гость",
        active_client_username=thread.get("username"),
        active_branch_id=thread["branch_id"]
    )

    await target_message.answer(
        "\n".join(history_lines),
        reply_markup=get_admin_chat_kb(),
        parse_mode="HTML"
    )

    for ph_id, ph_caption, ph_time in photos_to_duplicate[-3:]:
        try:
            cap = f"🖼 Изображение от клиента ({ph_time}):\n{ph_caption}" if ph_caption else f"🖼 Изображение от клиента ({ph_time})"
            await target_message.answer_photo(photo=ph_id, caption=cap[:1024])
        except Exception:
            pass

    await target_message.answer(
        "Управление диалогом:",
        reply_markup=ikb.admin_active_chat_inline_kb()
    )


@router.callback_query(F.data.startswith("adm_chat:"))
async def cb_admin_open_client_chat(call: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin_callback(call, state):
        return
    try:
        client_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer("Некорректный ID диалога.", show_alert=True)
        return
    await open_client_chat_for_admin(call.message, state, client_id)
    await call.answer()


@router.message(StateFilter(AdminState.chatting_with_client, None), F.chat.type == "private", F.text == "🚪 Выйти из диалога")
async def admin_exit_chat_button(message: Message, state: FSMContext):
    if not await check_admin(message.from_user.id):
        await state.clear()
        return
    await state.clear()
    await message.answer("✨ Вы вышли из режима диалога с гостем.", reply_markup=await _get_admin_main_kb(message.from_user.id))
    text, unread = await build_admin_dashboard_text()
    await message.answer(text, reply_markup=ikb.admin_main_kb(unread), parse_mode="Markdown")


@router.message(AdminState.chatting_with_client, F.text.in_(MAIN_MENU_BUTTONS))
async def admin_exit_on_menu_press(message: Message, state: FSMContext):
    await state.clear()
    if not await check_admin(message.from_user.id):
        return
    await message.answer(
        "ℹ️ Режим прямого диалога завершён. Нажмите выбранную кнопку меню повторно:",
        reply_markup=await _get_admin_main_kb(message.from_user.id)
    )


def _describe_admin_attachment(message: Message) -> tuple[str | None, str]:
    photo = getattr(message, "photo", None)
    if photo:
        return photo[-1].file_id, "[Изображение]"
    doc = getattr(message, "document", None)
    if doc:
        fname = getattr(doc, "file_name", None) or "файл"
        return None, f"[Документ: {fname}]"
    if getattr(message, "video", None):
        return None, "[Видео]"
    if getattr(message, "voice", None):
        return None, "[Голосовое сообщение]"
    if getattr(message, "audio", None):
        return None, "[Аудиозапись]"
    if getattr(message, "video_note", None):
        return None, "[Видеосообщение]"
    if getattr(message, "sticker", None):
        emoji = getattr(message.sticker, "emoji", "") or ""
        return None, f"[Стикер {emoji}]".strip()
    if getattr(message, "location", None):
        return None, "[Геометка]"
    if getattr(message, "contact", None):
        return None, "[Контакт]"
    return None, "[Медиафайл]"


@router.message(
    AdminState.chatting_with_client,
    F.photo
    | F.document
    | F.video
    | F.voice
    | F.audio
    | F.video_note
    | F.sticker
    | F.location
    | F.contact
    | (F.text & ~F.text.in_(MAIN_MENU_BUTTONS) & ~F.text.startswith("/"))
)
async def process_admin_chat_message(message: Message, state: FSMContext):
    if not await check_admin(message.from_user.id):
        await state.clear()
        return

    data = await state.get_data()
    client_id = data.get("active_client_id")
    if not client_id:
        await state.clear()
        await message.answer("⚠️ Диалог не выбран или сессия сброшена.", reply_markup=await _get_admin_main_kb(message.from_user.id))
        return
    client_name = data.get("active_client_name", "Гость")
    client_username = data.get("active_client_username")
    branch_id = data.get("active_branch_id", "prazhskaya")
    admin_name = message.from_user.first_name or "Администратор"

    photo_id, attachment_label = _describe_admin_attachment(message)
    caption_val = getattr(message, "caption", None)
    text_val = getattr(message, "text", None)
    text = (caption_val if caption_val else text_val) or ""

    try:
        await message.copy_to(chat_id=client_id)
    except TelegramForbiddenError:
        await message.answer("⚠️ Клиент заблокировал бота — сообщение не доставлено.")
        return
    except Exception as e:
        logging.warning(f"Ошибка доставки сообщения поддержки клиенту {client_id}: {e}")
        await message.answer("⚠️ Не удалось доставить сообщение клиенту (ошибка Telegram или чат недоступен).")
        return

    stored_text = text or attachment_label
    if photo_id is None and text and attachment_label != "[Медиафайл]" and not text_val:
        stored_text = f"{attachment_label} {text}"

    await db.save_support_message(
        telegram_id=client_id,
        client_name=client_name,
        username=client_username,
        branch_id=branch_id,
        sender="admin",
        sender_name=admin_name,
        text=stored_text,
        photo_id=photo_id
    )

    await send_to_archive_group(
        bot=message.bot,
        branch_id=branch_id,
        direction="from_admin",
        client_name=client_name,
        client_id=client_id,
        username=client_username,
        text=stored_text,
        photo_id=photo_id,
        admin_name=admin_name
    )


@router.callback_query(F.data == "adm:clear_cache")
async def cb_admin_clear_cache_ask(call: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin_callback(call, state):
        return
    await _safe_admin_edit_md(
        call.message,
        "🧹 **Очистка тестового кэша**\n"
        "──────────────\n"
        "Будут удалены тестовые записи, оценки и переписки внутри бота.\n"
        "База CRM YCLIENTS останется нетронутой.\n\n"
        "Подтверждаете действие?",
        reply_markup=ikb.admin_clear_cache_confirm_kb(),
    )
    await call.answer()


@router.callback_query(F.data == "adm_cache:confirm")
async def cb_admin_clear_cache_confirm(call: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin_callback(call, state):
        return
    scheduler.remove_all_jobs()
    await db.clear_test_cache()
    if state is not None:
        await state.clear()
    await call.answer("Тестовые данные очищены!", show_alert=True)

    text, unread = await build_admin_dashboard_text()
    await _safe_admin_edit_md(
        call.message,
        "✨ **Тестовые данные успешно очищены.**\n\n" + text,
        reply_markup=ikb.admin_main_kb(unread),
    )


# =====================================================================
# РЕДАКТОР ВСЕХ ШАГОВ, КАТЕГОРИЙ, ПОДКАТЕГОРИЙ И ПРАЙСА
# =====================================================================

@router.callback_query(F.data == "adm:steps")
async def cb_admin_steps_groups(call: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin_callback(call, state):
        return
    if state is not None:
        await state.clear()
    all_steps = await db.get_all_steps()

    counts: dict[str, tuple[int, int]] = {}
    for grp_key in ikb.STEP_GROUP_TITLES:
        grp_items = [s for s in all_steps if s.get("group") == grp_key]
        with_pic = sum(1 for s in grp_items if s.get("photo_id"))
        counts[grp_key] = (len(grp_items), with_pic)

    total_all = len(all_steps)
    total_pics = sum(1 for s in all_steps if s.get("photo_id"))

    await _safe_admin_edit_md(
        call.message,
        "🎨 **Редактор экранов, категорий и фото · Sahar&Vosk**\n"
        "──────────────\n"
        f"Всего экранов в боте: **{total_all}** *(с изображениями: {total_pics})*\n\n"
        "Выберите раздел, который хотите настроить:",
        reply_markup=ikb.admin_step_groups_kb(counts),
    )
    await call.answer()


@router.callback_query(F.data.startswith("adm_st_grp:"))
async def cb_admin_steps_in_group(call: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin_callback(call, state):
        return
    if state is not None:
        await state.clear()
    try:
        _, group, page_str = call.data.split(":")
        page = int(page_str)
    except (ValueError, IndexError):
        await call.answer("Некорректная страница.", show_alert=True)
        return

    steps = await db.get_steps_by_group(group)
    grp_title = ikb.STEP_GROUP_TITLES.get(group, "Экраны")

    await _safe_admin_edit_md(
        call.message,
        f"🎨 **{grp_title}**\n"
        "──────────────\n"
        "Значком 🖼 отмечены экраны, к которым уже прикреплено фото.\n"
        "Выберите экран для редактирования текста или загрузки фотографии:",
        reply_markup=ikb.admin_steps_list_kb(steps, group=group, page=page),
    )
    await call.answer()


@router.callback_query(F.data.startswith("adm_step:"))
async def cb_admin_step_card(call: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin_callback(call, state):
        return
    if state is not None:
        await state.clear()
    step_key = call.data.split(":", 1)[1]
    step = await db.get_step_content(step_key)
    group = db.get_step_group(step_key)

    photo_status = "✓ Прикреплено" if step.get("photo_id") else "✕ Отсутствует"
    if step_key.startswith("max_") and not step.get("photo_id"):
        photo_status = "— (MAX: только текст)"
    escaped_text = _clip_html_preview(step["custom_text"])
    escaped_title = html.escape(step["title"])

    text = (
        f"⚙️ <b>Настройка: {escaped_title}</b>\n"
        "──────────────\n"
        f"🖼 <b>Изображение:</b> {photo_status}\n\n"
        f"📝 <b>Текущий текст:</b>\n"
        f"<pre>{escaped_text}</pre>\n"
        "Выберите действие для редактирования:"
    )
    await _safe_admin_edit_html(
        call.message,
        text,
        reply_markup=ikb.admin_step_actions_kb(step_key, bool(step.get("photo_id")), group=group),
    )
    await call.answer()


@router.callback_query(F.data.startswith("adm_st_txt:"))
async def cb_admin_edit_step_text(call: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin_callback(call, state):
        return
    step_key = call.data.split(":", 1)[1]
    step = await db.get_step_content(step_key)

    if state is not None:
        await state.update_data(edit_step_key=step_key)
        await state.set_state(AdminState.editing_step_text)

    escaped_title = html.escape(step["title"])
    escaped_text = _clip_html_preview(step["custom_text"], max_escaped_len=2400)
    escaped_hints = html.escape(step["hints"] or "")

    text = (
        f"✏️ <b>Редактирование: «{escaped_title}»</b>\n"
        "──────────────\n"
        "Нажмите на блок ниже, чтобы скопировать текущий текст, внесите изменения и отправьте в ответ:\n\n"
        f"<code>{escaped_text}</code>\n\n"
        f"💡 <b>Доступные переменные:</b>\n<code>{escaped_hints}</code>"
    )
    await _safe_admin_edit_html(
        call.message,
        text,
        reply_markup=ikb.admin_edit_text_kb(step_key, step["custom_text"]),
    )
    await call.answer()


def _find_step_markup_warnings(step_key: str, raw_text: str) -> list[str]:
    warnings: list[str] = []
    without_vars = re.sub(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}", "", raw_text or "")
    if "{" in without_vars or "}" in without_vars:
        warnings.append("Обнаружены одиночные фигурные скобки `{` / `}` вне переменных шаблона.")

    if step_key.startswith("max_"):
        if "**" in raw_text:
            warnings.append("Для уведомлений MAX используется обычный текст без Markdown-звёздочек `**`.")
        return warnings

    cleaned = re.sub(r"\\[\\_*`]", "", raw_text or "")
    cleaned = re.sub(r"https?://\S+", "", cleaned)
    if cleaned.count("**") % 2 != 0:
        warnings.append("Незакрытое выделение `**` (жирный шрифт в Telegram).")
    single_stars = cleaned.replace("**", "")
    if single_stars.count("*") % 2 != 0:
        warnings.append("Незакрытый символ `*` (разметка Telegram Markdown может отобразиться сырой).")
    if cleaned.count("`") % 2 != 0:
        warnings.append("Незакрытый символ `` ` `` (моноширинный блок).")
    return warnings


@router.message(AdminState.editing_step_text, _ADMIN_MSG_FILTER)
async def process_edited_step_text(message: Message, state: FSMContext):
    if not await check_admin(message.from_user.id):
        await state.clear()
        return
    if not message.text:
        await message.answer("⚠️ Пожалуйста, отправьте текстовый вариант сообщения.")
        return

    data = await state.get_data()
    step_key = data.get("edit_step_key")
    if not step_key:
        await state.clear()
        await message.answer("🔄 Сессия редактирования истекла. Откройте /admin заново.")
        return

    new_text = message.text.strip()
    try:
        bot_user = await message.bot.get_me()
        prefix = f"@{bot_user.username}" if getattr(bot_user, "username", None) else ""
    except Exception:
        prefix = ""
    if prefix and new_text.startswith(prefix):
        new_text = new_text[len(prefix):].lstrip()

    if not new_text:
        await message.answer("⚠️ Текст экрана не может быть пустым. Отправьте текст сообщения заново.")
        return

    group = db.get_step_group(step_key)

    await db.update_step_text(step_key, new_text)
    await state.clear()

    step = await db.get_step_content(step_key)
    photo_status = "✓ Прикреплено" if step.get("photo_id") else "✕ Отсутствует"
    warnings = _find_step_markup_warnings(step_key, new_text)
    warn_html = ""
    if warnings:
        warn_html = "\n\n⚠️ <b>Предупреждения разметки:</b>\n" + "\n".join(
            f"• {html.escape(w)}" for w in warnings
        )

    await message.answer(
        db._clip_utf16(
            f"✓ <b>Текст «{html.escape(step['title'])}» обновлён!</b>\n"
            "──────────────\n"
            f"🖼 <b>Изображение:</b> {photo_status}\n\n"
            f"📝 <b>Новый текст:</b>\n<pre>{_clip_html_preview(step['custom_text'])}</pre>"
            f"{warn_html}",
            4096,
        ),
        reply_markup=ikb.admin_step_actions_kb(step_key, bool(step.get("photo_id")), group=group),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("adm_st_rst:"))
async def cb_admin_reset_step_text(call: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin_callback(call, state):
        return
    step_key = call.data.split(":", 1)[1]
    group = db.get_step_group(step_key)
    await db.reset_step_text(step_key)
    if state is not None:
        await state.clear()
    await call.answer("Восстановлен стандартный текст Sahar&Vosk!", show_alert=True)

    step = await db.get_step_content(step_key)
    photo_status = "✓ Прикреплено" if step.get("photo_id") else "✕ Отсутствует"
    await _safe_admin_edit_html(
        call.message,
        f"⚙️ <b>Настройка: {html.escape(step['title'])}</b>\n"
        "──────────────\n"
        f"🖼 <b>Изображение:</b> {photo_status}\n\n"
        f"📝 <b>Текущий текст:</b>\n<pre>{_clip_html_preview(step['custom_text'])}</pre>",
        reply_markup=ikb.admin_step_actions_kb(step_key, bool(step.get("photo_id")), group=group),
    )


@router.callback_query(F.data.startswith("adm_st_pic:"))
async def cb_admin_edit_step_photo(call: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin_callback(call, state):
        return
    step_key = call.data.split(":", 1)[1]
    step = await db.get_step_content(step_key)

    if state is not None:
        await state.update_data(edit_step_key=step_key)
        await state.set_state(AdminState.editing_step_photo)

    has_photo = bool(step.get("photo_id"))
    status_line = "К данному экрану **уже прикреплено изображение**." if has_photo else "На данном экране **нет изображения**."
    max_note = (
        "\n\nℹ️ *Примечание: уведомления MAX отправляются только текстом (без фото).*"
        if step_key.startswith("max_")
        else ""
    )

    await _safe_admin_edit_md(
        call.message,
        f"🖼 **Изображение для экрана: «{escape_md(step['title'])}»**\n"
        f"──────────────\n"
        f"{status_line}{max_note}\n\n"
        "Отправьте фотографию в чат, чтобы закрепить её за этим экраном:",
        reply_markup=ikb.admin_edit_photo_kb(step_key, has_photo),
    )
    await call.answer()


@router.message(AdminState.editing_step_photo, _ADMIN_MSG_FILTER)
async def process_edited_step_photo(message: Message, state: FSMContext):
    if not await check_admin(message.from_user.id):
        await state.clear()
        return
    if not getattr(message, "photo", None):
        if getattr(message, "document", None):
            await message.answer(
                "⚠️ Картинка отправлена как файл (документ без сжатия).\n"
                "Пожалуйста, отправьте её как обычную **фотографию** (со сжатием), чтобы бот мог прикрепить её к экрану.",
                parse_mode="Markdown",
            )
            return
        await message.answer("⚠️ Пожалуйста, отправьте изображение или нажмите «‹ Назад к карточке шага».")
        return

    data = await state.get_data()
    step_key = data.get("edit_step_key")
    if not step_key:
        await state.clear()
        await message.answer("🔄 Сессия редактирования истекла. Откройте /admin заново.")
        return

    photo_id = message.photo[-1].file_id
    group = db.get_step_group(step_key)

    await db.update_step_photo(step_key, photo_id)
    await state.clear()

    step = await db.get_step_content(step_key)
    max_warn = (
        "\n\nℹ️ <i>Примечание: для уведомлений MAX фото не отправляются (доставляется только текст).</i>"
        if step_key.startswith("max_")
        else ""
    )
    await message.answer(
        db._clip_utf16(
            f"✓ <b>Изображение для экрана «{html.escape(step['title'])}» сохранено!</b>\n"
            "──────────────\n"
            f"🖼 <b>Изображение:</b> ✓ Прикреплено\n\n"
            f"📝 <b>Текущий текст:</b>\n<pre>{_clip_html_preview(step['custom_text'])}</pre>"
            f"{max_warn}",
            4096,
        ),
        reply_markup=ikb.admin_step_actions_kb(step_key, True, group=group),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("adm_st_delpic:"))
async def cb_admin_delete_step_photo(call: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin_callback(call, state):
        return
    step_key = call.data.split(":", 1)[1]
    group = db.get_step_group(step_key)
    await db.update_step_photo(step_key, None)
    if state is not None:
        await state.clear()
    await call.answer("Изображение удалено!", show_alert=True)

    step = await db.get_step_content(step_key)
    await _safe_admin_edit_html(
        call.message,
        f"⚙️ <b>Настройка: {html.escape(step['title'])}</b>\n"
        "──────────────\n"
        f"🖼 <b>Изображение:</b> ✕ Отсутствует\n\n"
        f"📝 <b>Текущий текст:</b>\n<pre>{_clip_html_preview(step['custom_text'])}</pre>",
        reply_markup=ikb.admin_step_actions_kb(step_key, False, group=group),
    )


@router.callback_query(F.data.startswith("adm_st_prev:"))
async def cb_admin_preview_step(call: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin_callback(call, state):
        return
    if not call.message:
        await call.answer()
        return
    step_key = call.data.split(":", 1)[1]
    await call.answer("Загружаю предпросмотр...")
    today_fmt = db.now_msk().date().strftime("%d.%m.%Y")
    safe_name = escape_md(call.from_user.first_name or "Анастасия")
    await db.render_step(
        call.message,
        step_key,
        first_name=safe_name,
        branch_title="Пражская, 3",
        cat_title="Удаление волос",
        subcat_title="Шугаринг",
        service_title="Глубокое бикини",
        services_list="1. **Глубокое бикини** — 2200 ₽ (30 мин.)",
        count=1,
        total_price="2200 ₽",
        total_duration=30,
        price="2200 ₽",
        master_name="Анна",
        visit_date=today_fmt,
        formatted_date=today_fmt,
        visit_time="14:30",
        client_name=safe_name,
        phone="+7 (999) 123-45-67",
        appointment_id=101,
        status_badge="✓ Подтверждена",
        address="г. Кудрово, Пражская ул., 3",
        page=1,
        total_pages=2,
        days=28,
        stars="⭐️⭐️⭐️⭐️⭐️",
        reason="Специалист Анна больше не выполняет данный набор услуг в этой студии.",
        reason_text="была перенесена",
        upsell_block="💡 **Рекомендуем дополнить ваш визит:**\n*(быстрое добавление в 1 клик кнопками ниже)*\n\n",
        hint=""
    )


# =====================================================================
# АДМИНИСТРАТОРЫ, ЖУРНАЛ ЗАПИСЕЙ, ПОВТОРНЫЕ ВИЗИТЫ И РАССЫЛКИ
# =====================================================================

@router.callback_query(F.data == "adm:admins")
async def cb_admin_manage_admins(call: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin_callback(call, state):
        return
    if state is not None:
        await state.clear()
    text, kb = await build_admins_view(current_admin_id=call.from_user.id)
    await _safe_admin_edit_md(call.message, text, reply_markup=kb)
    await call.answer()


async def build_admins_view(current_admin_id: int | None = None):
    db_admins = await db.get_db_admins()
    lines = [
        "👥 **Управление доступом администраторов**\n"
        "──────────────\n"
    ]
    lines.append("👑 **Главные администраторы (.env):**")
    for env_id in ADMIN_IDS:
        lines.append(f"· ID: `{env_id}` *(защищенный доступ)*")

    lines.append("\n🛡 **Назначенные администраторы:**")
    extra_admins = [a for a in db_admins if a["telegram_id"] not in ADMIN_IDS]
    if not extra_admins:
        lines.append("· *Дополнительных администраторов нет*")
    else:
        for a in extra_admins:
            lines.append(f"· **{escape_md(a['name'])}** — ID: `{a['telegram_id']}` *(добавлен {a['added_at']})*")

    return "\n".join(lines), ikb.admin_manage_admins_kb(db_admins, ADMIN_IDS, current_admin_id=current_admin_id)


@router.callback_query(F.data == "adm_mgr:add")
async def cb_admin_start_add_admin(call: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin_callback(call, state):
        return
    if state is not None:
        await state.set_state(AdminState.adding_admin)
    await _safe_admin_edit_md(
        call.message,
        "＋ **Назначение администратора**\n"
        "──────────────\n"
        "Отправьте **Telegram ID** (например: `123456789 Мария`), перешлите сообщение от пользователя или отправьте контакт:",
        reply_markup=ikb.admin_cancel_input_kb("adm:admins"),
    )
    await call.answer()


@router.message(AdminState.adding_admin, _ADMIN_MSG_FILTER)
async def process_add_admin(message: Message, state: FSMContext):
    if not await check_admin(message.from_user.id):
        await state.clear()
        return

    new_admin_id = None
    new_admin_name = "Администратор"

    if message.forward_from:
        new_admin_id = message.forward_from.id
        new_admin_name = message.forward_from.first_name or "Администратор"
    elif message.contact and message.contact.user_id:
        new_admin_id = message.contact.user_id
        new_admin_name = message.contact.first_name or "Администратор"
    elif message.text:
        parts = message.text.strip().split(maxsplit=1)
        if parts[0].isdigit():
            cand_id = int(parts[0])
            if cand_id > 0:
                new_admin_id = cand_id
                new_admin_name = parts[1] if len(parts) > 1 else f"Администратор {new_admin_id}"

    if not new_admin_id or new_admin_id <= 0:
        await message.answer(
            "⚠️ Не удалось определить корректный положительный ID. Попросите пользователя отправить боту `/my_id` и скопируйте число сюда.",
            reply_markup=ikb.admin_cancel_input_kb("adm:admins"),
            parse_mode="Markdown"
        )
        return

    await db.add_db_admin(new_admin_id, new_admin_name)
    await state.clear()
    text, kb = await build_admins_view(current_admin_id=message.from_user.id)
    await message.answer(f"✓ **{escape_md(new_admin_name)}** (`{new_admin_id}`) успешно добавлен!\n\n{text}", reply_markup=kb, parse_mode="Markdown")


@router.callback_query(F.data.startswith("adm_mgr:del:"))
async def cb_admin_delete_admin(call: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin_callback(call, state):
        return
    try:
        target_id = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        await call.answer("Некорректный ID администратора.", show_alert=True)
        return
    if target_id in ADMIN_IDS:
        await call.answer("Главного администратора удалить нельзя!", show_alert=True)
        return
    if target_id == call.from_user.id:
        await call.answer("Нельзя удалить самого себя из администраторов!", show_alert=True)
        return
    all_admins = await db.get_all_admin_ids(ADMIN_IDS)
    if len([a for a in all_admins if a != target_id]) == 0:
        await call.answer("Нельзя удалить последнего администратора бота!", show_alert=True)
        return
    await db.remove_db_admin(target_id)
    await call.answer("Администратор удален")
    text, kb = await build_admins_view(current_admin_id=call.from_user.id)
    await _safe_admin_edit_md(call.message, text, reply_markup=kb)


@router.callback_query(F.data == "adm:apps")
async def cb_admin_apps_menu(call: CallbackQuery, state: FSMContext | None = None, answer_cb: bool = True):
    if not await _is_admin_callback(call, state):
        return
    await _safe_admin_edit_md(
        call.message,
        "📋 **Журнал активных записей · Sahar&Vosk**\n"
        "──────────────\n"
        "Выберите студию для просмотра предстоящих бронирований:",
        reply_markup=ikb.admin_branches_filter_kb(),
    )
    if answer_cb:
        await call.answer()


@router.callback_query(F.data.startswith("adm_apps:"))
async def cb_admin_show_apps(call: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin_callback(call, state):
        return
    parts = call.data.split(":")
    branch_id = parts[1] if len(parts) > 1 else "all"
    try:
        page = int(parts[2]) if len(parts) > 2 else 1
    except ValueError:
        page = 1
    page_size = 5

    apps = await db.get_all_active_appointments(branch_id)
    if not apps:
        await call.answer("Активных предстоящих бронирований нет.", show_alert=True)
        return

    total_pages = max(1, (len(apps) + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    current_apps = apps[start:start + page_size]

    lines = [
        f"✨ <b>Активные записи · Sahar&Vosk ({len(apps)} шт.)</b>",
        f"<i>Страница {page} из {total_pages}</i>",
        "──────────────\n"
    ]
    buttons = []

    for app in current_apps:
        formatted_date = date.fromisoformat(app["visit_date"]).strftime("%d.%m.%Y")
        conf_status = "✓ Подтверждён" if app.get("confirmed") else "⏳ Ожидает"
        price_val = int(app.get("price") or 0)
        price_str = f"{price_val} ₽" if price_val > 0 else "по прайсу"
        srv_short = app.get("service_title") or "Услуга"
        if len(srv_short) > 220:
            srv_short = srv_short[:217] + "..."
        client_label = str(app.get("client_name") or "Гость")
        lines.append(
            f"🔹 <b>Запись №{app['id']}</b> · <i>{conf_status}</i>\n"
            f"📍 Sahar&Vosk · {html.escape(app['branch_title'])}\n"
            f"👤 <b>{html.escape(client_label)}</b> (<code>{html.escape(app.get('phone') or app.get('client_phone') or '—')}</code>)\n"
            f"✨ {html.escape(srv_short)} ({price_str})\n"
            f"👩‍⚕️ Специалист: {html.escape(app.get('master_name') or 'Специалист')}\n"
            f"🗓 <b>{formatted_date} в {app['visit_time']}</b>\n"
        )
        btn_name = client_label[:24] + "…" if len(client_label) > 24 else client_label
        buttons.append([
            InlineKeyboardButton(
                text=f"✕ Отменить запись №{app['id']} ({btn_name})",
                callback_data=f"adm_cancel:{app['id']}"
            )
        ])

    if total_pages > 1:
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton(text="‹", callback_data=f"adm_apps:{branch_id}:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page} / {total_pages}", callback_data="ignore"))
        if page < total_pages:
            nav.append(InlineKeyboardButton(text="›", callback_data=f"adm_apps:{branch_id}:{page + 1}"))
        buttons.append(nav)

    buttons.append([InlineKeyboardButton(text="‹ К выбору студии", callback_data="adm:apps")])

    await _safe_admin_edit_html(
        call.message,
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await call.answer()


@router.callback_query(F.data.startswith("adm_cancel:"))
async def cb_admin_cancel_app(call: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin_callback(call, state):
        return
    try:
        app_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer("Запись не найдена.", show_alert=True)
        return
    app = await db.admin_cancel_appointment(app_id)
    if not app:
        await call.answer("Запись уже отменена или не найдена.", show_alert=True)
        return

    remove_appointment_reminders(app_id)
    try:
        yc_deleted = await yclients_api.delete_yclients_record(app["branch_id"], app.get("yclients_record_id"))
    except Exception:
        yc_deleted = False

    tg_id = app.get("telegram_id")
    if tg_id and int(tg_id) > 0:
        formatted_date = date.fromisoformat(app["visit_date"]).strftime("%d.%m.%Y")
        cancel_notice = (
            f"✨ **Уведомление от студии Sahar&Vosk**\n"
            f"──────────────\n"
            f"Ваш визит №{app_id} на {formatted_date} в {app['visit_time']} "
            f"({escape_md(app['service_title'])}) был отменён администратором студии.\n\n"
            "Будем рады подобрать для вас другое удобное время онлайн или по телефону."
        )
        try:
            await call.bot.send_message(chat_id=int(tg_id), text=cancel_notice, parse_mode="Markdown")
        except TelegramBadRequest:
            try:
                await call.bot.send_message(chat_id=int(tg_id), text=cancel_notice.replace("**", ""))
            except Exception as e:
                logging.warning(f"Не удалось доставить уведомление об отмене гостю {tg_id}: {e}")
        except Exception as e:
            logging.warning(f"Не удалось доставить уведомление об отмене гостю {tg_id}: {e}")

    if yc_deleted:
        await call.answer(f"Запись №{app_id} отменена в боте и в YCLIENTS.", show_alert=True)
    else:
        await call.answer(
            f"Запись №{app_id} отменена в боте, но ⚠️ не удалось удалить её в YCLIENTS — проверьте CRM вручную!",
            show_alert=True,
        )
    await cb_admin_apps_menu(call, state=state, answer_cb=False)


@router.callback_query(F.data == "adm:repeat_settings")
async def cb_admin_repeat_settings(call: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin_callback(call, state):
        return
    if state is not None:
        await state.clear()
    settings = await db.get_all_repeat_settings()
    await _safe_admin_edit_md(
        call.message,
        "⏳ **Интервалы приглашения на повторный визит**\n"
        "──────────────\n"
        "Выберите направление, чтобы скорректировать срок отправки уведомления:",
        reply_markup=ikb.admin_repeat_settings_kb(REPEAT_CATEGORIES, settings),
    )
    await call.answer()


@router.callback_query(F.data.startswith("adm_rep:"))
async def cb_admin_edit_repeat_cat(call: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin_callback(call, state):
        return
    cat_id = call.data.split(":")[1]
    cat_title = REPEAT_CATEGORIES.get(cat_id, cat_id)
    current_days = await db.get_repeat_days(cat_id)

    if state is not None:
        await state.update_data(edit_cat_id=cat_id)
        await state.set_state(AdminState.entering_repeat_days)
    await _safe_admin_edit_md(
        call.message,
        f"⏳ Направление: **{cat_title}**\n"
        f"Текущий интервал: **{current_days} дн.**\n\n"
        "Введите новое количество дней (целое число):",
        reply_markup=ikb.admin_cancel_input_kb("adm:repeat_settings"),
    )
    await call.answer()


@router.message(AdminState.entering_repeat_days, _ADMIN_MSG_FILTER)
async def process_new_repeat_days(message: Message, state: FSMContext):
    if not await check_admin(message.from_user.id):
        await state.clear()
        return
    if not message.text or not message.text.strip().isdigit():
        await message.answer("⚠️ Пожалуйста, введите целое число дней (от 1 до 365).")
        return

    days = int(message.text.strip())
    if days < 1 or days > 365:
        await message.answer("⚠️ Пожалуйста, укажите интервал от 1 до 365 дней.")
        return

    data = await state.get_data()
    cat_id = data.get("edit_cat_id")
    if not cat_id:
        await state.clear()
        await message.answer("🔄 Сессия редактирования истекла. Откройте /admin заново.")
        return
    cat_title = REPEAT_CATEGORIES.get(cat_id, cat_id)

    await db.set_repeat_days(cat_id, days)
    await state.clear()
    settings = await db.get_all_repeat_settings()
    await message.answer(
        f"✓ Для направления **{cat_title}** установлен интервал: **{days} дн.**",
        reply_markup=ikb.admin_repeat_settings_kb(REPEAT_CATEGORIES, settings),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "adm:broadcast")
async def cb_admin_broadcast_start(call: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin_callback(call, state):
        return
    if state is not None:
        await state.set_state(AdminState.choosing_segment)
    await _safe_admin_edit_md(
        call.message,
        "📢 **Создание рассылки · Sahar&Vosk**\n"
        "──────────────\n"
        "Выберите целевую аудиторию получателей:",
        reply_markup=ikb.admin_segments_kb(),
    )
    await call.answer()


@router.callback_query(AdminState.choosing_segment, F.data.startswith("seg:"))
async def cb_admin_select_segment(call: CallbackQuery, state: FSMContext):
    if not await _is_admin_callback(call, state):
        return
    segment = call.data.split(":", 1)[1]
    recipients = await db.get_users_by_segment(segment)
    seg_title = SEGMENT_TITLES.get(segment, segment)

    if not recipients:
        await call.answer(f"В сегменте «{seg_title}» нет получателей.", show_alert=True)
        return

    await state.update_data(segment=segment, recipients_count=len(recipients))
    await state.set_state(AdminState.entering_broadcast_content)
    await _safe_admin_edit_md(
        call.message,
        f"📢 **Создание рассылки**\n"
        f"🎯 **Сегмент:** {seg_title} ({len(recipients)} чел.)\n"
        "──────────────\n"
        "Отправьте текст сообщения или фотографию с подписью:",
        reply_markup=ikb.admin_cancel_input_kb("adm:broadcast"),
    )
    await call.answer()


@router.message(AdminState.entering_broadcast_content, _ADMIN_MSG_FILTER)
async def process_broadcast_content(message: Message, state: FSMContext):
    if not await check_admin(message.from_user.id):
        await state.clear()
        return
    photo = getattr(message, "photo", None)
    photo_id = photo[-1].file_id if photo else None
    text = (getattr(message, "caption", None) if photo else getattr(message, "text", None)) or ""
    if not photo_id and not text:
        if getattr(message, "document", None):
            await message.answer("⚠️ Для рассылки отправьте текст или обычное **фото** (со сжатием), а не файл.", parse_mode="Markdown")
            return
        await message.answer("⚠️ Отправьте текст или изображение с описанием.")
        return

    await state.update_data(bc_photo=photo_id, bc_text=text)
    await state.set_state(AdminState.confirming_broadcast)
    data = await state.get_data()

    await message.answer("👁 **Предпросмотр сообщения рассылки:**", parse_mode="Markdown")
    try:
        if photo_id:
            await message.answer_photo(
                photo=photo_id,
                caption=db._clip_utf16(text, 1024),
                reply_markup=ikb.repeat_booking_kb()
            )
            if db._utf16_len(text) > 1024:
                await message.answer(
                    "⚠️ **Внимание:** подпись к фото длиннее 1024 символов (UTF-16) — при отправке с фото Telegram обрежет её до лимита.\n"
                    "Для длинных текстов рекомендуем рассылку текстом без изображения.",
                    parse_mode="Markdown"
                )
        else:
            await message.answer(
                text=db._clip_utf16(text, 4096),
                reply_markup=ikb.repeat_booking_kb()
            )
    except TelegramBadRequest as e:
        await message.answer(f"⚠️ Не удалось сформировать предпросмотр ({e}). Попробуйте другой текст или фото.")
        return

    await message.answer(
        f"👥 Получателей: **{data.get('recipients_count', 0)} чел.** Начать отправку?",
        reply_markup=ikb.admin_broadcast_confirm_kb(),
        parse_mode="Markdown"
    )


@router.callback_query(AdminState.confirming_broadcast, F.data == "adm_bc:send")
async def cb_admin_send_broadcast(call: CallbackQuery, state: FSMContext):
    if not await _is_admin_callback(call, state):
        return
    admin_id = call.from_user.id
    if admin_id in _pending_broadcasts:
        await call.answer("⏳ Рассылка уже выполняется...", show_alert=False)
        return

    _pending_broadcasts.add(admin_id)
    try:
        data = await state.get_data()
        segment = data.get("segment")
        if not segment:
            await state.clear()
            await call.answer("🔄 Сессия рассылки устарела.", show_alert=True)
            return

        recipients = await db.get_users_by_segment(segment)
        photo_id = data.get("bc_photo")
        text = data.get("bc_text", "")
        cap_text = db._clip_utf16(text, 1024)
        msg_text = db._clip_utf16(text, 4096)

        await state.clear()
        await call.answer()
        try:
            await call.message.edit_text(f"⏳ Отправка рассылки ({len(recipients)} получателей)...")
        except Exception:
            pass

        sent, failed = 0, 0
        for uid in recipients:
            if not uid or int(uid) <= 0:
                continue
            try:
                if photo_id:
                    await call.bot.send_photo(chat_id=uid, photo=photo_id, caption=cap_text, reply_markup=ikb.repeat_booking_kb())
                else:
                    await call.bot.send_message(chat_id=uid, text=msg_text, reply_markup=ikb.repeat_booking_kb())
                sent += 1
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 0.1)
                try:
                    if photo_id:
                        await call.bot.send_photo(chat_id=uid, photo=photo_id, caption=cap_text, reply_markup=ikb.repeat_booking_kb())
                    else:
                        await call.bot.send_message(chat_id=uid, text=msg_text, reply_markup=ikb.repeat_booking_kb())
                    sent += 1
                except TelegramRetryAfter as e2:
                    await asyncio.sleep(e2.retry_after + 0.1)
                    failed += 1
                except Exception:
                    failed += 1
            except (TelegramForbiddenError, TelegramBadRequest):
                failed += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)

        _, unread = await build_admin_dashboard_text()
        try:
            await call.message.answer(
                f"✨ **Рассылка завершена!**\n"
                f"──────────────\n"
                f"Успешно доставлено: **{sent}** | Ошибок: **{failed}**",
                reply_markup=ikb.admin_main_kb(unread),
                parse_mode="Markdown"
            )
        except Exception as e:
            logging.warning(f"Не удалось отправить отчёт о завершении рассылки: {e}")
    finally:
        _pending_broadcasts.discard(admin_id)


@router.callback_query(F.data.startswith(("seg:", "adm_bc:")))
async def cb_admin_stale_broadcast_callbacks(call: CallbackQuery, state: FSMContext | None = None):
    if not await _is_admin_callback(call, state):
        return
    if state is not None:
        await state.clear()
    await call.answer("🔄 Сессия рассылки устарела. Откройте «Создать рассылку» заново.", show_alert=True)