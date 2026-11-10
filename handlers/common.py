from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from datetime import date

from states.booking import SupportState
from keyboards.default import get_main_keyboard, get_client_support_kb, MAIN_MENU_BUTTONS
from keyboards import inline as ikb
from services import booking_service as bs
from handlers.admin import (
    check_admin,
    notify_admins,
    send_to_archive_group,
    open_client_chat_for_admin
)
from database import db

router = Router()


async def ensure_user_consent(message: Message) -> bool:
    await db.register_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name
    )
    if await db.has_user_agreed(message.from_user.id):
        return True

    await db.render_step(
        message,
        "consent",
        reply_markup=ikb.consent_kb(),
        first_name=message.from_user.first_name or "Гость"
    )
    return False


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await db.register_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name
    )

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].startswith("chat_"):
        if await check_admin(message.from_user.id):
            client_id_str = parts[1].replace("chat_", "")
            if client_id_str.isdigit():
                await open_client_chat_for_admin(message, state, int(client_id_str))
                return

    if not await db.has_user_agreed(message.from_user.id):
        await message.answer(
            "✨ **Sahar&Vosk**",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="Markdown"
        )
        await db.render_step(
            message,
            "consent",
            reply_markup=ikb.consent_kb(),
            first_name=message.from_user.first_name or "Гость"
        )
        return

    real_name = await db.get_user_real_name(message.from_user.id)
    display_name = real_name or message.from_user.first_name or "Гость"

    await db.render_step(
        message,
        "start",
        reply_markup=get_main_keyboard(),
        first_name=display_name
    )


@router.callback_query(F.data == "consent:accept")
async def cb_accept_consent(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await db.register_user(
        telegram_id=call.from_user.id,
        username=call.from_user.username,
        first_name=call.from_user.first_name
    )
    await db.set_user_agreed(call.from_user.id)
    await call.answer("Добро пожаловать в Sahar&Vosk ✨")

    try:
        await call.message.delete()
    except Exception:
        pass

    real_name = await db.get_user_real_name(call.from_user.id)
    display_name = real_name or call.from_user.first_name or "Гость"

    await db.render_step(
        call.message,
        "start",
        reply_markup=get_main_keyboard(),
        first_name=display_name
    )


@router.message(F.text == "📍 Адреса и контакты")
async def show_contacts(message: Message, state: FSMContext):
    if not await ensure_user_consent(message):
        return
    await state.clear()
    await db.render_step(
        message,
        "contacts",
        reply_markup=ikb.contacts_routes_kb()
    )


@router.callback_query(F.data.startswith("geo:"))
async def cb_send_branch_geolocation(call: CallbackQuery):
    branch_id = call.data.split(":")[1]
    branch = bs.get_branches().get(branch_id)
    if not branch:
        await call.answer("Студия не найдена.", show_alert=True)
        return

    await call.answer("Отправляю геометку студии...")
    await call.message.answer_location(
        latitude=branch["lat"],
        longitude=branch["lon"]
    )
    map_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗺 Построить маршрут в Яндекс.Картах", url=branch["yandex_map_url"])]
    ])
    await call.message.answer(
        f"✨ **Sahar&Vosk · {branch['title']}**\n"
        f"────────────────────\n"
        f"📍 Адрес: {branch['address']}\n"
        f"🕒 Ежедневно: 10:00 – 22:00\n"
        f"📞 Телефон: `{branch['phone']}`",
        reply_markup=map_kb,
        parse_mode="Markdown"
    )


@router.callback_query(F.data.startswith("show_memo:"))
async def cb_show_prep_memo(call: CallbackQuery):
    parts = call.data.split(":")
    memo_key = parts[1]
    await call.answer()

    apps = await db.get_user_active_appointments(call.from_user.id)
    if apps:
        app = apps[0]
        formatted_date = date.fromisoformat(app["visit_date"]).strftime("%d.%m.%Y")
        await db.render_step(
            call.message,
            memo_key,
            service_title=app["service_title"],
            formatted_date=formatted_date,
            visit_time=app["visit_time"],
            branch_title=app["branch_title"],
            master_name=app["master_name"]
        )
    else:
        await db.render_step(
            call.message,
            memo_key,
            service_title="Выбранная процедура",
            formatted_date="день визита",
            visit_time="назначенное время",
            branch_title="Sahar&Vosk",
            master_name="Специалист студии"
        )


@router.message(F.text == "🎁 Акции")
async def show_promotions(message: Message, state: FSMContext):
    if not await ensure_user_consent(message):
        return
    await state.clear()
    await db.render_step(message, "promotions")


@router.message(F.text == "💬 Связаться с администратором")
async def contact_admin(message: Message, state: FSMContext):
    if not await ensure_user_consent(message):
        return
    await state.clear()
    await state.set_state(SupportState.choosing_branch)
    await db.render_step(
        message,
        "admin_contact",
        reply_markup=ikb.support_branches_kb(bs.get_branches())
    )


@router.callback_query(F.data.startswith("sup_br:"))
async def select_support_branch(call: CallbackQuery, state: FSMContext):
    branch_id = call.data.split(":")[1]
    branch_title = bs.get_branches().get(branch_id, {}).get("title", branch_id)

    await state.set_state(SupportState.chatting)
    await state.update_data(support_branch_id=branch_id)

    await db.render_step(
        call.message,
        "support_chat_start",
        reply_markup=get_client_support_kb(),
        branch_title=branch_title
    )
    await call.answer()


@router.message(SupportState.chatting, F.text == "❌ Завершить диалог с администратором")
async def exit_client_support(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Вы вернулись в главное меню **Sahar&Vosk**. Ответ администратора поступит прямо в этот чат.",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )


async def forward_client_message_to_admins_and_archive(message: Message, branch_id: str):
    photo_id = message.photo[-1].file_id if message.photo else None
    text = (message.caption if message.photo else message.text) or ""

    real_name = await db.get_user_real_name(message.from_user.id)
    client_name = real_name or message.from_user.first_name or "Клиент"
    username = message.from_user.username

    actual_branch_id = await db.save_support_message(
        telegram_id=message.from_user.id,
        client_name=client_name,
        username=username,
        branch_id=branch_id,
        sender="client",
        sender_name=client_name,
        text=text,
        photo_id=photo_id
    )
    branch_title = bs.get_branches().get(actual_branch_id, {}).get("title", actual_branch_id)

    await send_to_archive_group(
        bot=message.bot,
        branch_id=actual_branch_id,
        direction="from_client",
        client_name=client_name,
        client_id=message.from_user.id,
        username=username,
        text=text,
        photo_id=photo_id
    )

    user_link = f" (@{username})" if username else ""
    admin_alert_text = (
        f"📩 **Новое обращение · Sahar&Vosk**\n"
        f"────────────────────\n"
        f"📍 **Студия:** {branch_title}\n"
        f"👤 **Клиент:** {client_name}{user_link} (`{message.from_user.id}`)\n\n"
        f"💬 {text or '[Изображение]'}"
    )
    await notify_admins(
        message.bot,
        admin_alert_text,
        reply_markup=ikb.admin_quick_reply_kb(message.from_user.id),
        photo_id=photo_id
    )


@router.message(
    SupportState.chatting,
    F.photo | (F.text & ~F.text.in_(MAIN_MENU_BUTTONS) & ~F.text.startswith("/"))
)
async def process_client_support_message(message: Message, state: FSMContext):
    data = await state.get_data()
    branch_id = data.get("support_branch_id", "prazhskaya")
    await forward_client_message_to_admins_and_archive(message, branch_id)


@router.message(
    StateFilter(None),
    F.photo | (F.text & ~F.text.in_(MAIN_MENU_BUTTONS) & ~F.text.startswith("/"))
)
async def process_direct_client_reply(message: Message):
    if not await ensure_user_consent(message):
        return
    thread = await db.get_thread_info(message.from_user.id)
    branch_id = thread["branch_id"] if thread else "prazhskaya"
    await forward_client_message_to_admins_and_archive(message, branch_id)