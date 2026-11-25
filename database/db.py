import re
import aiosqlite
from contextlib import asynccontextmanager
from datetime import datetime, date, timedelta, timezone
from aiogram.types import Message, CallbackQuery
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from config import ARCHIVE_GROUPS_ENV
from services import booking_service as bs

DB_NAME = "saharvosk.db"
MSK_TZ = timezone(timedelta(hours=3))


def now_msk() -> datetime:
    return datetime.now(MSK_TZ).replace(tzinfo=None)


_THIN_DIVIDER_TARGET_LEN = 14
_WIDE_DIVIDER_TARGET_LEN = 12
_DIVIDER_NORMALIZE_RE = re.compile(
    rf"([─━═])\1{{{_THIN_DIVIDER_TARGET_LEN},}}|([—➖])\2{{{_WIDE_DIVIDER_TARGET_LEN},}}"
)


def normalize_message_dividers(text: str | None) -> str:
    """Сжимает слишком длинные последовательности декоративных разделителей до мобильной ширины."""
    if not text:
        return text or ""

    def _shrink(match: re.Match) -> str:
        if match.group(1):
            return match.group(1) * _THIN_DIVIDER_TARGET_LEN
        return match.group(2) * _WIDE_DIVIDER_TARGET_LEN

    return _DIVIDER_NORMALIZE_RE.sub(_shrink, text)


def normalize_client_phone(raw: str | None) -> str:
    """
    Приводит телефон к международному формату +7XXXXXXXXXX (из 8999..., 7999...,
    +7 (999) ..., 999...). Для нероссийских международных номеров с '+' и длиной
    11–15 цифр сохраняет '+<digits>'. Возвращает '' для некорректного номера.
    """
    if not raw:
        return ""
    raw_str = str(raw).strip()
    digits = re.sub(r"\D", "", raw_str)
    if len(digits) < 10:
        return ""
    if len(digits) == 10:
        return f"+7{digits}"
    if len(digits) == 11 and digits[0] in ("7", "8"):
        return f"+7{digits[1:]}"
    if raw_str.startswith("+") and 11 <= len(digits) <= 15:
        return f"+{digits}"
    return f"+7{digits[-10:]}"


DEFAULT_REPEAT_DAYS = {
    "hair_removal": 28,
    "manicure": 21,
    "pedicure": 28,
    "brows": 28,
    "lashes": 28,
    "massage": 14,
    "cosmetology": 28,
    "men": 28
}

BASE_STEPS = {
    # --- ГРУППА 1: ГЛАВНОЕ МЕНЮ, МОИ ЗАПИСИ И ПРАЙС-ЛИСТ ---
    "consent": {
        "group": "menu",
        "title": "📝 Согласие на обработку данных",
        "text": (
            "✨ **Sahar&Vosk · Персональный сервис**\n"
            "──────────────\n"
            "Здравствуйте, {first_name}!\n\n"
            "Добро пожаловать в цифровое пространство сети студий красоты **Sahar&Vosk**.\n\n"
            "Для продолжения работы и бронирования визитов, пожалуйста, подтвердите согласие "
            "на обработку персональных данных в соответствии с ФЗ №152."
        ),
        "hints": "{first_name} — имя клиента"
    },
    "start": {
        "group": "menu",
        "title": "🏠 Приветствие (/start)",
        "text": (
            "✨ **Sahar&Vosk · Кудрово**\n"
            "──────────────\n"
            "Рады видеть вас, {first_name}!\n\n"
            "Добро пожаловать в студию эстетики тела и красоты **Sahar&Vosk**.\n\n"
            "📍 **Наши локации:**\n"
            "· Пражская ул., 3\n"
            "· Ленинградская ул., 7\n\n"
            "Выберите нужный раздел в меню ниже, чтобы забронировать визит, "
            "ознакомиться с прайс-листом или связаться с администратором."
        ),
        "hints": "{first_name} — имя клиента"
    },
    "contacts": {
        "group": "menu",
        "title": "📍 Адреса и контакты",
        "text": (
            "📍 **Локации Sahar&Vosk в Кудрово**\n"
            "──────────────\n\n"
            "✨ **Sahar&Vosk · Пражская, 3**\n"
            "Адрес: г. Кудрово, Пражская ул., 3\n"
            "Телефон: `+7 (812) 988-98-53`\n\n"
            "✨ **Sahar&Vosk · Ленинградская, 7**\n"
            "Адрес: г. Кудрово, Ленинградская ул., 7\n"
            "Телефон: `+7 (812) 988-98-53`\n\n"
            "🕒 **Часы работы:**\n"
            "Ежедневно · 10:00 – 22:00\n\n"
            "🌐 Сайт: [saharvosk.ru/kudrovo](https://saharvosk.ru/kudrovo)\n\n"
            "Выберите филиал ниже, чтобы получить геометку или построить маршрут 👇"
        ),
        "hints": "Переменных нет"
    },
    "promotions": {
        "group": "menu",
        "title": "🎁 Акции и привилегии",
        "text": (
            "🎁 **Специальные предложения Sahar&Vosk**\n"
            "──────────────\n\n"
            "· **Привилегия первого визита** — специальные условия для новых гостей студии\n"
            "· **Лазерная SHR-эпиляция -50%** — скидка на любую зону или комплекс при первом посещении\n"
            "· **Комплексные программы** — выгодные сеты депиляции, косметологии и ухода\n\n"
            "Ознакомиться со всеми предложениями и записаться можно через кнопку **«💅 Записаться онлайн»**."
        ),
        "hints": "Переменных нет"
    },
    "admin_contact": {
        "group": "menu",
        "title": "💬 Связь с администратором (выбор филиала)",
        "text": (
            "💬 **Консьерж-сервис Sahar&Vosk**\n"
            "──────────────\n\n"
            "Выберите интересующий вас филиал ниже, чтобы начать прямой диалог с администратором студии 👇\n\n"
            "📞 Единая линия: `+7 (812) 988-98-53`\n"
            "🕒 Ежедневно с 10:00 до 22:00"
        ),
        "hints": "Переменных нет"
    },
    "support_chat_start": {
        "group": "menu",
        "title": "💬 Начало диалога с администратором",
        "text": (
            "💬 **Sahar&Vosk · {branch_title}**\n"
            "──────────────\n"
            "Напишите ваш вопрос или отправьте изображение — сообщение сразу поступит администратору студии.\n\n"
            "Для возврата в меню нажмите кнопку **«❌ Завершить диалог с администратором»** внизу экрана."
        ),
        "hints": "{branch_title} — название филиала"
    },
    "my_apps_empty": {
        "group": "menu",
        "title": "📋 Мои записи (когда список пуст)",
        "text": (
            "✨ **Ваши визиты · Sahar&Vosk**\n"
            "──────────────\n"
            "У вас пока нет активных предстоящих бронирований.\n\n"
            "Нажмите **«💅 Записаться онлайн»**, чтобы выбрать удобное время."
        ),
        "hints": "Переменных нет"
    },
    "my_apps_card": {
        "group": "menu",
        "title": "📋 Мои записи (карточка активной записи)",
        "text": (
            "✨ **Визит №{appointment_id}** · *{status_badge}*\n"
            "──────────────\n"
            "📍 **Студия:** Sahar&Vosk · {branch_title}\n"
            "✨ **Услуги:** {service_title}\n"
            "👩‍⚕️ **Специалист:** {master_name}\n"
            "🗓 **Дата и время:** {formatted_date} в **{visit_time}**\n"
            "💳 **Стоимость:** {price}"
        ),
        "hints": "{appointment_id}, {status_badge}, {branch_title}, {service_title}, {master_name}, {formatted_date}, {visit_time}, {price}"
    },
    "price_1_branch": {
        "group": "menu",
        "title": "💰 Прайс-лист: Шаг 1 (Выбор студии)",
        "text": (
            "✨ **Прайс-лист · Sahar&Vosk**\n"
            "──────────────\n"
            "Выберите студию для просмотра каталога услуг и актуальных цен:"
        ),
        "hints": "Переменных нет"
    },
    "price_2_category": {
        "group": "menu",
        "title": "💰 Прайс-лист: Шаг 2 (Направления)",
        "text": (
            "📍 **Прайс-лист · Sahar&Vosk ({branch_title})**\n"
            "──────────────\n"
            "Выберите интересующее направление:"
        ),
        "hints": "{branch_title} — название филиала"
    },
    "price_3_subcategory": {
        "group": "menu",
        "title": "💰 Прайс-лист: Шаг 3 (Категории)",
        "text": (
            "📍 **Sahar&Vosk · {branch_title}**\n"
            "✨ **Направление:** {cat_title}\n"
            "──────────────\n"
            "Выберите категорию услуг:"
        ),
        "hints": "{branch_title}, {cat_title}"
    },
    "price_4_services": {
        "group": "menu",
        "title": "💰 Прайс-лист: Шаг 4 (Список цен)",
        "text": (
            "📍 **Sahar&Vosk · {branch_title}**\n"
            "✨ **{cat_title}** · {subcat_title} *(Стр. {page}/{total_pages})*\n"
            "──────────────\n"
            "{services_list}\n\n"
            "──────────────\n"
            "Для бронирования визита нажмите кнопку ниже 👇"
        ),
        "hints": "{branch_title}, {cat_title}, {subcat_title}, {page}, {total_pages}, {services_list}"
    },

    # --- ГРУППА 2: ОБЩИЕ ШАГИ ОНЛАЙН-ЗАПИСИ ---
    "step_1_branch": {
        "group": "booking",
        "title": "💅 Шаг 1: Выбор филиала",
        "text": (
            "✨ **Онлайн-запись · Sahar&Vosk**\n"
            "──────────────\n"
            "Выберите удобную студию в Кудрово:"
        ),
        "hints": "Переменных нет"
    },
    "step_2_category": {
        "group": "booking",
        "title": "📂 Шаг 2: Выбор направления",
        "text": (
            "📍 **Студия:** Sahar&Vosk · {branch_title}\n"
            "──────────────\n"
            "Выберите интересующее вас направление:"
        ),
        "hints": "{branch_title} — название филиала"
    },
    "step_2_5_subcategory": {
        "group": "booking",
        "title": "🗂 Шаг 3: Выбор категории (общий шаблон)",
        "text": (
            "📍 **Студия:** Sahar&Vosk · {branch_title}\n"
            "✨ **Направление:** {cat_title}\n"
            "──────────────\n"
            "Выберите категорию услуг:"
        ),
        "hints": "{branch_title}, {cat_title}"
    },
    "step_3_service": {
        "group": "booking",
        "title": "✨ Шаг 4: Выбор услуги (общий шаблон)",
        "text": (
            "✨ **Направление:** {cat_title}\n"
            "🗂 **Категория:** {subcat_title}\n"
            "──────────────\n"
            "Выберите желаемую процедуру:"
        ),
        "hints": "{cat_title}, {subcat_title}"
    },
    "step_cart": {
        "group": "booking",
        "title": "🛒 Корзина выбранных процедур",
        "text": (
            "📍 **Студия:** Sahar&Vosk · {branch_title}\n"
            "──────────────\n"
            "✨ **Выбранные услуги ({count}):**\n\n"
            "{services_list}\n\n"
            "──────────────\n"
            "💳 **Итого:** {total_price}  ·  ⏱ **Длительность:** ~{total_duration} мин.\n\n"
            "{upsell_block}"
            "Вы можете дополнить визит ещё одной процедурой или перейти к выбору специалиста:"
        ),
        "hints": "{branch_title}, {count}, {services_list}, {total_price}, {total_duration}, {upsell_block}"
    },
    "step_cart_upsell": {
        "group": "booking",
        "title": "✨ Корзина: Блок рекомендаций (Upsell)",
        "text": (
            "💡 **Рекомендуем дополнить ваш визит:**\n"
            "*(быстрое добавление в 1 клик кнопками ниже)*"
        ),
        "hints": "{branch_title}, {total_price}, {total_duration}"
    },
    "step_4_master": {
        "group": "booking",
        "title": "👩‍⚕️ Шаг 5: Выбор специалиста",
        "text": (
            "✨ **Выбрано:** {service_title}\n"
            "💳 **Стоимость:** {price}\n"
            "──────────────\n"
            "Выберите специалиста:"
        ),
        "hints": "{service_title}, {price}"
    },
    "step_5_date": {
        "group": "booking",
        "title": "📅 Шаг 6: Выбор даты (Календарь)",
        "text": (
            "👩‍⚕️ **Специалист:** {master_name}\n"
            "──────────────\n"
            "Выберите удобную дату визита\n"
            "*(в календаре отображаются доступные дни работы специалиста)*:"
        ),
        "hints": "{master_name}"
    },
    "step_6_time": {
        "group": "booking",
        "title": "🕒 Шаг 7: Выбор времени",
        "text": (
            "📅 **Дата визита:** {formatted_date}\n"
            "──────────────\n"
            "Выберите удобное время для записи:"
        ),
        "hints": "{formatted_date}"
    },
    "step_phone": {
        "group": "booking",
        "title": "📱 Ввод номера телефона",
        "text": (
            "📱 **Контактные данные для записи**\n"
            "──────────────\n"
            "Нажмите кнопку **«📱 Отправить мой номер телефона»** внизу экрана "
            "или введите номер вручную в формате `+79991234567`:"
        ),
        "hints": "Переменных нет"
    },
    "step_name": {
        "group": "booking",
        "title": "👤 Ввод имени клиента",
        "text": (
            "👤 **Как к вам обращаться?**\n"
            "──────────────\n"
            "Напишите ваше имя для записи к специалисту{hint}:"
        ),
        "hints": "{hint} — подсказка о кнопке"
    },
    "step_confirm": {
        "group": "booking",
        "title": "📋 Подтверждение визита",
        "text": (
            "✨ **Подтверждение записи · Sahar&Vosk**\n"
            "──────────────\n"
            "📍 **Студия:** Sahar&Vosk · {branch_title}\n"
            "👩‍⚕️ **Специалист:** {master_name}\n"
            "🗓 **Дата и время:** {formatted_date} в **{visit_time}**\n"
            "👤 **Гость:** {client_name} ({phone})\n\n"
            "✨ **Выбранные услуги:**\n{service_title}\n\n"
            "💳 **Итоговая стоимость:** {price}\n"
            "──────────────\n"
            "Подтверждаем бронирование?"
        ),
        "hints": "{branch_title}, {service_title}, {master_name}, {formatted_date}, {visit_time}, {price}, {client_name}, {phone}"
    },
    "step_success": {
        "group": "booking",
        "title": "🎉 Финал: Бронирование подтверждено",
        "text": (
            "✨ **Ваш визит забронирован · №{appointment_id}**\n"
            "──────────────\n"
            "📍 **Студия:** Sahar&Vosk ({address})\n"
            "✨ **Услуги:** {service_title}\n"
            "👩‍⚕️ **Специалист:** {master_name}\n"
            "🗓 **Дата и время:** {formatted_date} в **{visit_time}**\n"
            "💳 **Стоимость:** {price}\n"
            "📞 **Контакт студии:** `+7 (812) 988-98-53`\n"
            "──────────────\n"
            "Мы бережно напомним вам о визите за 24 часа и за 1 час до начала. "
            "Управлять записью можно в разделе **«📋 Мои записи»**."
        ),
        "hints": "{appointment_id}, {address}, {service_title}, {price}, {master_name}, {formatted_date}, {visit_time}"
    },
    "repeat_visit_summary": {
        "group": "booking",
        "title": "🔄 Повтор визита: Сводка перед календарём",
        "text": (
            "🔄 **Повторить прошлый визит · Sahar&Vosk**\n"
            "──────────────\n"
            "📍 **Студия:** Sahar&Vosk · {branch_title}\n"
            "✨ **Услуги:** {services_list}\n"
            "👩‍⚕️ **Специалист:** {master_name}\n"
            "⏱ **Длительность:** ~{total_duration} мин.\n"
            "💳 **Стоимость:** {total_price}\n"
            "──────────────\n"
            "Выберите удобную дату визита в календаре ниже 👇"
        ),
        "hints": "{branch_title}, {services_list}, {master_name}, {total_duration}, {total_price}"
    },
    "repeat_services_updated": {
        "group": "booking",
        "title": "⚠️ Повтор визита: Состав услуг обновился",
        "text": (
            "ℹ️ **Sahar&Vosk · {branch_title}**\n"
            "──────────────\n"
            "Состав услуг вашего прошлого визита обновился в каталоге студии.\n\n"
            "Пожалуйста, выберите желаемые процедуры для записи:"
        ),
        "hints": "{branch_title}"
    },
    "repeat_master_unavailable": {
        "group": "booking",
        "title": "👩‍⚕️ Повтор визита: Мастер недоступен или нет окон",
        "text": (
            "ℹ️ **Sahar&Vosk · {branch_title}**\n"
            "──────────────\n"
            "{reason}\n\n"
            "✨ **Выбранные услуги:** {services_list}\n"
            "💳 **Стоимость:** {total_price}  ·  ⏱ ~{total_duration} мин.\n\n"
            "Пожалуйста, выберите специалиста:"
        ),
        "hints": "{branch_title}, {reason}, {master_name}, {services_list}, {total_price}, {total_duration}"
    },
    "confirm_record_outdated": {
        "group": "booking",
        "title": "ℹ️ Подтверждение визита: Запись неактуальна в CRM",
        "text": (
            "ℹ️ **Запись №{appointment_id} уже неактуальна**\n"
            "──────────────\n"
            "Данная бронь {reason_text} в расписании студии.\n\n"
            "Вы можете выбрать другое удобное время и оформить новый визит в **Sahar&Vosk**:"
        ),
        "hints": "{appointment_id}, {reason_text}, {branch_title}"
    },
    "webhook_record_created": {
        "group": "booking",
        "title": "📲 Внешняя запись: Карточка визита (телефон / карты)",
        "text": (
            "✨ **Ваш визит забронирован · Sahar&Vosk**\n"
            "──────────────\n"
            "📍 **Студия:** Sahar&Vosk · {branch_title}\n"
            "✨ **Услуги:** {services_list}\n"
            "👩‍⚕️ **Специалист:** {master_name}\n"
            "🗓 **Дата и время:** {visit_date} в **{visit_time}**\n"
            "⏱ **Длительность:** ~{total_duration} мин.\n"
            "💳 **Стоимость:** {total_price}\n"
            "📞 **Контакт студии:** `+7 (812) 988-98-53`\n"
            "──────────────\n"
            "Мы зафиксировали вашу запись в расписании студии и напомним о ней за 24 часа и за 1 час до начала.\n"
            "Вы можете управлять визитом кнопками ниже 👇"
        ),
        "hints": "{branch_title}, {visit_date}, {visit_time}, {master_name}, {services_list}, {total_price}, {total_duration}"
    },
    "webhook_record_updated": {
        "group": "booking",
        "title": "🔄 Внешняя запись: Время или мастер изменены",
        "text": (
            "🔄 **Параметры визита обновлены · Sahar&Vosk**\n"
            "──────────────\n"
            "Детали вашей записи были изменены администратором студии:\n\n"
            "📍 **Студия:** Sahar&Vosk · {branch_title}\n"
            "✨ **Услуги:** {services_list}\n"
            "👩‍⚕️ **Специалист:** {master_name}\n"
            "🗓 **Новая дата и время:** {visit_date} в **{visit_time}**\n"
            "──────────────\n"
            "Все напоминания автоматически обновлены на новое время. Ждём вас! 🤍"
        ),
        "hints": "{branch_title}, {visit_date}, {visit_time}, {master_name}, {services_list}"
    },
    "webhook_record_cancelled": {
        "group": "booking",
        "title": "❌ Внешняя запись: Визит отменён администратором",
        "text": (
            "❌ **Визит отменён · Sahar&Vosk**\n"
            "──────────────\n"
            "Ваша запись в студию **Sahar&Vosk · {branch_title}** на **{visit_date}** в **{visit_time}** "
            "(специалист {master_name}) была отменена в расписании студии.\n\n"
            "Если у вас возникли вопросы или вы хотите подобрать другое удобное время, "
            "нажмите **«💅 Записаться онлайн»** или свяжитесь с администратором."
        ),
        "hints": "{branch_title}, {visit_date}, {visit_time}, {master_name}"
    },

    # --- ГРУППА 5: НАПОМИНАНИЯ И ОТЗЫВЫ (NPS) ---
    "rem_24h": {
        "group": "memos",
        "title": "🔔 Напоминание за 24 часа до визита",
        "text": (
            "✨ **Напоминание о визите · Sahar&Vosk**\n"
            "──────────────\n"
            "Ждём вас завтра в нашей студии:\n\n"
            "📍 **Локация:** Sahar&Vosk ({address})\n"
            "✨ **Услуги:** {service_title}\n"
            "👩‍⚕️ **Специалист:** {master_name}\n"
            "🗓 **Дата и время:** {formatted_date} в **{visit_time}**\n"
            "──────────────\n"
            "Пожалуйста, подтвердите ваш визит кнопкой ниже 👇"
        ),
        "hints": "{address}, {service_title}, {master_name}, {formatted_date}, {visit_time}, {branch_title}"
    },
    "rem_1h": {
        "group": "memos",
        "title": "🕒 Напоминание за 1 час до визита",
        "text": (
            "🕒 **Скоро начнётся ваш визит · Sahar&Vosk**\n"
            "──────────────\n"
            "Ждём вас сегодня в **{visit_time}** (через 1 час):\n\n"
            "📍 **Локация:** Sahar&Vosk ({address})\n"
            "✨ **Услуги:** {service_title}\n"
            "👩‍⚕️ **Специалист:** {master_name}\n\n"
            "До скорой встречи! 🤍"
        ),
        "hints": "{address}, {service_title}, {master_name}, {visit_time}, {branch_title}"
    },
    "max_rem_24h": {
        "group": "memos",
        "title": "💬 MAX: Напоминание за 24 часа до визита",
        "text": (
            "Напоминание о визите · Sahar&Vosk\n\n"
            "Здравствуйте, {client_name}! Ждём вас завтра в нашей студии:\n\n"
            "Локация: Sahar&Vosk · {branch_title} ({address})\n"
            "Услуги: {service_title}\n"
            "Специалист: {master_name}\n"
            "Дата и время: {visit_date} в {visit_time}\n\n"
            "Если планы изменятся, пожалуйста, сообщите нам заранее. До встречи!"
        ),
        "hints": "{client_name}, {branch_title}, {address}, {service_title}, {master_name}, {visit_date}, {visit_time}"
    },
    "max_rem_1h": {
        "group": "memos",
        "title": "💬 MAX: Напоминание за 1 час до визита",
        "text": (
            "Скоро начнётся ваш визит · Sahar&Vosk\n\n"
            "{client_name}, ждём вас сегодня в {visit_time} (через 1 час):\n\n"
            "Локация: Sahar&Vosk · {branch_title} ({address})\n"
            "Услуги: {service_title}\n"
            "Специалист: {master_name}\n\n"
            "До скорой встречи!"
        ),
        "hints": "{client_name}, {branch_title}, {address}, {service_title}, {master_name}, {visit_date}, {visit_time}"
    },
    "rem_repeat": {
        "group": "memos",
        "title": "✨ Приглашение на повторный визит",
        "text": (
            "✨ **Время для себя · Sahar&Vosk**\n"
            "──────────────\n"
            "Прошло {days} дн. с вашего прошлого визита на процедуру **«{service_title}»** "
            "(специалист {master_name}).\n\n"
            "Будем рады вновь видеть вас в **Sahar&Vosk** — выберите удобное время онлайн 👇"
        ),
        "hints": "{days}, {service_title}, {master_name}, {branch_title}"
    },
    "nps_request": {
        "group": "memos",
        "title": "⭐️ Оценка визита (NPS-запрос)",
        "text": (
            "✨ **Благодарим за визит в Sahar&Vosk!**\n"
            "──────────────\n"
            "📍 **Студия:** Sahar&Vosk · {branch_title}\n"
            "✨ **Услуга:** {service_title}\n"
            "👩‍⚕️ **Специалист:** {master_name}\n\n"
            "Нам очень важно, чтобы каждый визит приносил вам удовольствие. "
            "Пожалуйста, оцените качество сервиса от 1 до 5 звёзд 👇"
        ),
        "hints": "{branch_title}, {service_title}, {master_name}"
    },
    "nps_high": {
        "group": "memos",
        "title": "💖 NPS: Ответ на высокую оценку (4–5 звёзд)",
        "text": (
            "✨ **Благодарим вас за высокую оценку {stars}!**\n"
            "──────────────\n"
            "Команде **Sahar&Vosk** и специалисту **{master_name}** очень приятно, что визит оставил у вас тёплые впечатления.\n\n"
            "Мы будем искренне признательны, если вы поделитесь коротким отзывом на **Яндекс.Картах** по кнопке ниже 👇"
        ),
        "hints": "{stars}, {master_name}, {branch_title}, {service_title}"
    },
    "nps_low": {
        "group": "memos",
        "title": "🤍 NPS: Ответ на низкую оценку (1–3 звезды)",
        "text": (
            "🤍 **Благодарим за честную оценку ({stars}).**\n"
            "──────────────\n"
            "Нам искренне жаль, что визит оправдал ваши ожидания не в полной мере.\n\n"
            "Пожалуйста, **напишите в ответном сообщении**, на что нам стоит обратить внимание — "
            "ваше сообщение поступит напрямую руководству **Sahar&Vosk**, и мы обязательно примем меры."
        ),
        "hints": "{stars}, {master_name}, {branch_title}, {service_title}"
    },
    "nps_feedback_saved": {
        "group": "memos",
        "title": "✨ NPS: Благодарность за письменный отзыв",
        "text": (
            "✨ **Благодарим вас за обратную связь!**\n"
            "──────────────\n"
            "Ваше сообщение передано управляющему студии **Sahar&Vosk**. Мы свяжемся с вами в ближайшее время."
        ),
        "hints": "Переменных нет"
    }
}


def build_all_default_steps() -> dict:
    """Собирает все базовые шаги + динамически генерирует шаги для всех категорий, подкатегорий и подуровней."""
    steps = dict(BASE_STEPS)

    # 3. ГРУППА: ЭКРАНЫ КАТЕГОРИЙ (НАПРАВЛЕНИЙ)
    for main_id, cfg in bs.MAIN_CATEGORIES_CONFIG.items():
        key = f"cat_{main_id}"
        steps[key] = {
            "group": "cats",
            "title": f"📂 Направление: {cfg['title']}",
            "text": (
                "📍 **Студия:** Sahar&Vosk · {branch_title}\n"
                "✨ **Направление:** {cat_title}\n"
                "──────────────\n"
                "Выберите категорию услуг:"
            ),
            "hints": "{branch_title}, {cat_title}"
        }

    # 4. ГРУППА: ЭКРАНЫ ПОДКАТЕГОРИЙ И ЗОН (ВСЕХ НАПРАВЛЕНИЙ)
    for main_id, main_cfg in bs.MAIN_CATEGORIES_CONFIG.items():
        sub_rules = bs._get_subcat_rules_for_main(main_id)
        for sub_id, sub_cfg in sub_rules.items():
            if sub_id == "other":
                continue
            key = f"sc_{main_id}_{sub_id}"
            has_sub2 = bool(sub_cfg.get("has_sub2"))
            if has_sub2:
                default_txt = (
                    "📍 **Студия:** Sahar&Vosk · {branch_title}\n"
                    "✨ **Категория:** {cat_title}\n"
                    "──────────────\n"
                    "Выберите раздел:"
                )
            else:
                default_txt = (
                    "✨ **Направление:** {cat_title}\n"
                    "🗂 **Категория:** {subcat_title}\n"
                    "──────────────\n"
                    "Выберите желаемую процедуру:"
                )
            steps[key] = {
                "group": "subcats",
                "title": f"🗂 {sub_cfg['title']}",
                "text": default_txt,
                "hints": "{branch_title}, {cat_title}, {subcat_title}"
            }

    # Подуровни зон депиляции (Шугаринг / Воск)
    for s2_id, s2_cfg in bs.DEPIL_ZONES_SUB2.items():
        if s2_id == "all":
            continue
        key = f"s2_depil_{s2_id}"
        steps[key] = {
            "group": "subcats",
            "title": f"🍯 Зона депиляции: {s2_cfg['title']}",
            "text": (
                "✨ **Направление:** {cat_title}\n"
                "🗂 **Категория:** {subcat_title}\n"
                "──────────────\n"
                "Выберите желаемую процедуру:"
            ),
            "hints": "{cat_title}, {subcat_title}"
        }

    # Подуровни мужской депиляции (Шугаринг / Воск)
    for s2_id, s2_cfg in bs.MEN_DEPIL_SUB2.items():
        key = f"s2_men_{s2_id}"
        steps[key] = {
            "group": "subcats",
            "title": f"🧔 Мужской зал: {s2_cfg['title']}",
            "text": (
                "✨ **Направление:** {cat_title}\n"
                "🗂 **Категория:** {subcat_title}\n"
                "──────────────\n"
                "Выберите желаемую процедуру:"
            ),
            "hints": "{cat_title}, {subcat_title}"
        }

    return steps


DEFAULT_STEPS = build_all_default_steps()


def get_step_group(step_key: str) -> str:
    info = DEFAULT_STEPS.get(step_key)
    if info and "group" in info:
        return info["group"]
    if step_key.startswith("cat_"):
        return "cats"
    if step_key.startswith("sc_") or step_key.startswith("s2_"):
        return "subcats"
    if step_key.startswith(("rem_", "nps_", "max_")):
        return "memos"
    if step_key.startswith("step_") or step_key.startswith(("webhook_", "repeat_", "confirm_")):
        return "booking"
    return "menu"


class SafeFormatDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def escape_md(value: str | None) -> str:
    """Экранирует спецсимволы классического Telegram Markdown."""
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


def _utf16_len(text: str) -> int:
    """Возвращает длину строки в UTF-16 code units (метрика лимитов Telegram)."""
    return len((text or "").encode("utf-16-le")) // 2


def _clip_utf16(text: str | None, max_units: int = 1024) -> str:
    """Обрезает строку до лимита UTF-16 code units, не разрывая суррогатные пары эмодзи."""
    s = text or ""
    if _utf16_len(s) <= max_units:
        return s
    raw = s.encode("utf-16-le")[: max_units * 2]
    while len(raw) >= 2:
        try:
            return raw.decode("utf-16-le")
        except UnicodeDecodeError:
            raw = raw[:-2]
    return ""


def _format_step_text(raw_text: str, kwargs: dict) -> str:
    if not raw_text:
        return ""
    try:
        return raw_text.format_map(SafeFormatDict(kwargs))
    except Exception:
        text = raw_text
        for k, v in kwargs.items():
            text = text.replace("{" + str(k) + "}", str(v))
        return text


@asynccontextmanager
async def _connect():
    async with aiosqlite.connect(DB_NAME, timeout=25.0) as db:
        await db.execute("PRAGMA busy_timeout=25000;")
        await db.execute("PRAGMA foreign_keys=ON;")
        yield db


async def _fetch_scalar(cur, default=0):
    row = await cur.fetchone()
    if row and row[0] is not None:
        return row[0]
    return default


async def _migrate_appointments_nullable_telegram_id(db: aiosqlite.Connection) -> None:
    """
    Если старая БД была создана с NOT NULL или FOREIGN KEY на appointments.telegram_id,
    безопасно пересобирает таблицу с nullable telegram_id (необходимо для MAX-only записей).
    """
    cur_cols = await db.execute("PRAGMA table_info(appointments)")
    cols_info = await cur_cols.fetchall()
    if not cols_info:
        return

    tg_notnull = any(row[1] == "telegram_id" and row[3] == 1 for row in cols_info)
    cur_fks = await db.execute("PRAGMA foreign_key_list(appointments)")
    fk_rows = await cur_fks.fetchall()
    has_user_fk = any(len(row) > 2 and str(row[2]).lower() == "users" for row in fk_rows)

    if not tg_notnull and not has_user_fk:
        return

    existing_col_names = [row[1] for row in cols_info]
    target_cols = [
        "id", "telegram_id", "client_name", "phone", "client_phone",
        "branch_id", "branch_title", "cat_id", "service_id", "service_title",
        "price", "duration", "master_id", "master_name", "visit_date",
        "visit_time", "status", "confirmed", "rem_24h_sent", "rem_1h_sent",
        "yclients_record_id", "created_at",
    ]
    copy_cols = [c for c in target_cols if c in existing_col_names]
    cols_sql = ", ".join(copy_cols)

    await db.commit()
    await db.execute("PRAGMA foreign_keys=OFF;")
    try:
        await db.execute("DROP TABLE IF EXISTS appointments_new;")
        await db.execute("""
            CREATE TABLE appointments_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER,
                client_name TEXT,
                phone TEXT,
                client_phone TEXT,
                branch_id TEXT,
                branch_title TEXT,
                cat_id TEXT,
                service_id TEXT,
                service_title TEXT,
                price INTEGER,
                duration INTEGER DEFAULT 30,
                master_id TEXT,
                master_name TEXT,
                visit_date TEXT,
                visit_time TEXT,
                status TEXT DEFAULT 'active',
                confirmed INTEGER DEFAULT 0,
                rem_24h_sent INTEGER DEFAULT 0,
                rem_1h_sent INTEGER DEFAULT 0,
                yclients_record_id TEXT,
                created_at TEXT
            )
        """)
        if copy_cols:
            await db.execute(f"INSERT INTO appointments_new ({cols_sql}) SELECT {cols_sql} FROM appointments;")
        await db.execute("DROP TABLE appointments;")
        await db.execute("ALTER TABLE appointments_new RENAME TO appointments;")
        await db.commit()
    finally:
        await db.execute("PRAGMA foreign_keys=ON;")


async def init_db():
    global DEFAULT_STEPS
    DEFAULT_STEPS = build_all_default_steps()

    async with _connect() as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA synchronous=NORMAL;")
        await db.execute("PRAGMA busy_timeout=25000;")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                real_name TEXT,
                phone TEXT,
                last_branch_id TEXT,
                last_visit_date TEXT,
                is_agreed INTEGER DEFAULT 0,
                created_at TEXT
            )
        """)
        try:
            await db.execute("ALTER TABLE users ADD COLUMN is_agreed INTEGER DEFAULT 0;")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN real_name TEXT;")
        except Exception:
            pass

        await db.execute("""
            CREATE TABLE IF NOT EXISTS appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER,
                client_name TEXT,
                phone TEXT,
                client_phone TEXT,
                branch_id TEXT,
                branch_title TEXT,
                cat_id TEXT,
                service_id TEXT,
                service_title TEXT,
                price INTEGER,
                duration INTEGER DEFAULT 30,
                master_id TEXT,
                master_name TEXT,
                visit_date TEXT,
                visit_time TEXT,
                status TEXT DEFAULT 'active',
                confirmed INTEGER DEFAULT 0,
                rem_24h_sent INTEGER DEFAULT 0,
                rem_1h_sent INTEGER DEFAULT 0,
                yclients_record_id TEXT,
                created_at TEXT
            )
        """)
        try:
            await db.execute("ALTER TABLE appointments ADD COLUMN duration INTEGER DEFAULT 30;")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE appointments ADD COLUMN confirmed INTEGER DEFAULT 0;")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE appointments ADD COLUMN yclients_record_id TEXT;")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE appointments ADD COLUMN client_phone TEXT;")
        except Exception:
            pass
        await _migrate_appointments_nullable_telegram_id(db)
        try:
            await db.execute("ALTER TABLE appointments ADD COLUMN rem_24h_sent INTEGER DEFAULT 0;")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE appointments ADD COLUMN rem_1h_sent INTEGER DEFAULT 0;")
        except Exception:
            pass
        try:
            await db.execute("CREATE INDEX IF NOT EXISTS idx_appointments_yclients_record_id ON appointments(yclients_record_id);")
        except Exception:
            pass
        try:
            await db.execute("CREATE INDEX IF NOT EXISTS idx_appointments_client_phone ON appointments(client_phone);")
        except Exception:
            pass
        try:
            await db.execute("CREATE INDEX IF NOT EXISTS idx_appointments_user ON appointments(telegram_id, status);")
        except Exception:
            pass
        try:
            await db.execute("CREATE INDEX IF NOT EXISTS idx_appointments_date ON appointments(visit_date, status);")
        except Exception:
            pass
        try:
            await db.execute("CREATE INDEX IF NOT EXISTS idx_appointments_branch ON appointments(branch_id, status);")
        except Exception:
            pass
        try:
            await db.execute("""
                UPDATE appointments
                SET visit_time = substr('0' || visit_time, -5)
                WHERE length(visit_time) = 4 AND visit_time LIKE '_:__';
            """)
        except Exception:
            pass
        try:
            cur = await db.execute("""
                SELECT id, phone FROM appointments
                WHERE (client_phone IS NULL OR client_phone = '')
                  AND phone IS NOT NULL AND phone != ''
            """)
            missing_cp_rows = await cur.fetchall()
            for row_id, raw_phone in missing_cp_rows:
                norm_cp = normalize_client_phone(raw_phone)
                if norm_cp:
                    await db.execute(
                        "UPDATE appointments SET client_phone = ? WHERE id = ?",
                        (norm_cp, row_id)
                    )
        except Exception:
            pass

        await db.execute("""
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                appointment_id INTEGER,
                telegram_id INTEGER,
                branch_id TEXT,
                master_name TEXT,
                service_title TEXT,
                rating INTEGER,
                feedback_text TEXT,
                created_at TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS repeat_settings (
                cat_id TEXT PRIMARY KEY,
                days INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                telegram_id INTEGER PRIMARY KEY,
                name TEXT,
                added_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS step_content (
                step_key TEXT PRIMARY KEY,
                title TEXT,
                custom_text TEXT,
                photo_id TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS support_threads (
                telegram_id INTEGER PRIMARY KEY,
                client_name TEXT,
                username TEXT,
                branch_id TEXT,
                has_unread INTEGER DEFAULT 1,
                last_message_at TEXT,
                last_preview TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS support_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER,
                branch_id TEXT,
                sender TEXT,
                sender_name TEXT,
                text TEXT,
                photo_id TEXT,
                created_at TEXT
            )
        """)

        await db.execute("CREATE INDEX IF NOT EXISTS idx_apps_user_status ON appointments(telegram_id, status);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_apps_branch_date ON appointments(branch_id, visit_date, status);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_sup_msg_user ON support_messages(telegram_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_sup_threads_unread ON support_threads(has_unread);")

        for cat_id, days in DEFAULT_REPEAT_DAYS.items():
            await db.execute("INSERT OR IGNORE INTO repeat_settings (cat_id, days) VALUES (?, ?)", (cat_id, days))

        await db.execute("DELETE FROM step_content WHERE step_key LIKE 'memo_%'")

        for step_key, info in DEFAULT_STEPS.items():
            await db.execute(
                "INSERT OR IGNORE INTO step_content (step_key, title, custom_text, photo_id) VALUES (?, ?, ?, NULL)",
                (step_key, info["title"], info["text"])
            )
            await db.execute(
                "UPDATE step_content SET title = ? WHERE step_key = ?",
                (info["title"], step_key)
            )

        await db.execute("""
            UPDATE step_content
            SET custom_text = REPLACE(REPLACE(custom_text, '«Сахар & Воск»', 'Sahar&Vosk'), 'Сахар & Воск', 'Sahar&Vosk')
            WHERE custom_text LIKE '%Сахар%'
        """)
        await db.execute("""
            UPDATE step_content
            SET custom_text = REPLACE(custom_text, '{price} ₽', '{price}')
            WHERE custom_text LIKE '%{price} ₽%'
        """)
        await db.execute("""
            UPDATE step_content
            SET custom_text = REPLACE(custom_text, '{total_price} ₽', '{total_price}')
            WHERE custom_text LIKE '%{total_price} ₽%'
        """)

        cur_steps = await db.execute("SELECT step_key, custom_text FROM step_content WHERE custom_text IS NOT NULL")
        rows_steps = await cur_steps.fetchall()
        for sk_row, raw_step_txt in rows_steps:
            cleaned_txt = normalize_message_dividers(raw_step_txt)
            if cleaned_txt != raw_step_txt:
                await db.execute(
                    "UPDATE step_content SET custom_text = ? WHERE step_key = ?",
                    (cleaned_txt, sk_row)
                )

        await db.commit()


# --- СОГЛАСИЕ И ДАННЫЕ КЛИЕНТА ---

async def has_user_agreed(telegram_id: int) -> bool:
    async with _connect() as db:
        cur = await db.execute("SELECT is_agreed FROM users WHERE telegram_id = ?", (telegram_id,))
        row = await cur.fetchone()
        return bool(row and row[0] == 1)


async def set_user_agreed(telegram_id: int):
    async with _connect() as db:
        await db.execute("UPDATE users SET is_agreed = 1 WHERE telegram_id = ?", (telegram_id,))
        await db.commit()


async def get_user_real_name(telegram_id: int) -> str | None:
    async with _connect() as db:
        cur = await db.execute("SELECT real_name FROM users WHERE telegram_id = ?", (telegram_id,))
        row = await cur.fetchone()
        return row[0] if row and row[0] else None


async def set_user_real_name(telegram_id: int, real_name: str):
    async with _connect() as db:
        await db.execute("UPDATE users SET real_name = ? WHERE telegram_id = ?", (real_name, telegram_id))
        await db.commit()


# --- СОХРАНЕНИЕ ОЦЕНОК И ОТЗЫВОВ (NPS) ---

async def has_review_for_appointment(appointment_id: int) -> bool:
    async with _connect() as db:
        cur = await db.execute("SELECT 1 FROM reviews WHERE appointment_id = ?", (appointment_id,))
        row = await cur.fetchone()
        return row is not None


async def save_review(
    appointment_id: int,
    telegram_id: int,
    branch_id: str,
    master_name: str,
    service_title: str,
    rating: int,
    feedback_text: str = ""
) -> int:
    now_str = now_msk().strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        cur = await db.execute("""
            INSERT INTO reviews (
                appointment_id, telegram_id, branch_id,
                master_name, service_title, rating, feedback_text, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (appointment_id, telegram_id, branch_id, master_name, service_title, rating, feedback_text, now_str))
        await db.commit()
        return cur.lastrowid


async def update_review_feedback(review_id: int, feedback_text: str):
    async with _connect() as db:
        await db.execute("UPDATE reviews SET feedback_text = ? WHERE id = ?", (feedback_text, review_id))
        await db.commit()


# --- ПРИВЯЗКА АРХИВНЫХ ГРУПП ---

async def set_archive_group_id(branch_id: str, chat_id: int):
    async with _connect() as db:
        await db.execute(
            "INSERT INTO bot_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (f"archive_group_{branch_id}", str(chat_id))
        )
        await db.commit()


async def get_archive_group_id(branch_id: str) -> int | None:
    async with _connect() as db:
        cursor = await db.execute("SELECT value FROM bot_settings WHERE key = ?", (f"archive_group_{branch_id}",))
        row = await cursor.fetchone()
        if row and row[0]:
            return int(row[0])
    env_val = ARCHIVE_GROUPS_ENV.get(branch_id, 0)
    return env_val if env_val != 0 else None


# --- ЧАТЫ С КЛИЕНТАМИ ---

async def get_user_phone(telegram_id: int) -> str | None:
    async with _connect() as db:
        cur = await db.execute("SELECT phone FROM users WHERE telegram_id = ?", (telegram_id,))
        row = await cur.fetchone()
        return row[0] if row and row[0] else None


async def save_support_message(
    telegram_id: int,
    client_name: str,
    username: str | None,
    branch_id: str,
    sender: str,
    sender_name: str,
    text: str,
    photo_id: str | None = None
) -> str:
    now_str = now_msk().strftime("%Y-%m-%d %H:%M:%S")
    preview = text[:50] if text else "🖼 [Фотография]"
    has_unread = 1 if sender == "client" else 0

    async with _connect() as db:
        if not branch_id:
            cur = await db.execute("SELECT branch_id, client_name, username FROM support_threads WHERE telegram_id = ?", (telegram_id,))
            row = await cur.fetchone()
            if row:
                branch_id = row[0] or "prazhskaya"
                client_name = client_name or row[1]
                username = username or row[2]
            else:
                branch_id = "prazhskaya"

        await db.execute("""
            INSERT INTO support_messages (telegram_id, branch_id, sender, sender_name, text, photo_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (telegram_id, branch_id, sender, sender_name, text, photo_id, now_str))

        await db.execute("""
            INSERT INTO support_threads (telegram_id, client_name, username, branch_id, has_unread, last_message_at, last_preview)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                client_name = COALESCE(excluded.client_name, support_threads.client_name),
                username = COALESCE(excluded.username, support_threads.username),
                branch_id = excluded.branch_id,
                has_unread = excluded.has_unread,
                last_message_at = excluded.last_message_at,
                last_preview = excluded.last_preview
        """, (telegram_id, client_name, username, branch_id, has_unread, now_str, preview))

        await db.commit()
        return branch_id


async def mark_thread_read(telegram_id: int):
    async with _connect() as db:
        await db.execute("UPDATE support_threads SET has_unread = 0 WHERE telegram_id = ?", (telegram_id,))
        await db.commit()


async def get_unread_threads_count() -> int:
    async with _connect() as db:
        cur = await db.execute("SELECT COUNT(*) FROM support_threads WHERE has_unread = 1")
        row = await cur.fetchone()
        return row[0] if row else 0


async def get_support_threads(only_unread: bool = False) -> list[dict]:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        if only_unread:
            cur = await db.execute("SELECT * FROM support_threads WHERE has_unread = 1 ORDER BY last_message_at DESC")
        else:
            cur = await db.execute("SELECT * FROM support_threads ORDER BY last_message_at DESC")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_thread_info(telegram_id: int) -> dict | None:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM support_threads WHERE telegram_id = ?", (telegram_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_thread_messages(telegram_id: int, limit: int = 12) -> list[dict]:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT * FROM (
                SELECT * FROM support_messages
                WHERE telegram_id = ?
                ORDER BY id DESC
                LIMIT ?
            ) ORDER BY id ASC
        """, (telegram_id, limit))
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


# --- ОЧИСТКА ТЕСТОВОГО КЭША ---

async def clear_test_cache():
    async with _connect() as db:
        await db.execute("DELETE FROM appointments")
        await db.execute("DELETE FROM reviews")
        await db.execute("DELETE FROM support_messages")
        await db.execute("DELETE FROM support_threads")
        await db.execute("DELETE FROM users")
        await db.execute("DELETE FROM sqlite_sequence WHERE name IN ('appointments', 'support_messages', 'reviews')")
        await db.commit()


# --- КОНТЕНТ ШАГОВ, КАТЕГОРИЙ И ПОДКАТЕГОРИЙ ---

async def get_all_steps() -> list[dict]:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM step_content")
        rows = await cursor.fetchall()
        db_map = {r["step_key"]: dict(r) for r in rows}

    ordered = []
    for k, info in DEFAULT_STEPS.items():
        if k in db_map:
            item = db_map.pop(k)
            item["group"] = info.get("group", get_step_group(k))
            ordered.append(item)
    for k, item in db_map.items():
        item["group"] = get_step_group(k)
        ordered.append(item)
    return ordered


async def get_steps_by_group(group: str) -> list[dict]:
    all_steps = await get_all_steps()
    return [s for s in all_steps if s.get("group") == group]


async def get_step_content(step_key: str, fallback_key: str | None = None) -> dict:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM step_content WHERE step_key = ?", (step_key,))
        row = await cursor.fetchone()
        if row:
            res = dict(row)
            res["hints"] = DEFAULT_STEPS.get(step_key, {}).get("hints", "")
            # Если у конкретной категории/подкатегории нет фото, проверяем фото у общего шаблона (fallback_key)
            if not res.get("photo_id") and fallback_key:
                c2 = await db.execute("SELECT photo_id FROM step_content WHERE step_key = ?", (fallback_key,))
                r2 = await c2.fetchone()
                if r2 and r2["photo_id"]:
                    res["photo_id"] = r2["photo_id"]
            return res

        if fallback_key:
            return await get_step_content(fallback_key)

        default = DEFAULT_STEPS.get(step_key, {"title": step_key, "text": "", "hints": ""})
        return {
            "step_key": step_key,
            "title": default["title"],
            "custom_text": default["text"],
            "photo_id": None,
            "hints": default.get("hints", "")
        }


async def update_step_text(step_key: str, new_text: str):
    async with _connect() as db:
        title = DEFAULT_STEPS.get(step_key, {}).get("title", step_key)
        await db.execute(
            "INSERT INTO step_content (step_key, title, custom_text, photo_id) VALUES (?, ?, ?, NULL) "
            "ON CONFLICT(step_key) DO UPDATE SET custom_text = excluded.custom_text",
            (step_key, title, new_text)
        )
        await db.commit()


async def reset_step_text(step_key: str):
    default_text = DEFAULT_STEPS.get(step_key, {}).get("text", "")
    if default_text:
        await update_step_text(step_key, default_text)


async def update_step_photo(step_key: str, photo_id: str | None):
    async with _connect() as db:
        title = DEFAULT_STEPS.get(step_key, {}).get("title", step_key)
        default_text = DEFAULT_STEPS.get(step_key, {}).get("text", "")
        await db.execute(
            "INSERT INTO step_content (step_key, title, custom_text, photo_id) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(step_key) DO UPDATE SET photo_id = excluded.photo_id",
            (step_key, title, default_text, photo_id)
        )
        await db.commit()


async def render_step_to_chat(
    bot,
    chat_id: int,
    step_key: str,
    reply_markup=None,
    fallback_key: str | None = None,
    **kwargs
):
    step = await get_step_content(step_key, fallback_key=fallback_key)
    raw_text = step["custom_text"] or DEFAULT_STEPS.get(step_key, {}).get("text", "")
    text = _format_step_text(raw_text, kwargs) or " "
    photo_id = step.get("photo_id")

    if photo_id and _utf16_len(text) <= 1024:
        try:
            await bot.send_photo(chat_id=chat_id, photo=photo_id, caption=text, reply_markup=reply_markup, parse_mode="Markdown")
            return
        except TelegramForbiddenError:
            raise
        except TelegramBadRequest:
            try:
                await bot.send_photo(chat_id=chat_id, photo=photo_id, caption=text, reply_markup=reply_markup)
                return
            except TelegramForbiddenError:
                raise
            except Exception:
                pass

    if photo_id and _utf16_len(text) > 1024:
        try:
            await bot.send_photo(chat_id=chat_id, photo=photo_id)
        except TelegramForbiddenError:
            raise
        except Exception:
            pass

    msg_text = _clip_utf16(text, 4096)
    try:
        await bot.send_message(chat_id=chat_id, text=msg_text, reply_markup=reply_markup, parse_mode="Markdown", disable_web_page_preview=True)
    except TelegramBadRequest:
        await bot.send_message(chat_id=chat_id, text=msg_text, reply_markup=reply_markup, disable_web_page_preview=True)


async def render_step(
    target: Message | CallbackQuery,
    step_key: str,
    reply_markup=None,
    force_new: bool = False,
    fallback_key: str | None = None,
    **kwargs
):
    step = await get_step_content(step_key, fallback_key=fallback_key)
    raw_text = step["custom_text"] or DEFAULT_STEPS.get(step_key, {}).get("text", "")
    text = _format_step_text(raw_text, kwargs) or " "
    photo_id = step.get("photo_id")
    msg_text = _clip_utf16(text, 4096)

    if isinstance(target, Message):
        if photo_id and _utf16_len(text) <= 1024:
            try:
                await target.answer_photo(photo=photo_id, caption=text, reply_markup=reply_markup, parse_mode="Markdown")
                return
            except TelegramBadRequest:
                try:
                    await target.answer_photo(photo=photo_id, caption=text, reply_markup=reply_markup)
                    return
                except Exception:
                    pass
        elif photo_id and _utf16_len(text) > 1024:
            try:
                await target.answer_photo(photo=photo_id)
            except Exception:
                pass
        try:
            await target.answer(text=msg_text, reply_markup=reply_markup, parse_mode="Markdown", disable_web_page_preview=True)
        except TelegramBadRequest:
            await target.answer(text=msg_text, reply_markup=reply_markup, disable_web_page_preview=True)
        return

    msg = getattr(target, "message", target)
    has_old_photo = bool(getattr(msg, "photo", None))

    if not force_new and not has_old_photo and not photo_id:
        try:
            await msg.edit_text(text=msg_text, reply_markup=reply_markup, parse_mode="Markdown", disable_web_page_preview=True)
        except TelegramBadRequest as e:
            err_lower = str(e).lower()
            if "message is not modified" in err_lower:
                return
            if (
                "message can't be edited" in err_lower
                or "message to edit not found" in err_lower
                or "there is no text" in err_lower
            ):
                if "there is no text" in err_lower:
                    try:
                        await msg.delete()
                    except Exception:
                        pass
                try:
                    await msg.answer(text=msg_text, reply_markup=reply_markup, parse_mode="Markdown", disable_web_page_preview=True)
                    return
                except TelegramBadRequest:
                    try:
                        await msg.answer(text=msg_text, reply_markup=reply_markup, disable_web_page_preview=True)
                        return
                    except Exception:
                        pass
            try:
                await msg.edit_text(text=msg_text, reply_markup=reply_markup, disable_web_page_preview=True)
            except Exception:
                pass
    else:
        try:
            await msg.delete()
        except Exception:
            pass
        if photo_id and _utf16_len(text) <= 1024:
            try:
                await msg.answer_photo(photo=photo_id, caption=text, reply_markup=reply_markup, parse_mode="Markdown")
                return
            except TelegramBadRequest:
                try:
                    await msg.answer_photo(photo=photo_id, caption=text, reply_markup=reply_markup)
                    return
                except Exception:
                    pass
        elif photo_id and _utf16_len(text) > 1024:
            try:
                await msg.answer_photo(photo=photo_id)
            except Exception:
                pass
        try:
            await msg.answer(text=msg_text, reply_markup=reply_markup, parse_mode="Markdown", disable_web_page_preview=True)
        except TelegramBadRequest:
            await msg.answer(text=msg_text, reply_markup=reply_markup, disable_web_page_preview=True)


# --- АДМИНИСТРАТОРЫ ---

async def get_db_admins() -> list[dict]:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM admins ORDER BY added_at ASC")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def is_user_admin(telegram_id: int, env_admin_ids: list[int]) -> bool:
    if telegram_id in env_admin_ids:
        return True
    async with _connect() as db:
        cursor = await db.execute("SELECT 1 FROM admins WHERE telegram_id = ?", (telegram_id,))
        row = await cursor.fetchone()
        return row is not None


async def get_all_admin_ids(env_admin_ids: list[int]) -> list[int]:
    db_admins = await get_db_admins()
    combined = set(env_admin_ids) | {a["telegram_id"] for a in db_admins}
    return list(combined)


async def add_db_admin(telegram_id: int, name: str):
    now = now_msk().strftime("%d.%m.%Y %H:%M")
    async with _connect() as db:
        await db.execute("""
            INSERT INTO admins (telegram_id, name, added_at)
            VALUES (?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET name = excluded.name
        """, (telegram_id, name, now))
        await db.commit()


async def remove_db_admin(telegram_id: int) -> bool:
    async with _connect() as db:
        cursor = await db.execute("DELETE FROM admins WHERE telegram_id = ?", (telegram_id,))
        await db.commit()
        return cursor.rowcount > 0


# --- ЗАПИСИ И ПОЛЬЗОВАТЕЛИ ---

async def register_user(telegram_id: int, username: str | None, first_name: str | None):
    now = now_msk().isoformat(timespec="seconds")
    async with _connect() as db:
        await db.execute("""
            INSERT INTO users (telegram_id, username, first_name, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name
        """, (telegram_id, username, first_name, now))
        await db.commit()


def normalize_time_str(time_val: str | None) -> str:
    if not time_val:
        return "00:00"
    time_val = str(time_val).strip()
    if "T" in time_val:
        time_val = time_val.split("T")[-1]
    elif " " in time_val:
        time_val = time_val.split(" ")[-1]
    parts = time_val.split(":")
    if len(parts) >= 2:
        try:
            return f"{int(parts[0]):02d}:{int(parts[1]):02d}"
        except ValueError:
            return time_val
    return time_val


async def _archive_past_appointments(db: aiosqlite.Connection):
    now = now_msk()
    today_str = now.date().isoformat()
    time_str = now.strftime("%H:%M")
    await db.execute("""
        UPDATE appointments
        SET status = 'completed'
        WHERE status = 'active'
          AND (visit_date < ? OR (visit_date = ? AND substr('0' || visit_time, -5) < ?))
    """, (today_str, today_str, time_str))
    await db.commit()


async def reschedule_and_create_appointment(old_app_id: int | None, telegram_id: int | None, new_data: dict) -> int:
    now = now_msk().isoformat(timespec="seconds")
    duration = int(new_data.get("duration", 30) or 30)
    norm_time = normalize_time_str(new_data.get("visit_time"))
    yc_rec_id = str(new_data["yclients_record_id"]) if new_data.get("yclients_record_id") is not None else None
    raw_phone = new_data.get("client_phone") or new_data.get("phone")
    client_phone = normalize_client_phone(raw_phone) or None
    raw_tg = new_data.get("telegram_id", telegram_id)
    try:
        tg_id_int = int(raw_tg) if raw_tg is not None else None
        if tg_id_int is not None and tg_id_int <= 0:
            tg_id_int = None
    except (ValueError, TypeError):
        tg_id_int = None

    async with _connect() as db:
        if old_app_id:
            if tg_id_int:
                await db.execute("""
                    UPDATE appointments
                    SET status = 'rescheduled'
                    WHERE id = ? AND telegram_id = ? AND status = 'active'
                """, (old_app_id, tg_id_int))
            else:
                await db.execute("""
                    UPDATE appointments
                    SET status = 'rescheduled'
                    WHERE id = ? AND telegram_id IS NULL AND status = 'active'
                """, (old_app_id,))

        cursor = await db.execute("""
            INSERT INTO appointments (
                telegram_id, client_name, phone, client_phone,
                branch_id, branch_title, cat_id,
                service_id, service_title, price, duration,
                master_id, master_name, visit_date, visit_time,
                status, yclients_record_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
        """, (
            tg_id_int, new_data["client_name"], new_data["phone"], client_phone,
            new_data["branch_id"], new_data["branch_title"], new_data["cat_id"],
            new_data["service_id"], new_data["service_title"], new_data["price"], duration,
            new_data["master_id"], new_data["master_name"], new_data["visit_date"], norm_time,
            yc_rec_id, now
        ))
        appointment_id = cursor.lastrowid

        if tg_id_int:
            await db.execute("""
                UPDATE users
                SET phone = ?, real_name = ?, last_branch_id = ?, last_visit_date = ?
                WHERE telegram_id = ?
            """, (new_data["phone"], new_data["client_name"], new_data["branch_id"], new_data["visit_date"], tg_id_int))

        await db.commit()
        return appointment_id


async def create_appointment(data: dict) -> int:
    return await reschedule_and_create_appointment(None, data.get("telegram_id"), data)


async def get_booked_slots(branch_id: str, master_id: str, visit_date: str) -> list[str]:
    async with _connect() as db:
        if master_id == "any":
            return []
        cursor = await db.execute("""
            SELECT substr('0' || visit_time, -5) FROM appointments
            WHERE branch_id = ? AND master_id = ? AND visit_date = ? AND status = 'active'
        """, (branch_id, master_id, visit_date))
        rows = await cursor.fetchall()
        return [row[0] for row in rows]


async def get_user_active_appointments(telegram_id: int) -> list[dict]:
    async with _connect() as db:
        await _archive_past_appointments(db)
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT * FROM appointments
            WHERE telegram_id = ? AND status = 'active'
            ORDER BY visit_date ASC, substr('0' || visit_time, -5) ASC, id ASC
        """, (telegram_id,))
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]



async def get_all_active_appointments(branch_id: str | None = None) -> list[dict]:
    async with _connect() as db:
        await _archive_past_appointments(db)
        db.row_factory = aiosqlite.Row
        if branch_id and branch_id != "all":
            cursor = await db.execute("""
                SELECT * FROM appointments
                WHERE status = 'active' AND branch_id = ?
                ORDER BY visit_date ASC, substr('0' || visit_time, -5) ASC, id ASC
            """, (branch_id,))
        else:
            cursor = await db.execute("""
                SELECT * FROM appointments
                WHERE status = 'active'
                ORDER BY visit_date ASC, substr('0' || visit_time, -5) ASC, id ASC
            """)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_appointments_for_scheduler() -> list[dict]:
    cutoff = (now_msk().date() - timedelta(days=366)).isoformat()
    async with _connect() as db:
        await _archive_past_appointments(db)
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT * FROM appointments
            WHERE (
                (telegram_id IS NOT NULL AND telegram_id > 0)
                OR (
                    status = 'active'
                    AND client_phone IS NOT NULL
                    AND client_phone != ''
                )
            )
              AND (status = 'active'
               OR (status = 'completed' AND visit_date >= ?))
            ORDER BY visit_date ASC, substr('0' || visit_time, -5) ASC, id ASC
        """, (cutoff,))
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_appointment_by_id(appointment_id: int) -> dict | None:
    async with _connect() as db:
        await _archive_past_appointments(db)
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM appointments WHERE id = ?", (appointment_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def confirm_appointment_visit(appointment_id: int, telegram_id: int) -> bool:
    async with _connect() as db:
        cursor = await db.execute("""
            UPDATE appointments
            SET confirmed = 1
            WHERE id = ? AND telegram_id = ? AND status = 'active'
        """, (appointment_id, telegram_id))
        await db.commit()
        return cursor.rowcount > 0


async def cancel_appointment(appointment_id: int, telegram_id: int) -> bool:
    async with _connect() as db:
        cursor = await db.execute("""
            UPDATE appointments
            SET status = 'cancelled'
            WHERE id = ? AND telegram_id = ? AND status = 'active'
        """, (appointment_id, telegram_id))
        await db.commit()
        return cursor.rowcount > 0


async def admin_cancel_appointment(appointment_id: int) -> dict | None:
    app = await get_appointment_by_id(appointment_id)
    if not app or app["status"] == "cancelled":
        return None
    async with _connect() as db:
        await db.execute("UPDATE appointments SET status = 'cancelled' WHERE id = ?", (appointment_id,))
        await db.commit()
    return app


async def claim_appointment_reminder(appointment_id: int, kind: str) -> bool:
    """Атомарно фиксирует отправку напоминания ('24h' или '1h') для активной записи."""
    col = "rem_24h_sent" if kind == "24h" else ("rem_1h_sent" if kind == "1h" else None)
    if not col:
        raise ValueError(f"Unsupported reminder kind: {kind}")
    async with _connect() as db:
        cursor = await db.execute(f"""
            UPDATE appointments
            SET {col} = 1
            WHERE id = ? AND status = 'active' AND COALESCE({col}, 0) = 0
        """, (appointment_id,))
        await db.commit()
        return cursor.rowcount > 0


async def get_repeat_days(cat_id: str) -> int:
    async with _connect() as db:
        cursor = await db.execute("SELECT days FROM repeat_settings WHERE cat_id = ?", (cat_id,))
        row = await cursor.fetchone()
        return row[0] if row else 28


async def get_all_repeat_settings() -> dict[str, int]:
    async with _connect() as db:
        cursor = await db.execute("SELECT cat_id, days FROM repeat_settings")
        rows = await cursor.fetchall()
        return {row[0]: row[1] for row in rows}


async def set_repeat_days(cat_id: str, days: int):
    async with _connect() as db:
        await db.execute("""
            INSERT INTO repeat_settings (cat_id, days)
            VALUES (?, ?)
            ON CONFLICT(cat_id) DO UPDATE SET days = excluded.days
        """, (cat_id, days))
        await db.commit()


async def get_admin_stats() -> dict:
    async with _connect() as db:
        await _archive_past_appointments(db)
        c1 = await db.execute("SELECT COUNT(*) FROM users")
        total_users = await _fetch_scalar(c1, 0)

        c2 = await db.execute("SELECT COUNT(*) FROM appointments WHERE status = 'active'")
        active_apps = await _fetch_scalar(c2, 0)

        c3 = await db.execute("SELECT COUNT(*) FROM appointments WHERE status = 'active' AND branch_id = 'prazhskaya'")
        prazhskaya_apps = await _fetch_scalar(c3, 0)

        c4 = await db.execute("SELECT COUNT(*) FROM appointments WHERE status = 'active' AND branch_id = 'leningradskaya'")
        leningradskaya_apps = await _fetch_scalar(c4, 0)

        c5 = await db.execute("SELECT COUNT(*) FROM support_threads WHERE has_unread = 1")
        unread_chats = await _fetch_scalar(c5, 0)

        return {
            "total_users": total_users,
            "active_apps": active_apps,
            "prazhskaya_apps": prazhskaya_apps,
            "leningradskaya_apps": leningradskaya_apps,
            "unread_chats": unread_chats
        }


async def get_users_by_segment(segment: str) -> list[int]:
    async with _connect() as db:
        if segment == "all":
            cursor = await db.execute("SELECT telegram_id FROM users WHERE telegram_id IS NOT NULL AND telegram_id > 0")
        elif segment.startswith("branch_"):
            branch_id = segment.replace("branch_", "")
            cursor = await db.execute(
                "SELECT telegram_id FROM users WHERE last_branch_id = ? AND telegram_id IS NOT NULL AND telegram_id > 0",
                (branch_id,)
            )
        elif segment.startswith("cat_"):
            cat_id = segment.replace("cat_", "")
            cursor = await db.execute(
                "SELECT DISTINCT telegram_id FROM appointments WHERE cat_id = ? AND telegram_id IS NOT NULL AND telegram_id > 0",
                (cat_id,)
            )
        elif segment == "inactive_30":
            cutoff = (now_msk().date() - timedelta(days=30)).isoformat()
            cursor = await db.execute(
                "SELECT telegram_id FROM users WHERE last_visit_date IS NOT NULL AND last_visit_date <= ? AND telegram_id IS NOT NULL AND telegram_id > 0",
                (cutoff,)
            )
        else:
            cursor = await db.execute("SELECT telegram_id FROM users WHERE telegram_id IS NOT NULL AND telegram_id > 0")

        rows = await cursor.fetchall()
        return [row[0] for row in rows]
async def reschedule_appointment(appointment_id: int, telegram_id: int) -> bool:
    async with _connect() as db:
        cursor = await db.execute("""
            UPDATE appointments
            SET status = 'rescheduled'
            WHERE id = ? AND telegram_id = ? AND status = 'active'
        """, (appointment_id, telegram_id))
        await db.commit()
        return cursor.rowcount > 0


async def admin_reschedule_appointment(appointment_id: int) -> dict | None:
    app = await get_appointment_by_id(appointment_id)
    if not app:
        return None
    async with _connect() as db:
        await db.execute("UPDATE appointments SET status = 'rescheduled' WHERE id = ?", (appointment_id,))
        await db.commit()
    return app


async def get_branch_digest_stats(db: aiosqlite.Connection, branch_id: str | None, start_date: str, end_date: str) -> dict:
    start_dt = f"{start_date} 00:00:00"
    end_dt = f"{end_date} 23:59:59.999999"

    # 1. Новые бронирования за период
    if branch_id:
        c1 = await db.execute("""
            SELECT COUNT(*) FROM appointments
            WHERE REPLACE(created_at, 'T', ' ') BETWEEN ? AND ?
              AND branch_id = ?
        """, (start_dt, end_dt, branch_id))
    else:
        c1 = await db.execute("""
            SELECT COUNT(*) FROM appointments
            WHERE REPLACE(created_at, 'T', ' ') BETWEEN ? AND ?
        """, (start_dt, end_dt))
    new_bookings = await _fetch_scalar(c1, 0)

    # 2. Сумма забронированных услуг (без отмененных и перенесенных)
    if branch_id:
        c2 = await db.execute("""
            SELECT COALESCE(SUM(CASE WHEN price > 0 THEN price ELSE 0 END), 0) FROM appointments
            WHERE REPLACE(created_at, 'T', ' ') BETWEEN ? AND ?
              AND status NOT IN ('cancelled', 'rescheduled')
              AND branch_id = ?
        """, (start_dt, end_dt, branch_id))
    else:
        c2 = await db.execute("""
            SELECT COALESCE(SUM(CASE WHEN price > 0 THEN price ELSE 0 END), 0) FROM appointments
            WHERE REPLACE(created_at, 'T', ' ') BETWEEN ? AND ?
              AND status NOT IN ('cancelled', 'rescheduled')
        """, (start_dt, end_dt))
    revenue = int(await _fetch_scalar(c2, 0))

    # 3. Визиты состоялось (completed)
    if branch_id:
        c3 = await db.execute("""
            SELECT COUNT(*) FROM appointments
            WHERE (visit_date BETWEEN ? AND ? OR REPLACE(created_at, 'T', ' ') BETWEEN ? AND ?)
              AND status = 'completed'
              AND branch_id = ?
        """, (start_date, end_date, start_dt, end_dt, branch_id))
    else:
        c3 = await db.execute("""
            SELECT COUNT(*) FROM appointments
            WHERE (visit_date BETWEEN ? AND ? OR REPLACE(created_at, 'T', ' ') BETWEEN ? AND ?)
              AND status = 'completed'
        """, (start_date, end_date, start_dt, end_dt))
    completed = await _fetch_scalar(c3, 0)

    # 4. Визиты отменено (cancelled)
    if branch_id:
        c4 = await db.execute("""
            SELECT COUNT(*) FROM appointments
            WHERE (visit_date BETWEEN ? AND ? OR REPLACE(created_at, 'T', ' ') BETWEEN ? AND ?)
              AND status = 'cancelled'
              AND branch_id = ?
        """, (start_date, end_date, start_dt, end_dt, branch_id))
    else:
        c4 = await db.execute("""
            SELECT COUNT(*) FROM appointments
            WHERE (visit_date BETWEEN ? AND ? OR REPLACE(created_at, 'T', ' ') BETWEEN ? AND ?)
              AND status = 'cancelled'
        """, (start_date, end_date, start_dt, end_dt))
    cancelled = await _fetch_scalar(c4, 0)

    # 5. Визиты перенесено (rescheduled)
    if branch_id:
        c5 = await db.execute("""
            SELECT COUNT(*) FROM appointments
            WHERE (visit_date BETWEEN ? AND ? OR REPLACE(created_at, 'T', ' ') BETWEEN ? AND ?)
              AND status = 'rescheduled'
              AND branch_id = ?
        """, (start_date, end_date, start_dt, end_dt, branch_id))
    else:
        c5 = await db.execute("""
            SELECT COUNT(*) FROM appointments
            WHERE (visit_date BETWEEN ? AND ? OR REPLACE(created_at, 'T', ' ') BETWEEN ? AND ?)
              AND status = 'rescheduled'
        """, (start_date, end_date, start_dt, end_dt))
    rescheduled = await _fetch_scalar(c5, 0)

    # 6. NPS отзывы
    if branch_id:
        c6 = await db.execute("""
            SELECT COUNT(*), AVG(rating) FROM reviews
            WHERE REPLACE(created_at, 'T', ' ') BETWEEN ? AND ?
              AND branch_id = ?
        """, (start_dt, end_dt, branch_id))
    else:
        c6 = await db.execute("""
            SELECT COUNT(*), AVG(rating) FROM reviews
            WHERE REPLACE(created_at, 'T', ' ') BETWEEN ? AND ?
        """, (start_dt, end_dt))
    row6 = await c6.fetchone()
    reviews_count = row6[0] if row6 else 0
    avg_rating = round(float(row6[1]), 1) if (row6 and row6[1] is not None) else None

    # 7. Топ-3 специалиста (только для филиалов)
    top_masters = []
    if branch_id:
        cur_rev = await db.execute("""
            SELECT master_name,
                   COUNT(*) as total_reviews,
                   SUM(CASE WHEN rating = 5 THEN 1 ELSE 0 END) as five_stars,
                   AVG(rating) as avg_rating
            FROM reviews
            WHERE REPLACE(created_at, 'T', ' ') BETWEEN ? AND ?
              AND branch_id = ?
              AND master_name IS NOT NULL AND master_name != ''
            GROUP BY master_name
        """, (start_dt, end_dt, branch_id))
        rev_rows = await cur_rev.fetchall()
        rev_by_master = {r[0]: {"total_reviews": r[1], "five_stars": r[2] or 0, "avg_rating": round(float(r[3]), 1) if r[3] else 0.0} for r in rev_rows}

        cur_comp = await db.execute("""
            SELECT master_name, COUNT(*) as completed_count
            FROM appointments
            WHERE (visit_date BETWEEN ? AND ? OR REPLACE(created_at, 'T', ' ') BETWEEN ? AND ?)
              AND status = 'completed'
              AND branch_id = ?
              AND master_name IS NOT NULL AND master_name != ''
              AND master_id != 'any'
            GROUP BY master_name
        """, (start_date, end_date, start_dt, end_dt, branch_id))
        comp_rows = await cur_comp.fetchall()
        comp_by_master = {r[0]: r[1] for r in comp_rows}

        all_names = set(rev_by_master.keys()) | set(comp_by_master.keys())
        filtered_names = []
        for name in all_names:
            norm = name.strip().lower()
            if not norm or "любой" in norm or "свободный" in norm or norm == "any":
                continue
            filtered_names.append(name.strip())

        masters_data = []
        for name in filtered_names:
            rdata = rev_by_master.get(name, {})
            c_count = comp_by_master.get(name, 0)
            f_stars = rdata.get("five_stars", 0)
            a_rate = rdata.get("avg_rating", 0.0)
            t_rev = rdata.get("total_reviews", 0)
            if f_stars > 0 or c_count > 0 or t_rev > 0:
                masters_data.append({
                    "name": name,
                    "five_stars": f_stars,
                    "completed": c_count,
                    "avg_rating": a_rate,
                    "reviews_count": t_rev
                })

        masters_data.sort(key=lambda m: (m["five_stars"], m["completed"], m["avg_rating"]), reverse=True)
        top_masters = masters_data[:3]

    return {
        "new_bookings": new_bookings,
        "revenue": revenue,
        "completed": completed,
        "cancelled": cancelled,
        "rescheduled": rescheduled,
        "reviews_count": reviews_count,
        "avg_rating": avg_rating,
        "top_masters": top_masters
    }


def _format_revenue_digest(val: int) -> str:
    if val <= 0:
        return "0 ₽"
    return f"{val:,} ₽".replace(",", " ")


def _format_nps_digest(avg_rating: float | None, reviews_count: int) -> str:
    if not reviews_count or avg_rating is None:
        return "— (нет оценок)"
    word = "отзыв" if reviews_count % 10 == 1 and reviews_count % 100 != 11 else ("отзыва" if 2 <= reviews_count % 10 <= 4 and not (12 <= reviews_count % 100 <= 14) else "отзывов")
    return f"**{avg_rating:.1f} ★** ({reviews_count} {word})"


async def build_management_digest_text(start_date: str, end_date: str, is_weekly: bool = False) -> str:
    async with _connect() as db:
        await _archive_past_appointments(db)
        prazh = await get_branch_digest_stats(db, "prazhskaya", start_date, end_date)
        lenin = await get_branch_digest_stats(db, "leningradskaya", start_date, end_date)
        total = await get_branch_digest_stats(db, None, start_date, end_date)

    start_fmt = date.fromisoformat(start_date).strftime("%d.%m.%Y")
    end_fmt = date.fromisoformat(end_date).strftime("%d.%m.%Y")

    header_title = "📊 **Еженедельный дайджест · Sahar&Vosk**" if is_weekly else "📊 **Управленческий дайджест · Sahar&Vosk**"
    period_title = f"🗓 **Календарная неделя:** `{start_fmt} – {end_fmt}`" if is_weekly else f"🗓 **Период (7 дней):** `{start_fmt} – {end_fmt}`"

    def _render_masters(masters: list[dict]) -> str:
        if not masters:
            return "  • *нет данных за период*\n"
        res = []
        for i, m in enumerate(masters, 1):
            parts = []
            if m["five_stars"] > 0:
                parts.append(f"★ 5.0 ({m['five_stars']} 5★)")
            elif m["avg_rating"] > 0:
                parts.append(f"★ {m['avg_rating']:.1f}")
            if m["completed"] > 0:
                parts.append(f"{m['completed']} виз.")
            stats_str = " · ".join(parts) if parts else "активен"
            res.append(f"  {i}. **{escape_md(m['name'])}** — {stats_str}")
        return "\n".join(res) + "\n"

    lines = [
        header_title,
        period_title,
        "──────────────\n",
        "✨ **Студия · Пражская, 3**",
        f"• Новых записей через бота: **{prazh['new_bookings']}**",
        f"• Сумма бронирований: **{_format_revenue_digest(prazh['revenue'])}**",
        f"• Визиты: состоялось **{prazh['completed']}** · отмен **{prazh['cancelled']}** · переносов **{prazh['rescheduled']}**",
        f"• Качество сервиса (NPS): {_format_nps_digest(prazh['avg_rating'], prazh['reviews_count'])}",
        "• Топ-3 специалиста:",
        _render_masters(prazh["top_masters"]),
        "──────────────\n",
        "✨ **Студия · Ленинградская, 7**",
        f"• Новых записей через бота: **{lenin['new_bookings']}**",
        f"• Сумма бронирований: **{_format_revenue_digest(lenin['revenue'])}**",
        f"• Визиты: состоялось **{lenin['completed']}** · отмен **{lenin['cancelled']}** · переносов **{lenin['rescheduled']}**",
        f"• Качество сервиса (NPS): {_format_nps_digest(lenin['avg_rating'], lenin['reviews_count'])}",
        "• Топ-3 специалиста:",
        _render_masters(lenin["top_masters"]),
        "──────────────\n",
        "🏆 **ИТОГО ПО СЕТИ Sahar&Vosk:**",
        f"• Всего новых записей: **{total['new_bookings']}**",
        f"• Общая выручка броней: **{_format_revenue_digest(total['revenue'])}**",
        f"• Визиты: состоялось **{total['completed']}** · отмен **{total['cancelled']}** · переносов **{total['rescheduled']}**",
        f"• Средний NPS по сети: {_format_nps_digest(total['avg_rating'], total['reviews_count'])}"
    ]

    return "\n".join(lines)
async def get_last_completed_appointment(telegram_id: int) -> dict | None:
    async with _connect() as db:
        await _archive_past_appointments(db)
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT * FROM appointments
            WHERE telegram_id = ? AND status = 'completed'
            ORDER BY visit_date DESC, substr('0' || visit_time, -5) DESC, id DESC
            LIMIT 1
        """, (telegram_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def has_completed_appointments(telegram_id: int) -> bool:
    async with _connect() as db:
        await _archive_past_appointments(db)
        cur = await db.execute("""
            SELECT 1 FROM appointments
            WHERE telegram_id = ? AND status = 'completed'
            LIMIT 1
        """, (telegram_id,))
        row = await cur.fetchone()
        return row is not None
