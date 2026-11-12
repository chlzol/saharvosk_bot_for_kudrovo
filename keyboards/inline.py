import calendar
from datetime import date
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from services.booking_service import get_allowed_months, get_available_dates_in_month, get_branches

MONTH_NAMES = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь"
}

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

STEP_GROUP_TITLES = {
    "menu": "🏠 Главное меню, Прайс и Мои записи",
    "booking": "💅 Общие шаги онлайн-записи",
    "cats": "📂 Категории (Направления услуг)",
    "subcats": "🗂 Подкатегории и Зоны процедур",
    "memos": "🔔 Напоминания и Отзывы (NPS)"
}


def consent_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✨ Принять условия и продолжить", callback_data="consent:accept")]
    ])


def contacts_routes_kb() -> InlineKeyboardMarkup:
    branches = get_branches()
    pr = branches.get("prazhskaya", {})
    ln = branches.get("leningradskaya", {})

    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📍 Геометка · Пражская, 3", callback_data="geo:prazhskaya"),
            InlineKeyboardButton(text="🗺 Яндекс.Карты", url=pr.get("yandex_map_url", "https://yandex.ru/maps"))
        ],
        [
            InlineKeyboardButton(text="📍 Геометка · Ленинградская, 7", callback_data="geo:leningradskaya"),
            InlineKeyboardButton(text="🗺 Яндекс.Карты", url=ln.get("yandex_map_url", "https://yandex.ru/maps"))
        ]
    ])


def branch_route_kb(branch_id: str) -> InlineKeyboardMarkup:
    b = get_branches().get(branch_id, {})
    title = b.get("title", "Студия")
    map_url = b.get("yandex_map_url", "https://yandex.ru/maps")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📍 Геометка · {title}", callback_data=f"geo:{branch_id}")],
        [InlineKeyboardButton(text="🗺 Построить маршрут в Яндекс.Картах", url=map_url)]
    ])


def post_booking_kb(appointment_id: int, branch_id: str, google_cal_url: str) -> InlineKeyboardMarkup:
    b = get_branches().get(branch_id, {})
    map_url = b.get("yandex_map_url", "https://yandex.ru/maps")
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📅 В Google Календарь", url=google_cal_url),
            InlineKeyboardButton(text="📥 В Apple / Смартфон (.ICS)", callback_data=f"cal_ics:{appointment_id}")
        ],
        [InlineKeyboardButton(text="📍 Геометка студии в чат", callback_data=f"geo:{branch_id}")],
        [InlineKeyboardButton(text="🗺 Построить маршрут в Яндекс.Картах", url=map_url)]
    ])


def nps_rating_kb(appointment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="1 ⭐️", callback_data=f"nps:{appointment_id}:1"),
            InlineKeyboardButton(text="2 ⭐️", callback_data=f"nps:{appointment_id}:2"),
            InlineKeyboardButton(text="3 ⭐️", callback_data=f"nps:{appointment_id}:3"),
            InlineKeyboardButton(text="4 ⭐️", callback_data=f"nps:{appointment_id}:4"),
            InlineKeyboardButton(text="5 ⭐️", callback_data=f"nps:{appointment_id}:5"),
        ]
    ])


def yandex_review_kb(branch_id: str) -> InlineKeyboardMarkup:
    b = get_branches().get(branch_id, {})
    rev_url = b.get("yandex_review_url", "https://yandex.ru/maps")
    title = b.get("title", "")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🌟 Оставить отзыв · Яндекс.Карты ({title})", url=rev_url)]
    ])


def branches_kb(branches: dict) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=f"📍 Sahar&Vosk · {b['title']}", callback_data=f"branch:{b_id}")]
        for b_id, b in branches.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def categories_kb(categories: dict, page: int = 1, page_size: int = 11, selected_count: int = 0) -> InlineKeyboardMarkup:
    items = list(categories.items())
    total_pages = max(1, (len(items) + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))

    start = (page - 1) * page_size
    current_items = items[start:start + page_size]

    buttons = [
        [InlineKeyboardButton(text=title, callback_data=f"cat:{c_id}")]
        for c_id, title in current_items
    ]

    if total_pages > 1:
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton(text="‹", callback_data=f"cat_pg:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page} / {total_pages}", callback_data="ignore"))
        if page < total_pages:
            nav.append(InlineKeyboardButton(text="›", callback_data=f"cat_pg:{page + 1}"))
        buttons.append(nav)

    if selected_count > 0:
        buttons.append([InlineKeyboardButton(text=f"✨ Вернуться к выбранным ({selected_count}) ›", callback_data="cart:open")])

    buttons.append([InlineKeyboardButton(text="‹ К выбору студии", callback_data="back:branches")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def subcategories_kb(subcategories: dict, page: int = 1, page_size: int = 10, selected_count: int = 0) -> InlineKeyboardMarkup:
    items = list(subcategories.items())
    total_pages = max(1, (len(items) + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))

    start = (page - 1) * page_size
    current_items = items[start:start + page_size]

    buttons = [
        [InlineKeyboardButton(text=title, callback_data=f"subcat:{sub_id}")]
        for sub_id, title in current_items
    ]

    if total_pages > 1:
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton(text="‹", callback_data=f"subcat_pg:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page} / {total_pages}", callback_data="ignore"))
        if page < total_pages:
            nav.append(InlineKeyboardButton(text="›", callback_data=f"subcat_pg:{page + 1}"))
        buttons.append(nav)

    if selected_count > 0:
        buttons.append([InlineKeyboardButton(text=f"✨ Вернуться к выбранным ({selected_count}) ›", callback_data="cart:open")])

    buttons.append([InlineKeyboardButton(text="‹ К выбору направления", callback_data="back:categories")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def sub2_kb(sub2_options: dict, selected_count: int = 0) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=title, callback_data=f"sub2:{s2_id}")]
        for s2_id, title in sub2_options.items()
    ]
    if selected_count > 0:
        buttons.append([InlineKeyboardButton(text=f"✨ Вернуться к выбранным ({selected_count}) ›", callback_data="cart:open")])
    buttons.append([InlineKeyboardButton(text="‹ Назад к категориям", callback_data="back:subcategories")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def services_kb(
    services: list[dict],
    page: int = 1,
    page_size: int = 6,
    selected_ids: list[str] | None = None,
    has_sub2: bool = False
) -> InlineKeyboardMarkup:
    selected_set = set(str(x) for x in (selected_ids or []))
    total_pages = max(1, (len(services) + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))

    start = (page - 1) * page_size
    current_services = services[start:start + page_size]

    buttons = []
    for s in current_services:
        price_text = f"{s['price']} ₽" if s['price'] > 0 else "по прайсу"
        title = s['title']
        if len(title) > 33:
            title = title[:31] + "…"
        check_mark = "✓ " if str(s["id"]) in selected_set else ""
        buttons.append([InlineKeyboardButton(text=f"{check_mark}{title} · {price_text}", callback_data=f"srv:{s['id']}")])

    if total_pages > 1:
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton(text="‹", callback_data=f"srv_pg:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page} / {total_pages}", callback_data="ignore"))
        if page < total_pages:
            nav.append(InlineKeyboardButton(text="›", callback_data=f"srv_pg:{page + 1}"))
        buttons.append(nav)

    if selected_set:
        buttons.append([
            InlineKeyboardButton(
                text=f"✨ Перейти к выбранным ({len(selected_set)}) ›",
                callback_data="cart:open"
            )
        ])

    back_target = "back:sub2" if has_sub2 else "back:subcategories"
    buttons.append([InlineKeyboardButton(text="‹ Назад", callback_data=back_target)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def cart_kb(selected_services: list[dict]) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="✨ Выбрать специалиста и время ›", callback_data="cart:proceed")],
        [InlineKeyboardButton(text="＋ Добавить услугу из этой категории", callback_data="cart:add_more")],
        [InlineKeyboardButton(text="📂 Добавить услугу из другого раздела", callback_data="cart:add_other_cat")]
    ]

    if len(selected_services) > 1:
        for s in selected_services:
            short_t = s["title"][:25] + "…" if len(s["title"]) > 25 else s["title"]
            buttons.append([
                InlineKeyboardButton(text=f"✕ Убрать: {short_t}", callback_data=f"cart_del:{s['id']}")
            ])

    buttons.append([InlineKeyboardButton(text="↺ Очистить выбор и начать заново", callback_data="cart:clear")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def masters_kb(masters: list[dict], page: int = 1, page_size: int = 6) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text="✨ Любой свободный специалист", callback_data="master:any")]]

    total_pages = max(1, (len(masters) + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))

    start = (page - 1) * page_size
    current_masters = masters[start:start + page_size]

    for m in current_masters:
        buttons.append([InlineKeyboardButton(text=f"👩‍⚕️ {m['name']}", callback_data=f"master:{m['id']}")])

    if total_pages > 1:
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton(text="‹", callback_data=f"mst_pg:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page} / {total_pages}", callback_data="ignore"))
        if page < total_pages:
            nav.append(InlineKeyboardButton(text="›", callback_data=f"mst_pg:{page + 1}"))
        buttons.append(nav)

    buttons.append([InlineKeyboardButton(text="‹ К выбранным услугам", callback_data="cart:open")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def calendar_kb(year: int, month: int, api_dates: list[str] | None = None) -> InlineKeyboardMarkup:
    allowed_months = get_allowed_months()
    available_dates = set(get_available_dates_in_month(year, month, api_dates=api_dates))

    keyboard = [
        [InlineKeyboardButton(text=f"🗓 {MONTH_NAMES[month]} {year}", callback_data="ignore")],
        [InlineKeyboardButton(text=day, callback_data="ignore") for day in WEEKDAYS]
    ]

    month_calendar = calendar.monthcalendar(year, month)
    for week in month_calendar:
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(text=" ", callback_data="ignore"))
            else:
                d = date(year, month, day)
                if d in available_dates:
                    row.append(InlineKeyboardButton(text=str(day), callback_data=f"date:{d.isoformat()}"))
                else:
                    row.append(InlineKeyboardButton(text="·", callback_data="ignore"))
        keyboard.append(row)

    nav_row = []
    if (year, month) == allowed_months[1]:
        prev_y, prev_m = allowed_months[0]
        nav_row.append(InlineKeyboardButton(text=f"‹ {MONTH_NAMES[prev_m]}", callback_data=f"cal:{prev_y}:{prev_m}"))
    if (year, month) == allowed_months[0]:
        next_y, next_m = allowed_months[1]
        nav_row.append(InlineKeyboardButton(text=f"{MONTH_NAMES[next_m]} ›", callback_data=f"cal:{next_y}:{next_m}"))
    if nav_row:
        keyboard.append(nav_row)

    keyboard.append([InlineKeyboardButton(text="‹ К выбору специалиста", callback_data="back:masters")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def times_kb(times: list[str]) -> InlineKeyboardMarkup:
    keyboard = []
    row = []
    for t in times:
        row.append(InlineKeyboardButton(text=f"🕒 {t}", callback_data=f"time:{t}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton(text="‹ К выбору даты", callback_data="back:dates")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✨ Подтвердить бронирование", callback_data="confirm:yes")],
        [InlineKeyboardButton(text="↺ Изменить параметры визита", callback_data="confirm:restart")],
        [InlineKeyboardButton(text="✕ Отменить", callback_data="confirm:cancel")]
    ])


def appointment_manage_kb(
    appointment_id: int,
    branch_id: str | None = None,
    google_cal_url: str | None = None
) -> InlineKeyboardMarkup:
    buttons = []

    cal_row = []
    if google_cal_url:
        cal_row.append(InlineKeyboardButton(text="📅 В Google Календарь", url=google_cal_url))
    cal_row.append(InlineKeyboardButton(text="📥 В Apple (.ICS)", callback_data=f"cal_ics:{appointment_id}"))
    buttons.append(cal_row)

    if branch_id:
        buttons.append([InlineKeyboardButton(text="📍 Как добраться (Геометка)", callback_data=f"geo:{branch_id}")])

    buttons.append([
        InlineKeyboardButton(text="↺ Перенести визит", callback_data=f"reschedule_app:{appointment_id}"),
        InlineKeyboardButton(text="✕ Отменить запись", callback_data=f"cancel_app:{appointment_id}")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def reminder_24h_kb(appointment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✨ Подтверждаю визит", callback_data=f"confirm_visit:{appointment_id}")],
        [
            InlineKeyboardButton(text="↺ Перенести", callback_data=f"reschedule_app:{appointment_id}"),
            InlineKeyboardButton(text="✕ Отменить", callback_data=f"cancel_app:{appointment_id}")
        ]
    ])


def repeat_booking_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✨ Записаться онлайн", callback_data="start_new_booking")]
    ])


def price_branches_kb(branches: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📍 Sahar&Vosk · {b['title']}", callback_data=f"price_br:{b_id}")]
        for b_id, b in branches.items()
    ])


def price_categories_kb(branch_id: str, categories: dict, page: int = 1, page_size: int = 11) -> InlineKeyboardMarkup:
    items = list(categories.items())
    total_pages = max(1, (len(items) + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))

    start = (page - 1) * page_size
    current_items = items[start:start + page_size]

    buttons = [
        [InlineKeyboardButton(text=title, callback_data=f"pr_cat:{branch_id}:{c_id}")]
        for c_id, title in current_items
    ]

    if total_pages > 1:
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton(text="‹", callback_data=f"price_cat_pg:{branch_id}:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page} / {total_pages}", callback_data="ignore"))
        if page < total_pages:
            nav.append(InlineKeyboardButton(text="›", callback_data=f"price_cat_pg:{branch_id}:{page + 1}"))
        buttons.append(nav)

    buttons.append([InlineKeyboardButton(text="‹ К выбору студии", callback_data="price_back:branches")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


_SHORT_BRANCH = {"prazhskaya": "pr", "leningradskaya": "ln"}


def _short_branch(branch_id: str) -> str:
    return _SHORT_BRANCH.get(branch_id, branch_id)


def price_subcategories_kb(branch_id: str, main_cat_id: str, subcategories: dict) -> InlineKeyboardMarkup:
    sb = _short_branch(branch_id)
    buttons = [
        [InlineKeyboardButton(text=title, callback_data=f"ps:{sb}:{main_cat_id}:{sub_id}:1")]
        for sub_id, title in subcategories.items()
    ]
    buttons.append([InlineKeyboardButton(text="‹ К направлениям", callback_data=f"price_br:{branch_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def price_services_view_kb(branch_id: str, main_cat_id: str, subcat_id: str, page: int, total_pages: int) -> InlineKeyboardMarkup:
    sb = _short_branch(branch_id)
    buttons = []
    if total_pages > 1:
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton(text="‹", callback_data=f"ps:{sb}:{main_cat_id}:{subcat_id}:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page} / {total_pages}", callback_data="ignore"))
        if page < total_pages:
            nav.append(InlineKeyboardButton(text="›", callback_data=f"ps:{sb}:{main_cat_id}:{subcat_id}:{page + 1}"))
        buttons.append(nav)

    buttons.append([InlineKeyboardButton(text="✨ Записаться онлайн", callback_data="start_new_booking")])
    buttons.append([InlineKeyboardButton(text="‹ Назад к категориям", callback_data=f"pr_cat:{branch_id}:{main_cat_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def support_branches_kb(branches: dict) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=f"💬 Sahar&Vosk · {b['title']}", callback_data=f"sup_br:{b_id}")]
        for b_id, b in branches.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def archive_jump_to_chat_kb(bot_username: str, client_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="💬 Открыть диалог с клиентом",
            url=f"https://t.me/{bot_username}?start=chat_{client_id}"
        )]
    ])


# =====================================================================
# КЛАВИАТУРЫ АДМИН-ПАНЕЛИ И РЕДАКТОРА ВСЕХ ЭКРАНОВ
# =====================================================================

def admin_main_kb(unread_count: int = 0) -> InlineKeyboardMarkup:
    badge = f" (🔴 {unread_count})" if unread_count > 0 else ""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💬 Диалоги с клиентами{badge}", callback_data="adm:support")],
        [InlineKeyboardButton(text="📋 Активные записи по студиям", callback_data="adm:apps")],
        [InlineKeyboardButton(text="🔄 Синхронизировать с YCLIENTS", callback_data="adm:sync")],
        [InlineKeyboardButton(text="📄 Выгрузить каталог услуг (.txt)", callback_data="adm:export_catalog")],
        [InlineKeyboardButton(text="🎨 Редактор всех шагов, категорий и фото", callback_data="adm:steps")],
        [InlineKeyboardButton(text="📢 Создать рассылку", callback_data="adm:broadcast")],
        [InlineKeyboardButton(text="⏳ Интервалы повторной записи", callback_data="adm:repeat_settings")],
        [InlineKeyboardButton(text="👥 Администраторы", callback_data="adm:admins")],
        [InlineKeyboardButton(text="🧹 Очистить тестовый кэш", callback_data="adm:clear_cache")],
        [InlineKeyboardButton(text="📊 Обновить показатели", callback_data="adm:home")]
    ])


def admin_step_groups_kb(counts: dict[str, tuple[int, int]]) -> InlineKeyboardMarkup:
    """Меню папок редактора шагов (показывает общее число экранов и сколько из них уже с картинками)."""
    buttons = []
    for grp_key, title in STEP_GROUP_TITLES.items():
        total, with_pic = counts.get(grp_key, (0, 0))
        pic_info = f" · 🖼 {with_pic}/{total}" if total > 0 else ""
        buttons.append([
            InlineKeyboardButton(
                text=f"{title}{pic_info}",
                callback_data=f"adm_st_grp:{grp_key}:1"
            )
        ])
    buttons.append([InlineKeyboardButton(text="‹ В панель управления", callback_data="adm:home")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_steps_list_kb(steps: list[dict], group: str = "menu", page: int = 1, page_size: int = 10) -> InlineKeyboardMarkup:
    """Постраничный список экранов внутри выбранной папки редактора шагов."""
    total_pages = max(1, (len(steps) + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    current_steps = steps[start:start + page_size]

    buttons = []
    for s in current_steps:
        pic_badge = "🖼 " if s.get("photo_id") else ""
        buttons.append([
            InlineKeyboardButton(
                text=f"{pic_badge}{s['title']}",
                callback_data=f"adm_step:{s['step_key']}"
            )
        ])

    if total_pages > 1:
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton(text="‹", callback_data=f"adm_st_grp:{group}:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page} / {total_pages}", callback_data="ignore"))
        if page < total_pages:
            nav.append(InlineKeyboardButton(text="›", callback_data=f"adm_st_grp:{group}:{page + 1}"))
        buttons.append(nav)

    buttons.append([InlineKeyboardButton(text="‹ К разделам редактора", callback_data="adm:steps")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_step_actions_kb(step_key: str, has_photo: bool, group: str = "menu") -> InlineKeyboardMarkup:
    photo_btn = "🖼 Изменить / удалить изображение" if has_photo else "🖼 Прикрепить изображение"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Редактировать текст", callback_data=f"adm_st_txt:{step_key}")],
        [InlineKeyboardButton(text=photo_btn, callback_data=f"adm_st_pic:{step_key}")],
        [InlineKeyboardButton(text="👁 Предпросмотр экрана", callback_data=f"adm_st_prev:{step_key}")],
        [InlineKeyboardButton(text="‹ Назад к списку экранов", callback_data=f"adm_st_grp:{group}:1")]
    ])


def admin_edit_text_kb(step_key: str, current_text: str) -> InlineKeyboardMarkup:
    current_text = current_text or ""
    buttons = []
    if len(current_text.encode("utf-8")) <= 240:
        buttons.append([InlineKeyboardButton(text="⌨️ Вставить текущий текст в поле ввода", switch_inline_query_current_chat=current_text)])
    buttons.append([InlineKeyboardButton(text="↺ Восстановить стандартный текст", callback_data=f"adm_st_rst:{step_key}")])
    buttons.append([InlineKeyboardButton(text="‹ Назад к карточке шага", callback_data=f"adm_step:{step_key}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_edit_photo_kb(step_key: str, has_photo: bool) -> InlineKeyboardMarkup:
    buttons = []
    if has_photo:
        buttons.append([InlineKeyboardButton(text="🗑 Удалить текущее изображение", callback_data=f"adm_st_delpic:{step_key}")])
    buttons.append([InlineKeyboardButton(text="‹ Назад к карточке шага", callback_data=f"adm_step:{step_key}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_support_menu_kb(unread_count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🔴 Ожидают ответа ({unread_count})", callback_data="adm_sup:unread:1")],
        [InlineKeyboardButton(text="🗂 Архив всех диалогов", callback_data="adm_sup:all:1")],
        [InlineKeyboardButton(text="‹ В панель управления", callback_data="adm:home")]
    ])


def admin_threads_list_kb(threads: list[dict], mode: str, page: int = 1, page_size: int = 8) -> InlineKeyboardMarkup:
    total_pages = max(1, (len(threads) + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    current = threads[start:start + page_size]

    buttons = []
    for t in current:
        status_icon = "🔴" if t["has_unread"] else "⚪"
        br_short = "Пражская, 3" if t["branch_id"] == "prazhskaya" else "Ленингр., 7"
        name = t["client_name"] or f"ID {t['telegram_id']}"
        preview = (t["last_preview"] or "")[:18]
        buttons.append([
            InlineKeyboardButton(
                text=f"{status_icon} [{br_short}] {name}: {preview}",
                callback_data=f"adm_chat:{t['telegram_id']}"
            )
        ])

    if total_pages > 1:
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton(text="‹", callback_data=f"adm_sup:{mode}:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page} / {total_pages}", callback_data="ignore"))
        if page < total_pages:
            nav.append(InlineKeyboardButton(text="›", callback_data=f"adm_sup:{mode}:{page + 1}"))
        buttons.append(nav)

    buttons.append([InlineKeyboardButton(text="‹ Назад к разделу сообщений", callback_data="adm:support")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_quick_reply_kb(client_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Открыть диалог и ответить", callback_data=f"adm_chat:{client_id}")]
    ])


def admin_active_chat_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚪 Завершить диалог и выйти в меню", callback_data="adm:support")]
    ])


def admin_clear_cache_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Подтвердить очистку тестовых данных", callback_data="adm_cache:confirm")],
        [InlineKeyboardButton(text="‹ Отмена", callback_data="adm:home")]
    ])


def admin_manage_admins_kb(db_admins: list[dict], env_admins: list[int]) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text="＋ Добавить администратора", callback_data="adm_mgr:add")]]
    for a in db_admins:
        if a["telegram_id"] not in env_admins:
            buttons.append([InlineKeyboardButton(text=f"🗑 Удалить: {a['name']} ({a['telegram_id']})", callback_data=f"adm_mgr:del:{a['telegram_id']}")])
    buttons.append([InlineKeyboardButton(text="‹ В панель управления", callback_data="adm:home")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_cancel_input_kb(back_callback: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="‹ Отмена", callback_data=back_callback)]])


def admin_branches_filter_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📍 Sahar&Vosk · Пражская, 3", callback_data="adm_apps:prazhskaya")],
        [InlineKeyboardButton(text="📍 Sahar&Vosk · Ленинградская, 7", callback_data="adm_apps:leningradskaya")],
        [InlineKeyboardButton(text="📋 Все филиалы", callback_data="adm_apps:all")],
        [InlineKeyboardButton(text="‹ В панель управления", callback_data="adm:home")]
    ])


def admin_cancel_app_kb(app_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"✕ Отменить запись №{app_id}", callback_data=f"adm_cancel:{app_id}")]])


def admin_segments_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Все клиенты бота", callback_data="seg:all")],
        [
            InlineKeyboardButton(text="📍 Пражская, 3", callback_data="seg:branch_prazhskaya"),
            InlineKeyboardButton(text="📍 Ленинградская, 7", callback_data="seg:branch_leningradskaya")
        ],
        [InlineKeyboardButton(text="💤 Не посещали более 30 дней", callback_data="seg:inactive_30")],
        [InlineKeyboardButton(text="‹ В панель управления", callback_data="adm:home")]
    ])


def admin_broadcast_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Запустить рассылку", callback_data="adm_bc:send")],
        [InlineKeyboardButton(text="✕ Отмена", callback_data="adm:home")]
    ])


def admin_repeat_settings_kb(categories: dict, settings: dict[str, int]) -> InlineKeyboardMarkup:
    buttons = []
    for cat_id, title in categories.items():
        days = settings.get(cat_id, 28)
        buttons.append([InlineKeyboardButton(text=f"{title} · {days} дн.", callback_data=f"adm_rep:{cat_id}")])
    buttons.append([InlineKeyboardButton(text="‹ В панель управления", callback_data="adm:home")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)