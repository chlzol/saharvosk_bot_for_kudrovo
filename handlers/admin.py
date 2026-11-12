import asyncio
import html
from datetime import date
from aiogram import Router, F, Bot
from aiogram.filters import Command, StateFilter
from aiogram.types import Message, CallbackQuery, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from config import ADMIN_IDS
from states.admin import AdminState
from database import db
from services import booking_service as bs
from services import yclients_api
from services.scheduler import scheduler, remove_appointment_reminders
from sync_yclients import run_yclients_sync
from keyboards.default import get_main_keyboard, get_admin_chat_kb, MAIN_MENU_BUTTONS
from keyboards import inline as ikb

router = Router()
_ADMIN_MSG_FILTER = (F.text == None) | (~F.text.in_(MAIN_MENU_BUTTONS) & ~F.text.startswith("/"))
_pending_broadcasts: set[int] = set()


def escape_md(value: str | None) -> str:
    if not value:
        return ""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("_", "\\_")
        .replace("*", "\\*")
        .replace("`", "\\`")
        .replace("[", "\\[")
    )


def _clip_html_preview(raw_text: str | None, max_escaped_len: int = 2800) -> str:
    txt = raw_text or ""
    if len(txt) <= max_escaped_len:
        escaped = html.escape(txt)
        if len(escaped) <= max_escaped_len:
            return escaped
    suffix = "\n... [текст сокращён для предпросмотра]"
    cut = min(len(txt), max_escaped_len)
    while cut > 0:
        escaped = html.escape(txt[:cut] + suffix)
        if len(escaped) <= max_escaped_len:
            return escaped
        cut -= 200
    return html.escape(suffix.strip())


async def _safe_admin_edit_md(message: Message, text: str, reply_markup=None):
    try:
        await message.edit_text(text[:4096], reply_markup=reply_markup, parse_mode="Markdown")
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        try:
            await message.edit_text(text[:4096], reply_markup=reply_markup)
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


async def notify_admins(bot: Bot, text: str, reply_markup=None, photo_id: str | None = None):
    all_admins = await db.get_all_admin_ids(ADMIN_IDS)
    for admin_id in all_admins:
        try:
            if photo_id:
                try:
                    await bot.send_photo(chat_id=admin_id, photo=photo_id, caption=text[:1024], reply_markup=reply_markup, parse_mode="Markdown")
                except TelegramBadRequest:
                    await bot.send_photo(chat_id=admin_id, photo=photo_id, caption=text[:1024], reply_markup=reply_markup)
            else:
                try:
                    await bot.send_message(chat_id=admin_id, text=text[:4096], reply_markup=reply_markup, parse_mode="Markdown")
                except TelegramBadRequest:
                    await bot.send_message(chat_id=admin_id, text=text[:4096], reply_markup=reply_markup)
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
            f"────────────────────\n"
            f"🆔 <code>{client_id}</code> · <b>{html.escape(client_name)}:</b> "
        )
    else:
        header_html = (
            f"📤 <b>АДМИНИСТРАТОР ({html.escape(admin_name or 'Администратор')}) ➡️ КЛИЕНТУ</b>\n"
            f"📍 Студия: Sahar&Vosk ({html.escape(branch_title)})\n"
            f"🔎 Профиль: #id{client_id}\n"
            f"👤 Кому: <b>{html.escape(client_name)}</b>{html.escape(user_tag)}{phone_str}\n"
            f"────────────────────\n"
            f"🆔 <code>{client_id}</code> · <b>Ответ:</b> "
        )

    max_len = 1024 if photo_id else 4096
    max_body_len = max(64, max_len - len(header_html))
    body_html = _clip_html_preview(raw_body, max_escaped_len=max_body_len)
    full_msg = (header_html + body_html)[:max_len]

    try:
        bot_me = await bot.get_me()
        kb = ikb.archive_jump_to_chat_kb(bot_me.username, client_id)
        if photo_id:
            await bot.send_photo(
                chat_id=group_id,
                photo=photo_id,
                caption=full_msg,
                reply_markup=kb,
                parse_mode="HTML"
            )
        else:
            await bot.send_message(
                chat_id=group_id,
                text=full_msg,
                reply_markup=kb,
                parse_mode="HTML"
            )
    except Exception:
        pass


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
        await message.answer(f"⛔ Доступ ограничен. Ваш ID: `{message.from_user.id}`.", parse_mode="Markdown")
        return

    await state.clear()
    text, unread = await build_admin_dashboard_text()
    await message.answer("✨ Панель управления открыта:", reply_markup=get_main_keyboard())
    await message.answer(text, reply_markup=ikb.admin_main_kb(unread), parse_mode="Markdown")


@router.callback_query(F.data == "adm:home")
async def cb_admin_home(call: CallbackQuery, state: FSMContext):
    if not await check_admin(call.from_user.id):
        return
    await state.clear()
    text, unread = await build_admin_dashboard_text()
    await _safe_admin_edit_md(call.message, text, reply_markup=ikb.admin_main_kb(unread))
    await call.answer()


async def build_admin_dashboard_text() -> tuple[str, int]:
    stats = await db.get_admin_stats()
    all_admins = await db.get_all_admin_ids(ADMIN_IDS)
    grp_pr = await db.get_archive_group_id("prazhskaya")
    grp_len = await db.get_archive_group_id("leningradskaya")

    text = (
        "✨ **Панель управления · Sahar&Vosk**\n"
        "────────────────────\n"
        f"👥 **Клиентов в базе:** {stats['total_users']}\n"
        f"💬 **Ожидают ответа:** {stats['unread_chats']}\n"
        f"🛡 **Администраторов:** {len(all_admins)}\n"
        f"📅 **Активных записей:** {stats['active_apps']}\n"
        f"  ├ 📍 Пражская, 3: {stats['prazhskaya_apps']} *(архив: {'✓' if grp_pr else '✕ не привязан'})*\n"
        f"  └ 📍 Ленинградская, 7: {stats['leningradskaya_apps']} *(архив: {'✓' if grp_len else '✕ не привязан'})*\n"
        "────────────────────\n"
        "Выберите действие для управления студией:"
    )
    return text, stats["unread_chats"]


@router.callback_query(F.data == "adm:sync")
async def cb_admin_sync_yclients(call: CallbackQuery):
    if not await check_admin(call.from_user.id):
        return

    await call.answer("Запускаю синхронизацию...")
    await call.message.edit_text(
        "🔄 **Синхронизация каталога Sahar&Vosk с YCLIENTS...**\n"
        "────────────────────\n"
        "Обновляю услуги, цены, реальную длительность и привязки специалистов по обоим филиалам.\n\n"
        "⏳ Пожалуйста, подождите несколько секунд…",
        parse_mode="Markdown"
    )

    try:
        sync_stats = await run_yclients_sync()
        dash_text, unread = await build_admin_dashboard_text()
        result_text = (
            "✨ **Синхронизация успешно завершена!**\n"
            "────────────────────\n"
            f"📍 **Пражская, 3:** {sync_stats['prazh_services']} услуг · {sync_stats['prazh_masters']} специалистов\n"
            f"📍 **Ленинградская, 7:** {sync_stats['len_services']} услуг · {sync_stats['len_masters']} специалистов\n"
            f"📂 **Всего в каталоге:** {sync_stats['total_services']} процедур и {sync_stats['total_masters']} специалистов.\n\n"
            "────────────────────\n" + dash_text
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


@router.callback_query(F.data == "adm:export_catalog")
async def cb_admin_export_catalog(call: CallbackQuery):
    if not await check_admin(call.from_user.id):
        return

    await call.answer("Формирую каталог...")
    report_text = bs.generate_catalog_report()
    file_bytes = report_text.encode("utf-8")
    doc = BufferedInputFile(file_bytes, filename="saharvosk_catalog.txt")

    await call.message.answer_document(
        document=doc,
        caption=(
            "📄 **Полный каталог процедур сети Sahar&Vosk**\n"
            "────────────────────\n"
            "Выгрузка содержит перечень всех услуг, цен, длительностей и привязанных специалистов по обеим студиям."
        ),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "adm:support")
async def cb_admin_support_menu(call: CallbackQuery, state: FSMContext):
    if not await check_admin(call.from_user.id):
        return
    await state.clear()
    unread_count = await db.get_unread_threads_count()
    await call.message.edit_text(
        "💬 **Центр сообщений и диалогов · Sahar&Vosk**\n"
        "────────────────────\n"
        f"🔴 **Ожидают ответа:** {unread_count}\n\n"
        "Выберите нужную категорию переписок:",
        reply_markup=ikb.admin_support_menu_kb(unread_count),
        parse_mode="Markdown"
    )
    await call.answer()


@router.callback_query(F.data.startswith("adm_sup:"))
async def cb_admin_threads_list(call: CallbackQuery, state: FSMContext):
    if not await check_admin(call.from_user.id):
        return
    await state.clear()
    _, mode, page_str = call.data.split(":")
    page = int(page_str)

    only_unread = (mode == "unread")
    threads = await db.get_support_threads(only_unread=only_unread)

    if not threads:
        msg = "Все сообщения обработаны!" if only_unread else "История диалогов пуста."
        await call.answer(msg, show_alert=True)
        return

    title = "🔴 **Неотвеченные сообщения гостей:**" if only_unread else "🗂 **Все диалоги с гостями студий:**"
    await call.message.edit_text(
        f"{title}\n\nНажмите на карточку диалога для ответа:",
        reply_markup=ikb.admin_threads_list_kb(threads, mode=mode, page=page),
        parse_mode="Markdown"
    )
    await call.answer()


async def open_client_chat_for_admin(target_message: Message, state: FSMContext, client_id: int):
    thread = await db.get_thread_info(client_id)
    if not thread:
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
        "────────────────────"
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

    history_lines.append("────────────────────")
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
async def cb_admin_open_client_chat(call: CallbackQuery, state: FSMContext):
    if not await check_admin(call.from_user.id):
        return
    client_id = int(call.data.split(":")[1])
    await open_client_chat_for_admin(call.message, state, client_id)
    await call.answer()


@router.message(StateFilter(AdminState.chatting_with_client, None), F.chat.type == "private", F.text == "🚪 Выйти из диалога")
async def admin_exit_chat_button(message: Message, state: FSMContext):
    if not await check_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer("✨ Вы вышли из режима диалога с гостем.", reply_markup=get_main_keyboard())
    text, unread = await build_admin_dashboard_text()
    await message.answer(text, reply_markup=ikb.admin_main_kb(unread), parse_mode="Markdown")


@router.message(AdminState.chatting_with_client, F.text.in_(MAIN_MENU_BUTTONS))
async def admin_exit_on_menu_press(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "ℹ️ Режим прямого диалога завершён. Нажмите выбранную кнопку меню повторно:",
        reply_markup=get_main_keyboard()
    )


@router.message(
    AdminState.chatting_with_client,
    F.photo | (F.text & ~F.text.startswith("/"))
)
async def process_admin_chat_message(message: Message, state: FSMContext):
    if not await check_admin(message.from_user.id):
        await state.clear()
        return

    data = await state.get_data()
    client_id = data.get("active_client_id")
    if not client_id:
        await state.clear()
        await message.answer("⚠️ Диалог не выбран или сессия сброшена.", reply_markup=get_main_keyboard())
        return
    client_name = data.get("active_client_name", "Гость")
    client_username = data.get("active_client_username")
    branch_id = data.get("active_branch_id", "prazhskaya")
    admin_name = message.from_user.first_name or "Администратор"

    photo_id = message.photo[-1].file_id if message.photo else None
    text = (message.caption if message.photo else message.text) or ""

    try:
        await message.copy_to(chat_id=client_id)
    except Exception:
        await message.answer("⚠️ Не удалось доставить сообщение (возможно, пользователь заблокировал бота).")
        return

    await db.save_support_message(
        telegram_id=client_id,
        client_name=client_name,
        username=client_username,
        branch_id=branch_id,
        sender="admin",
        sender_name=admin_name,
        text=text or "[Изображение]",
        photo_id=photo_id
    )

    await send_to_archive_group(
        bot=message.bot,
        branch_id=branch_id,
        direction="from_admin",
        client_name=client_name,
        client_id=client_id,
        username=client_username,
        text=text or "[Изображение]",
        photo_id=photo_id,
        admin_name=admin_name
    )


@router.callback_query(F.data == "adm:clear_cache")
async def cb_admin_clear_cache_ask(call: CallbackQuery):
    if not await check_admin(call.from_user.id):
        return
    await call.message.edit_text(
        "🧹 **Очистка тестового кэша**\n"
        "────────────────────\n"
        "Будут удалены тестовые записи, оценки и переписки внутри бота.\n"
        "База CRM YCLIENTS останется нетронутой.\n\n"
        "Подтверждаете действие?",
        reply_markup=ikb.admin_clear_cache_confirm_kb(),
        parse_mode="Markdown"
    )
    await call.answer()


@router.callback_query(F.data == "adm_cache:confirm")
async def cb_admin_clear_cache_confirm(call: CallbackQuery, state: FSMContext):
    if not await check_admin(call.from_user.id):
        return
    scheduler.remove_all_jobs()
    await db.clear_test_cache()
    await state.clear()
    await call.answer("Тестовые данные очищены!", show_alert=True)

    text, unread = await build_admin_dashboard_text()
    await call.message.edit_text(
        "✨ **Тестовые данные успешно очищены.**\n\n" + text,
        reply_markup=ikb.admin_main_kb(unread),
        parse_mode="Markdown"
    )


# =====================================================================
# РЕДАКТОР ВСЕХ ШАГОВ, КАТЕГОРИЙ, ПОДКАТЕГОРИЙ И ПРАЙСА
# =====================================================================

@router.callback_query(F.data == "adm:steps")
async def cb_admin_steps_groups(call: CallbackQuery, state: FSMContext):
    if not await check_admin(call.from_user.id):
        return
    await state.clear()
    all_steps = await db.get_all_steps()

    counts: dict[str, tuple[int, int]] = {}
    for grp_key in ikb.STEP_GROUP_TITLES:
        grp_items = [s for s in all_steps if s.get("group") == grp_key]
        with_pic = sum(1 for s in grp_items if s.get("photo_id"))
        counts[grp_key] = (len(grp_items), with_pic)

    total_all = len(all_steps)
    total_pics = sum(1 for s in all_steps if s.get("photo_id"))

    await call.message.edit_text(
        "🎨 **Редактор экранов, категорий и фото · Sahar&Vosk**\n"
        "────────────────────\n"
        f"Всего экранов в боте: **{total_all}** *(с изображениями: {total_pics})*\n\n"
        "Выберите раздел, который хотите настроить:",
        reply_markup=ikb.admin_step_groups_kb(counts),
        parse_mode="Markdown"
    )
    await call.answer()


@router.callback_query(F.data.startswith("adm_st_grp:"))
async def cb_admin_steps_in_group(call: CallbackQuery, state: FSMContext):
    if not await check_admin(call.from_user.id):
        return
    await state.clear()
    _, group, page_str = call.data.split(":")
    page = int(page_str)

    steps = await db.get_steps_by_group(group)
    grp_title = ikb.STEP_GROUP_TITLES.get(group, "Экраны")

    await call.message.edit_text(
        f"🎨 **{grp_title}**\n"
        "────────────────────\n"
        "Значком 🖼 отмечены экраны, к которым уже прикреплено фото.\n"
        "Выберите экран для редактирования текста или загрузки фотографии:",
        reply_markup=ikb.admin_steps_list_kb(steps, group=group, page=page),
        parse_mode="Markdown"
    )
    await call.answer()


@router.callback_query(F.data.startswith("adm_step:"))
async def cb_admin_step_card(call: CallbackQuery, state: FSMContext):
    if not await check_admin(call.from_user.id):
        return
    await state.clear()
    step_key = call.data.split(":", 1)[1]
    step = await db.get_step_content(step_key)
    group = db.get_step_group(step_key)

    photo_status = "✓ Прикреплено" if step.get("photo_id") else "✕ Отсутствует"
    escaped_text = _clip_html_preview(step["custom_text"])
    escaped_title = html.escape(step["title"])

    text = (
        f"⚙️ <b>Настройка: {escaped_title}</b>\n"
        "────────────────────\n"
        f"🖼 <b>Изображение:</b> {photo_status}\n\n"
        f"📝 <b>Текущий текст:</b>\n"
        f"<pre>{escaped_text}</pre>\n"
        "Выберите действие для редактирования:"
    )
    await call.message.edit_text(
        text,
        reply_markup=ikb.admin_step_actions_kb(step_key, bool(step.get("photo_id")), group=group),
        parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data.startswith("adm_st_txt:"))
async def cb_admin_edit_step_text(call: CallbackQuery, state: FSMContext):
    if not await check_admin(call.from_user.id):
        return
    step_key = call.data.split(":", 1)[1]
    step = await db.get_step_content(step_key)

    await state.update_data(edit_step_key=step_key)
    await state.set_state(AdminState.editing_step_text)

    escaped_title = html.escape(step["title"])
    escaped_text = _clip_html_preview(step["custom_text"], max_escaped_len=2400)
    escaped_hints = html.escape(step["hints"] or "")

    text = (
        f"✏️ <b>Редактирование: «{escaped_title}»</b>\n"
        "────────────────────\n"
        "Нажмите на блок ниже, чтобы скопировать текущий текст, внесите изменения и отправьте в ответ:\n\n"
        f"<code>{escaped_text}</code>\n\n"
        f"💡 <b>Доступные переменные:</b>\n<code>{escaped_hints}</code>"
    )
    await call.message.edit_text(
        text,
        reply_markup=ikb.admin_edit_text_kb(step_key, step["custom_text"]),
        parse_mode="HTML"
    )
    await call.answer()


@router.message(AdminState.editing_step_text, _ADMIN_MSG_FILTER)
async def process_edited_step_text(message: Message, state: FSMContext):
    if not await check_admin(message.from_user.id):
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
    bot_user = await message.bot.get_me()
    prefix = f"@{bot_user.username}"
    if new_text.startswith(prefix):
        new_text = new_text[len(prefix):].lstrip()

    group = db.get_step_group(step_key)

    await db.update_step_text(step_key, new_text)
    await state.clear()

    step = await db.get_step_content(step_key)
    photo_status = "✓ Прикреплено" if step.get("photo_id") else "✕ Отсутствует"
    await message.answer(
        f"✓ <b>Текст «{html.escape(step['title'])}» обновлён!</b>\n"
        "────────────────────\n"
        f"🖼 <b>Изображение:</b> {photo_status}\n\n"
        f"📝 <b>Новый текст:</b>\n<pre>{_clip_html_preview(step['custom_text'])}</pre>",
        reply_markup=ikb.admin_step_actions_kb(step_key, bool(step.get("photo_id")), group=group),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("adm_st_rst:"))
async def cb_admin_reset_step_text(call: CallbackQuery, state: FSMContext):
    if not await check_admin(call.from_user.id):
        return
    step_key = call.data.split(":", 1)[1]
    group = db.get_step_group(step_key)
    await db.reset_step_text(step_key)
    await state.clear()
    await call.answer("Восстановлен стандартный текст Sahar&Vosk!", show_alert=True)

    step = await db.get_step_content(step_key)
    photo_status = "✓ Прикреплено" if step.get("photo_id") else "✕ Отсутствует"
    await call.message.edit_text(
        f"⚙️ <b>Настройка: {html.escape(step['title'])}</b>\n"
        "────────────────────\n"
        f"🖼 <b>Изображение:</b> {photo_status}\n\n"
        f"📝 <b>Текущий текст:</b>\n<pre>{_clip_html_preview(step['custom_text'])}</pre>",
        reply_markup=ikb.admin_step_actions_kb(step_key, bool(step.get("photo_id")), group=group),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("adm_st_pic:"))
async def cb_admin_edit_step_photo(call: CallbackQuery, state: FSMContext):
    if not await check_admin(call.from_user.id):
        return
    step_key = call.data.split(":", 1)[1]
    step = await db.get_step_content(step_key)

    await state.update_data(edit_step_key=step_key)
    await state.set_state(AdminState.editing_step_photo)

    has_photo = bool(step.get("photo_id"))
    status_line = "К данному экрану **уже прикреплено изображение**." if has_photo else "На данном экране **нет изображения**."

    await call.message.edit_text(
        f"🖼 **Изображение для экрана: «{step['title']}»**\n"
        f"────────────────────\n"
        f"{status_line}\n\n"
        "Отправьте фотографию в чат, чтобы закрепить её за этим экраном:",
        reply_markup=ikb.admin_edit_photo_kb(step_key, has_photo),
        parse_mode="Markdown"
    )
    await call.answer()


@router.message(AdminState.editing_step_photo, _ADMIN_MSG_FILTER)
async def process_edited_step_photo(message: Message, state: FSMContext):
    if not await check_admin(message.from_user.id):
        return
    if not message.photo:
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
    await message.answer(
        f"✓ <b>Изображение для экрана «{html.escape(step['title'])}» сохранено!</b>\n"
        "────────────────────\n"
        f"🖼 <b>Изображение:</b> ✓ Прикреплено\n\n"
        f"📝 <b>Текущий текст:</b>\n<pre>{_clip_html_preview(step['custom_text'])}</pre>",
        reply_markup=ikb.admin_step_actions_kb(step_key, True, group=group),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("adm_st_delpic:"))
async def cb_admin_delete_step_photo(call: CallbackQuery, state: FSMContext):
    if not await check_admin(call.from_user.id):
        return
    step_key = call.data.split(":", 1)[1]
    group = db.get_step_group(step_key)
    await db.update_step_photo(step_key, None)
    await state.clear()
    await call.answer("Изображение удалено!", show_alert=True)

    step = await db.get_step_content(step_key)
    await call.message.edit_text(
        f"⚙️ <b>Настройка: {html.escape(step['title'])}</b>\n"
        "────────────────────\n"
        f"🖼 <b>Изображение:</b> ✕ Отсутствует\n\n"
        f"📝 <b>Текущий текст:</b>\n<pre>{html.escape(step['custom_text'])}</pre>",
        reply_markup=ikb.admin_step_actions_kb(step_key, False, group=group),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("adm_st_prev:"))
async def cb_admin_preview_step(call: CallbackQuery):
    if not await check_admin(call.from_user.id):
        return
    step_key = call.data.split(":", 1)[1]
    await call.answer("Загружаю предпросмотр...")
    await db.render_step(
        call.message,
        step_key,
        first_name=call.from_user.first_name or "Анастасия",
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
        formatted_date=db.now_msk().date().strftime("%d.%m.%Y"),
        visit_time="14:30",
        client_name=call.from_user.first_name or "Анастасия",
        phone="+7 (999) 123-45-67",
        appointment_id=101,
        status_badge="✓ Подтверждена",
        address="г. Кудрово, Пражская ул., 3",
        page=1,
        total_pages=2,
        days=28,
        stars="⭐️⭐️⭐️⭐️⭐️",
        hint=""
    )


# =====================================================================
# АДМИНИСТРАТОРЫ, ЖУРНАЛ ЗАПИСЕЙ, ПОВТОРНЫЕ ВИЗИТЫ И РАССЫЛКИ
# =====================================================================

@router.callback_query(F.data == "adm:admins")
async def cb_admin_manage_admins(call: CallbackQuery, state: FSMContext):
    if not await check_admin(call.from_user.id):
        return
    await state.clear()
    text, kb = await build_admins_view()
    await call.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    await call.answer()


async def build_admins_view():
    db_admins = await db.get_db_admins()
    lines = [
        "👥 **Управление доступом администраторов**\n"
        "────────────────────\n"
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

    return "\n".join(lines), ikb.admin_manage_admins_kb(db_admins, ADMIN_IDS)


@router.callback_query(F.data == "adm_mgr:add")
async def cb_admin_start_add_admin(call: CallbackQuery, state: FSMContext):
    if not await check_admin(call.from_user.id):
        return
    await state.set_state(AdminState.adding_admin)
    await call.message.edit_text(
        "＋ **Назначение администратора**\n"
        "────────────────────\n"
        "Отправьте **Telegram ID** (например: `123456789 Мария`), перешлите сообщение от пользователя или отправьте контакт:",
        reply_markup=ikb.admin_cancel_input_kb("adm:admins"),
        parse_mode="Markdown"
    )
    await call.answer()


@router.message(AdminState.adding_admin, _ADMIN_MSG_FILTER)
async def process_add_admin(message: Message, state: FSMContext):
    if not await check_admin(message.from_user.id):
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
            new_admin_id = int(parts[0])
            new_admin_name = parts[1] if len(parts) > 1 else f"Администратор {new_admin_id}"

    if not new_admin_id:
        await message.answer(
            "⚠️ Не удалось определить ID. Попросите пользователя отправить боту `/my_id` и скопируйте число сюда.",
            reply_markup=ikb.admin_cancel_input_kb("adm:admins"),
            parse_mode="Markdown"
        )
        return

    await db.add_db_admin(new_admin_id, new_admin_name)
    await state.clear()
    text, kb = await build_admins_view()
    await message.answer(f"✓ **{escape_md(new_admin_name)}** (`{new_admin_id}`) успешно добавлен!\n\n{text}", reply_markup=kb, parse_mode="Markdown")


@router.callback_query(F.data.startswith("adm_mgr:del:"))
async def cb_admin_delete_admin(call: CallbackQuery):
    if not await check_admin(call.from_user.id):
        return
    target_id = int(call.data.split(":")[2])
    if target_id in ADMIN_IDS:
        await call.answer("Главного администратора удалить нельзя!", show_alert=True)
        return
    await db.remove_db_admin(target_id)
    await call.answer("Администратор удален")
    text, kb = await build_admins_view()
    await call.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


@router.callback_query(F.data == "adm:apps")
async def cb_admin_apps_menu(call: CallbackQuery):
    if not await check_admin(call.from_user.id):
        return
    await call.message.edit_text(
        "📋 **Журнал активных записей · Sahar&Vosk**\n"
        "────────────────────\n"
        "Выберите студию для просмотра предстоящих бронирований:",
        reply_markup=ikb.admin_branches_filter_kb(),
        parse_mode="Markdown"
    )
    await call.answer()


@router.callback_query(F.data.startswith("adm_apps:"))
async def cb_admin_show_apps(call: CallbackQuery):
    if not await check_admin(call.from_user.id):
        return
    parts = call.data.split(":")
    branch_id = parts[1]
    page = int(parts[2]) if len(parts) > 2 else 1
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
        "────────────────────\n"
    ]
    buttons = []

    for app in current_apps:
        formatted_date = date.fromisoformat(app["visit_date"]).strftime("%d.%m.%Y")
        conf_status = "✓ Подтверждён" if app.get("confirmed") else "⏳ Ожидает"
        price_str = f"{app['price']} ₽" if int(app.get("price", 0)) > 0 else "по прайсу"
        srv_short = app["service_title"]
        if len(srv_short) > 220:
            srv_short = srv_short[:217] + "..."
        lines.append(
            f"🔹 <b>Запись №{app['id']}</b> · <i>{conf_status}</i>\n"
            f"📍 Sahar&Vosk · {html.escape(app['branch_title'])}\n"
            f"👤 <b>{html.escape(app['client_name'])}</b> (<code>{html.escape(app['phone'])}</code>)\n"
            f"✨ {html.escape(srv_short)} ({price_str})\n"
            f"👩‍⚕️ Специалист: {html.escape(app['master_name'])}\n"
            f"🗓 <b>{formatted_date} в {app['visit_time']}</b>\n"
        )
        buttons.append([
            InlineKeyboardButton(
                text=f"✕ Отменить запись №{app['id']} ({app['client_name']})",
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

    try:
        await call.message.edit_text(
            "\n".join(lines)[:4096],
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="HTML"
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise
    await call.answer()


@router.callback_query(F.data.startswith("adm_cancel:"))
async def cb_admin_cancel_app(call: CallbackQuery):
    if not await check_admin(call.from_user.id):
        return
    app_id = int(call.data.split(":")[1])
    app = await db.admin_cancel_appointment(app_id)
    if not app:
        await call.answer("Запись уже отменена или не найдена.", show_alert=True)
        return

    remove_appointment_reminders(app_id)
    await yclients_api.delete_yclients_record(app["branch_id"], app.get("yclients_record_id"))

    formatted_date = date.fromisoformat(app["visit_date"]).strftime("%d.%m.%Y")
    cancel_notice = (
        f"✨ **Уведомление от студии Sahar&Vosk**\n"
        f"────────────────────\n"
        f"Ваш визит №{app_id} на {formatted_date} в {app['visit_time']} "
        f"({escape_md(app['service_title'])}) был отменён администратором студии.\n\n"
        "Будем рады подобрать для вас другое удобное время онлайн или по телефону."
    )
    try:
        await call.bot.send_message(chat_id=app["telegram_id"], text=cancel_notice, parse_mode="Markdown")
    except TelegramBadRequest:
        try:
            await call.bot.send_message(chat_id=app["telegram_id"], text=cancel_notice.replace("**", ""))
        except Exception:
            pass
    except Exception:
        pass

    await call.answer(f"Запись №{app_id} отменена в боте и в YCLIENTS.", show_alert=True)
    await cb_admin_apps_menu(call)


@router.callback_query(F.data == "adm:repeat_settings")
async def cb_admin_repeat_settings(call: CallbackQuery, state: FSMContext):
    if not await check_admin(call.from_user.id):
        return
    await state.clear()
    settings = await db.get_all_repeat_settings()
    await call.message.edit_text(
        "⏳ **Интервалы приглашения на повторный визит**\n"
        "────────────────────\n"
        "Выберите направление, чтобы скорректировать срок отправки уведомления:",
        reply_markup=ikb.admin_repeat_settings_kb(REPEAT_CATEGORIES, settings),
        parse_mode="Markdown"
    )
    await call.answer()


@router.callback_query(F.data.startswith("adm_rep:"))
async def cb_admin_edit_repeat_cat(call: CallbackQuery, state: FSMContext):
    if not await check_admin(call.from_user.id):
        return
    cat_id = call.data.split(":")[1]
    cat_title = REPEAT_CATEGORIES.get(cat_id, cat_id)
    current_days = await db.get_repeat_days(cat_id)

    await state.update_data(edit_cat_id=cat_id)
    await state.set_state(AdminState.entering_repeat_days)
    await call.message.edit_text(
        f"⏳ Направление: **{cat_title}**\n"
        f"Текущий интервал: **{current_days} дн.**\n\n"
        "Введите новое количество дней (целое число):",
        reply_markup=ikb.admin_cancel_input_kb("adm:repeat_settings"),
        parse_mode="Markdown"
    )
    await call.answer()


@router.message(AdminState.entering_repeat_days, _ADMIN_MSG_FILTER)
async def process_new_repeat_days(message: Message, state: FSMContext):
    if not await check_admin(message.from_user.id):
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
async def cb_admin_broadcast_start(call: CallbackQuery, state: FSMContext):
    if not await check_admin(call.from_user.id):
        return
    await state.set_state(AdminState.choosing_segment)
    await call.message.edit_text(
        "📢 **Создание рассылки · Sahar&Vosk**\n"
        "────────────────────\n"
        "Выберите целевую аудиторию получателей:",
        reply_markup=ikb.admin_segments_kb(),
        parse_mode="Markdown"
    )
    await call.answer()


@router.callback_query(AdminState.choosing_segment, F.data.startswith("seg:"))
async def cb_admin_select_segment(call: CallbackQuery, state: FSMContext):
    segment = call.data.split(":", 1)[1]
    recipients = await db.get_users_by_segment(segment)
    seg_title = SEGMENT_TITLES.get(segment, segment)

    if not recipients:
        await call.answer(f"В сегменте «{seg_title}» нет получателей.", show_alert=True)
        return

    await state.update_data(segment=segment, recipients_count=len(recipients))
    await state.set_state(AdminState.entering_broadcast_content)
    await call.message.edit_text(
        f"📢 **Создание рассылки**\n"
        f"🎯 **Сегмент:** {seg_title} ({len(recipients)} чел.)\n"
        "────────────────────\n"
        "Отправьте текст сообщения или фотографию с подписью:",
        reply_markup=ikb.admin_cancel_input_kb("adm:broadcast"),
        parse_mode="Markdown"
    )
    await call.answer()


@router.message(AdminState.entering_broadcast_content, _ADMIN_MSG_FILTER)
async def process_broadcast_content(message: Message, state: FSMContext):
    if not await check_admin(message.from_user.id):
        return
    photo_id = message.photo[-1].file_id if message.photo else None
    text = (message.caption if message.photo else message.text) or ""
    if not photo_id and not text:
        await message.answer("⚠️ Отправьте текст или изображение с описанием.")
        return

    await state.update_data(bc_photo=photo_id, bc_text=text)
    await state.set_state(AdminState.confirming_broadcast)
    data = await state.get_data()

    await message.answer("👁 **Предпросмотр сообщения рассылки:**", parse_mode="Markdown")
    if photo_id:
        await message.answer_photo(photo=photo_id, caption=text[:1024], reply_markup=ikb.repeat_booking_kb())
    else:
        await message.answer(text=text[:4096], reply_markup=ikb.repeat_booking_kb())

    await message.answer(
        f"👥 Получателей: **{data['recipients_count']} чел.** Начать отправку?",
        reply_markup=ikb.admin_broadcast_confirm_kb(),
        parse_mode="Markdown"
    )


@router.callback_query(AdminState.confirming_broadcast, F.data == "adm_bc:send")
async def cb_admin_send_broadcast(call: CallbackQuery, state: FSMContext):
    if not await check_admin(call.from_user.id):
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

        await state.clear()
        await call.answer()
        try:
            await call.message.edit_text(f"⏳ Отправка рассылки ({len(recipients)} получателей)...")
        except Exception:
            pass

        sent, failed = 0, 0
        for uid in recipients:
            try:
                if photo_id:
                    await call.bot.send_photo(chat_id=uid, photo=photo_id, caption=text[:1024], reply_markup=ikb.repeat_booking_kb())
                else:
                    await call.bot.send_message(chat_id=uid, text=text[:4096], reply_markup=ikb.repeat_booking_kb())
                sent += 1
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
                try:
                    if photo_id:
                        await call.bot.send_photo(chat_id=uid, photo=photo_id, caption=text[:1024], reply_markup=ikb.repeat_booking_kb())
                    else:
                        await call.bot.send_message(chat_id=uid, text=text[:4096], reply_markup=ikb.repeat_booking_kb())
                    sent += 1
                except Exception:
                    failed += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)

        _, unread = await build_admin_dashboard_text()
        await call.message.answer(
            f"✨ **Рассылка завершена!**\n"
            f"────────────────────\n"
            f"Успешно доставлено: **{sent}** | Ошибок: **{failed}**",
            reply_markup=ikb.admin_main_kb(unread),
            parse_mode="Markdown"
        )
    finally:
        _pending_broadcasts.discard(admin_id)