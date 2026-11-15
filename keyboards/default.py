from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

MAIN_MENU_BUTTONS = {
    "💅 Записаться онлайн",
    "🔄 Повторить прошлый визит",
    "📋 Мои записи",
    "💰 Услуги и прайс",
    "🎁 Акции",
    "📍 Адреса и контакты",
    "💬 Связаться с администратором"
}

def get_main_keyboard(has_past_visit: bool = False) -> ReplyKeyboardMarkup:
    keyboard = []
    if has_past_visit:
        keyboard.append([KeyboardButton(text="🔄 Повторить прошлый визит")])
        keyboard.append([KeyboardButton(text="💅 Записаться онлайн")])
    else:
        keyboard.append([KeyboardButton(text="💅 Записаться онлайн")])
    keyboard.append([KeyboardButton(text="📋 Мои записи"), KeyboardButton(text="💰 Услуги и прайс")])
    keyboard.append([KeyboardButton(text="🎁 Акции"), KeyboardButton(text="📍 Адреса и контакты")])
    keyboard.append([KeyboardButton(text="💬 Связаться с администратором")])
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        input_field_placeholder="Выберите нужный раздел меню..."
    )

def get_phone_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(text="📱 Отправить мой номер телефона", request_contact=True)],
        [KeyboardButton(text="❌ Отменить запись")]
    ]
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        one_time_keyboard=True
    )

def get_client_support_kb() -> ReplyKeyboardMarkup:
    """Клавиатура клиента во время диалога с администратором."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Завершить диалог с администратором")]],
        resize_keyboard=True,
        input_field_placeholder="Напишите сообщение администратору..."
    )

def get_admin_chat_kb() -> ReplyKeyboardMarkup:
    """Клавиатура администратора во время ответа клиенту."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🚪 Выйти из диалога")]],
        resize_keyboard=True,
        input_field_placeholder="Напишите ответ клиенту (или нажмите Выйти)..."
    )