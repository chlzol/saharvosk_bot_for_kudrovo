import aiosqlite
from datetime import datetime, date, timedelta, timezone
from aiogram.types import Message, CallbackQuery
from aiogram.exceptions import TelegramBadRequest
from config import ARCHIVE_GROUPS_ENV
from services import booking_service as bs

DB_NAME = "saharvosk.db"
MSK_TZ = timezone(timedelta(hours=3))


def now_msk() -> datetime:
    return datetime.now(MSK_TZ).replace(tzinfo=None)


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
            "────────────────────\n"
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
            "────────────────────\n"
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
            "────────────────────\n\n"
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
            "────────────────────\n\n"
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
            "────────────────────\n\n"
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
            "────────────────────\n"
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
            "────────────────────\n"
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
            "────────────────────\n"
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
            "────────────────────\n"
            "Выберите студию для просмотра каталога услуг и актуальных цен:"
        ),
        "hints": "Переменных нет"
    },
    "price_2_category": {
        "group": "menu",
        "title": "💰 Прайс-лист: Шаг 2 (Направления)",
        "text": (
            "📍 **Прайс-лист · Sahar&Vosk ({branch_title})**\n"
            "────────────────────\n"
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
            "────────────────────\n"
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
            "────────────────────\n"
            "{services_list}\n\n"
            "────────────────────\n"
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
            "────────────────────\n"
            "Выберите удобную студию в Кудрово:"
        ),
        "hints": "Переменных нет"
    },
    "step_2_category": {
        "group": "booking",
        "title": "📂 Шаг 2: Выбор направления",
        "text": (
            "📍 **Студия:** Sahar&Vosk · {branch_title}\n"
            "────────────────────\n"
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
            "────────────────────\n"
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
            "────────────────────\n"
            "Выберите желаемую процедуру:"
        ),
        "hints": "{cat_title}, {subcat_title}"
    },
    "step_cart": {
        "group": "booking",
        "title": "🛒 Корзина выбранных процедур",
        "text": (
            "📍 **Студия:** Sahar&Vosk · {branch_title}\n"
            "────────────────────\n"
            "✨ **Выбранные услуги ({count}):**\n\n"
            "{services_list}\n\n"
            "────────────────────\n"
            "💳 **Итого:** {total_price}  ·  ⏱ **Длительность:** ~{total_duration} мин.\n\n"
            "Вы можете дополнить визит ещё одной процедурой или перейти к выбору специалиста:"
        ),
        "hints": "{branch_title}, {count}, {services_list}, {total_price}, {total_duration}"
    },
    "step_4_master": {
        "group": "booking",
        "title": "👩‍⚕️ Шаг 5: Выбор специалиста",
        "text": (
            "✨ **Выбрано:** {service_title}\n"
            "💳 **Стоимость:** {price}\n"
            "────────────────────\n"
            "Выберите специалиста:"
        ),
        "hints": "{service_title}, {price}"
    },
    "step_5_date": {
        "group": "booking",
        "title": "📅 Шаг 6: Выбор даты (Календарь)",
        "text": (
            "👩‍⚕️ **Специалист:** {master_name}\n"
            "────────────────────\n"
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
            "────────────────────\n"
            "Выберите удобное время для записи:"
        ),
        "hints": "{formatted_date}"
    },
    "step_phone": {
        "group": "booking",
        "title": "📱 Ввод номера телефона",
        "text": (
            "📱 **Контактные данные для записи**\n"
            "────────────────────\n"
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
            "────────────────────\n"
            "Напишите ваше имя для записи к специалисту{hint}:"
        ),
        "hints": "{hint} — подсказка о кнопке"
    },
    "step_confirm": {
        "group": "booking",
        "title": "📋 Подтверждение визита",
        "text": (
            "✨ **Подтверждение записи · Sahar&Vosk**\n"
            "────────────────────\n"
            "📍 **Студия:** Sahar&Vosk · {branch_title}\n"
            "👩‍⚕️ **Специалист:** {master_name}\n"
            "🗓 **Дата и время:** {formatted_date} в **{visit_time}**\n"
            "👤 **Гость:** {client_name} ({phone})\n\n"
            "✨ **Выбранные услуги:**\n{service_title}\n\n"
            "💳 **Итоговая стоимость:** {price}\n"
            "────────────────────\n"
            "Подтверждаем бронирование?"
        ),
        "hints": "{branch_title}, {service_title}, {master_name}, {formatted_date}, {visit_time}, {price}, {client_name}, {phone}"
    },
    "step_success": {
        "group": "booking",
        "title": "🎉 Финал: Бронирование подтверждено",
        "text": (
            "✨ **Ваш визит забронирован · №{appointment_id}**\n"
            "────────────────────\n"
            "📍 **Студия:** Sahar&Vosk ({address})\n"
            "✨ **Услуги:** {service_title}\n"
            "👩‍⚕️ **Специалист:** {master_name}\n"
            "🗓 **Дата и время:** {formatted_date} в **{visit_time}**\n"
            "💳 **Стоимость:** {price}\n"
            "📞 **Контакт студии:** `+7 (812) 988-98-53`\n"
            "────────────────────\n"
            "Мы бережно напомним вам о визите за 24 часа и за 1 час до начала. "
            "Управлять записью можно в разделе **«📋 Мои записи»**."
        ),
        "hints": "{appointment_id}, {address}, {service_title}, {price}, {master_name}, {formatted_date}, {visit_time}"
    },

    # --- ГРУППА 5: НАПОМИНАНИЯ И ОТЗЫВЫ (NPS) ---
    "rem_24h": {
        "group": "memos",
        "title": "🔔 Напоминание за 24 часа до визита",
        "text": (
            "✨ **Напоминание о визите · Sahar&Vosk**\n"
            "────────────────────\n"
            "Ждём вас завтра в нашей студии:\n\n"
            "📍 **Локация:** Sahar&Vosk ({address})\n"
            "✨ **Услуги:** {service_title}\n"
            "👩‍⚕️ **Специалист:** {master_name}\n"
            "🗓 **Дата и время:** {formatted_date} в **{visit_time}**\n"
            "────────────────────\n"
            "Пожалуйста, подтвердите ваш визит кнопкой ниже 👇"
        ),
        "hints": "{address}, {service_title}, {master_name}, {formatted_date}, {visit_time}, {branch_title}"
    },
    "rem_1h": {
        "group": "memos",
        "title": "🕒 Напоминание за 1 час до визита",
        "text": (
            "🕒 **Скоро начнётся ваш визит · Sahar&Vosk**\n"
            "────────────────────\n"
            "Ждём вас сегодня в **{visit_time}** (через 1 час):\n\n"
            "📍 **Локация:** Sahar&Vosk ({address})\n"
            "✨ **Услуги:** {service_title}\n"
            "👩‍⚕️ **Специалист:** {master_name}\n\n"
            "До скорой встречи! 🤍"
        ),
        "hints": "{address}, {service_title}, {master_name}, {visit_time}, {branch_title}"
    },
    "rem_repeat": {
        "group": "memos",
        "title": "✨ Приглашение на повторный визит",
        "text": (
            "✨ **Время для себя · Sahar&Vosk**\n"
            "────────────────────\n"
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
            "────────────────────\n"
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
            "────────────────────\n"
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
            "────────────────────\n"
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
            "────────────────────\n"
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
                "────────────────────\n"
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
                    "────────────────────\n"
                    "Выберите раздел:"
                )
            else:
                default_txt = (
                    "✨ **Направление:** {cat_title}\n"
                    "🗂 **Категория:** {subcat_title}\n"
                    "────────────────────\n"
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
                "────────────────────\n"
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
                "────────────────────\n"
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
    if step_key.startswith(("rem_", "nps_")):
        return "memos"
    if step_key.startswith("step_"):
        return "booking"
    return "menu"


class SafeFormatDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


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


def _connect():
    return aiosqlite.connect(DB_NAME, timeout=15.0)


async def init_db():
    global DEFAULT_STEPS
    DEFAULT_STEPS = build_all_default_steps()

    async with _connect() as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA synchronous=NORMAL;")
        await db.execute("PRAGMA busy_timeout=10000;")

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
                yclients_record_id TEXT,
                created_at TEXT
            )
        """)
        try:
            await db.execute("ALTER TABLE appointments ADD COLUMN duration INTEGER DEFAULT 30;")
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

    if photo_id and len(text) <= 1024:
        try:
            await bot.send_photo(chat_id=chat_id, photo=photo_id, caption=text, reply_markup=reply_markup, parse_mode="Markdown")
            return
        except TelegramBadRequest:
            try:
                await bot.send_photo(chat_id=chat_id, photo=photo_id, caption=text, reply_markup=reply_markup)
                return
            except Exception:
                pass

    if photo_id and len(text) > 1024:
        try:
            await bot.send_photo(chat_id=chat_id, photo=photo_id)
        except Exception:
            pass

    try:
        await bot.send_message(chat_id=chat_id, text=text[:4096], reply_markup=reply_markup, parse_mode="Markdown", disable_web_page_preview=True)
    except TelegramBadRequest:
        await bot.send_message(chat_id=chat_id, text=text[:4096], reply_markup=reply_markup, disable_web_page_preview=True)


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

    if isinstance(target, Message):
        if photo_id and len(text) <= 1024:
            try:
                await target.answer_photo(photo=photo_id, caption=text, reply_markup=reply_markup, parse_mode="Markdown")
                return
            except TelegramBadRequest:
                try:
                    await target.answer_photo(photo=photo_id, caption=text, reply_markup=reply_markup)
                    return
                except Exception:
                    pass
                return
        elif photo_id and len(text) > 1024:
            try:
                await target.answer_photo(photo=photo_id)
            except Exception:
                pass
        try:
            await target.answer(text=text[:4096], reply_markup=reply_markup, parse_mode="Markdown", disable_web_page_preview=True)
        except TelegramBadRequest:
            await target.answer(text=text[:4096], reply_markup=reply_markup, disable_web_page_preview=True)
        return

    msg = target.message
    has_old_photo = bool(msg.photo)

    if not force_new and not has_old_photo and not photo_id:
        try:
            await msg.edit_text(text=text[:4096], reply_markup=reply_markup, parse_mode="Markdown", disable_web_page_preview=True)
        except TelegramBadRequest as e:
            err_lower = str(e).lower()
            if "message is not modified" in err_lower:
                return
            if "message can't be edited" in err_lower or "message to edit not found" in err_lower:
                try:
                    await msg.answer(text=text[:4096], reply_markup=reply_markup, parse_mode="Markdown", disable_web_page_preview=True)
                    return
                except TelegramBadRequest:
                    try:
                        await msg.answer(text=text[:4096], reply_markup=reply_markup, disable_web_page_preview=True)
                        return
                    except Exception:
                        pass
            try:
                await msg.edit_text(text=text[:4096], reply_markup=reply_markup, disable_web_page_preview=True)
            except Exception:
                pass
    else:
        try:
            await msg.delete()
        except Exception:
            pass
        if photo_id and len(text) <= 1024:
            try:
                await msg.answer_photo(photo=photo_id, caption=text, reply_markup=reply_markup, parse_mode="Markdown")
                return
            except TelegramBadRequest:
                try:
                    await msg.answer_photo(photo=photo_id, caption=text, reply_markup=reply_markup)
                    return
                except Exception:
                    pass
                return
        elif photo_id and len(text) > 1024:
            try:
                await msg.answer_photo(photo=photo_id)
            except Exception:
                pass
        try:
            await msg.answer(text=text[:4096], reply_markup=reply_markup, parse_mode="Markdown", disable_web_page_preview=True)
        except TelegramBadRequest:
            await msg.answer(text=text[:4096], reply_markup=reply_markup, disable_web_page_preview=True)


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


async def _archive_past_appointments(db: aiosqlite.Connection):
    now = now_msk()
    today_str = now.date().isoformat()
    time_str = now.strftime("%H:%M")
    await db.execute("""
        UPDATE appointments
        SET status = 'completed'
        WHERE status = 'active'
          AND (visit_date < ? OR (visit_date = ? AND visit_time < ?))
    """, (today_str, today_str, time_str))
    await db.commit()


async def create_appointment(data: dict) -> int:
    now = now_msk().isoformat(timespec="seconds")
    duration = int(data.get("duration", 30) or 30)
    async with _connect() as db:
        cursor = await db.execute("""
            INSERT INTO appointments (
                telegram_id, client_name, phone,
                branch_id, branch_title, cat_id,
                service_id, service_title, price, duration,
                master_id, master_name, visit_date, visit_time,
                status, yclients_record_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
        """, (
            data["telegram_id"], data["client_name"], data["phone"],
            data["branch_id"], data["branch_title"], data["cat_id"],
            data["service_id"], data["service_title"], data["price"], duration,
            data["master_id"], data["master_name"], data["visit_date"], data["visit_time"],
            data.get("yclients_record_id"), now
        ))
        appointment_id = cursor.lastrowid

        await db.execute("""
            UPDATE users
            SET phone = ?, real_name = ?, last_branch_id = ?, last_visit_date = ?
            WHERE telegram_id = ?
        """, (data["phone"], data["client_name"], data["branch_id"], data["visit_date"], data["telegram_id"]))

        await db.commit()
        return appointment_id


async def get_booked_slots(branch_id: str, master_id: str, visit_date: str) -> list[str]:
    async with _connect() as db:
        if master_id == "any":
            return []
        cursor = await db.execute("""
            SELECT visit_time FROM appointments
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
            ORDER BY visit_date ASC, visit_time ASC
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
                ORDER BY visit_date ASC, visit_time ASC
            """, (branch_id,))
        else:
            cursor = await db.execute("""
                SELECT * FROM appointments
                WHERE status = 'active'
                ORDER BY visit_date ASC, visit_time ASC
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
            WHERE status = 'active'
               OR (status = 'completed' AND visit_date >= ?)
            ORDER BY visit_date ASC, visit_time ASC
        """, (cutoff,))
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_appointment_by_id(appointment_id: int) -> dict | None:
    async with _connect() as db:
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
        total_users = (await c1.fetchone())[0]

        c2 = await db.execute("SELECT COUNT(*) FROM appointments WHERE status = 'active'")
        active_apps = (await c2.fetchone())[0]

        c3 = await db.execute("SELECT COUNT(*) FROM appointments WHERE status = 'active' AND branch_id = 'prazhskaya'")
        prazhskaya_apps = (await c3.fetchone())[0]

        c4 = await db.execute("SELECT COUNT(*) FROM appointments WHERE status = 'active' AND branch_id = 'leningradskaya'")
        leningradskaya_apps = (await c4.fetchone())[0]

        c5 = await db.execute("SELECT COUNT(*) FROM support_threads WHERE has_unread = 1")
        unread_chats = (await c5.fetchone())[0]

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
            cursor = await db.execute("SELECT telegram_id FROM users")
        elif segment.startswith("branch_"):
            branch_id = segment.replace("branch_", "")
            cursor = await db.execute("SELECT telegram_id FROM users WHERE last_branch_id = ?", (branch_id,))
        elif segment.startswith("cat_"):
            cat_id = segment.replace("cat_", "")
            cursor = await db.execute("SELECT DISTINCT telegram_id FROM appointments WHERE cat_id = ?", (cat_id,))
        elif segment == "inactive_30":
            cutoff = (now_msk().date() - timedelta(days=30)).isoformat()
            cursor = await db.execute(
                "SELECT telegram_id FROM users WHERE last_visit_date IS NOT NULL AND last_visit_date <= ?",
                (cutoff,)
            )
        else:
            cursor = await db.execute("SELECT telegram_id FROM users")

        rows = await cursor.fetchall()
        return [row[0] for row in rows]