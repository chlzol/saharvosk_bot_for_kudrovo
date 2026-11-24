"""
Обработчик входящих вебхуков от YCLIENTS.

Подхватывает записи, созданные по телефону, через Яндекс.Карты
или вручную администратором в журнале YCLIENTS, и синхронизирует
их с локальной базой бота Sahar&Vosk.
"""

import asyncio
import logging
import re
from datetime import datetime, date, timedelta, timezone

import aiosqlite
from aiogram import Bot

import config
from config import YCLIENTS_COMPANY_TO_BRANCH
from database import db
from services import booking_service as bs
from services import calendar_helper as ch
from services.scheduler import schedule_appointment_reminders, remove_appointment_reminders
from keyboards import inline as ikb

logger = logging.getLogger(__name__)

MSK_TZ = timezone(timedelta(hours=3))

# Локи для защиты от гонки по yclients_record_id
_dedup_locks: dict[str, asyncio.Lock] = {}
_dedup_waiters: dict[str, int] = {}


def normalize_phone(raw: str | None) -> str | None:
    """
    Нормализация телефонного номера:
    очищает номер от всех нецифровых символов и возвращает последние 10 цифр
    (при условии, что суммарное количество цифр >= 10).
    Возвращает None, если цифр < 10 или номер пуст.
    """
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) < 10:
        return None
    return digits[-10:]


async def find_user_by_phone(phone: str | None) -> dict | None:
    """
    Ищет пользователя в таблице users по последним 10 цифрам телефона.
    Если с одним номером связано несколько записей, детерминированно выбирает
    самый последний / активный профиль пользователя.
    """
    target_last10 = normalize_phone(phone)
    if not target_last10:
        return None
    async with db._connect() as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("""
            SELECT * FROM users
            WHERE phone IS NOT NULL AND phone != ''
            ORDER BY COALESCE(last_visit_date, created_at, '1970-01-01') DESC, telegram_id DESC
        """)
        rows = await cur.fetchall()
        for row in rows:
            user_phone = normalize_phone(row["phone"])
            if user_phone and user_phone == target_last10:
                return dict(row)
    return None


def _resolve_branch_id(company_id: int | str | None) -> str | None:
    """Определяет branch_id (prazhskaya / leningradskaya) по company_id из YCLIENTS."""
    if not company_id:
        return None
    try:
        cid = int(str(company_id).strip())
    except (TypeError, ValueError):
        return None
    return YCLIENTS_COMPANY_TO_BRANCH.get(cid)


def _parse_visit_datetime(raw_datetime: any) -> tuple[str, str] | None:
    """
    Парсит дату и время из форматов YCLIENTS с корректным приведением к Europe/Moscow:
    - ISO с таймзоной: '2026-11-25T14:00:00+03:00', '2026-11-25T11:00:00Z', '2026-11-25T11:00:00+00:00'
    - Строка без таймзоны (считается временем в МСК): '2026-11-25 14:00:00', '2026-11-25T14:00'
    Возвращает (visit_date, visit_time) в виде ('YYYY-MM-DD', 'HH:MM') или None.
    """
    if not raw_datetime:
        return None
    raw = str(raw_datetime).strip()
    try:
        # ISO 8601 (включая Z и смещения) в Python 3.11+
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is not None:
            # Приводим к Europe/Moscow (UTC+3)
            dt = dt.astimezone(MSK_TZ)
        return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
    except Exception:
        try:
            clean = raw.replace("T", " ")
            if "." in clean:
                clean = clean.split(".")[0]
            dt = datetime.strptime(clean[:19], "%Y-%m-%d %H:%M:%S")
            return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
        except Exception:
            try:
                dt = datetime.strptime(raw[:16].replace("T", " "), "%Y-%m-%d %H:%M")
                return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
            except Exception:
                return None


def _parse_duration(val: any) -> int:
    """
    Парсит длительность услуги/сеанса из YCLIENTS.
    В YCLIENTS seance_length и length приходят в секундах (например, 1800, 3600),
    тогда как в БД бота длительность хранится в минутах.
    """
    if not val:
        return 0
    try:
        d = int(float(val))
        if d <= 0:
            return 0
        if d >= 300:
            return max(1, d // 60)
        if d >= 60 and d % 60 == 0:
            return max(1, d // 60)
        return d
    except (ValueError, TypeError):
        return 0


def _parse_cost(val: any) -> int:
    """Парсит стоимость услуги в целое число рублей."""
    if val is None or val == "":
        return 0
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return 0


def _extract_record_data(payload: dict) -> dict | None:
    """
    Извлекает и валидирует данные записи из входящего вебхука YCLIENTS.
    Устойчив к отсутствующим, пустым и нестандартным полям.
    """
    data = payload.get("data")
    if data is None or not isinstance(data, (dict, list)):
        data = payload
    if isinstance(data, list):
        data = data[0] if data and isinstance(data[0], dict) else {}

    record_id = data.get("id") or payload.get("id")
    company_id = data.get("company_id") or payload.get("company_id")
    if not record_id or not company_id:
        return None

    # Клиент
    client = data.get("client")
    if not isinstance(client, dict):
        client = {}
    client_phone = client.get("phone") or ""
    client_name = client.get("name") or data.get("client_name") or "Гость"

    # Дата и время визита
    raw_dt = data.get("datetime") or data.get("date")
    parsed_dt = _parse_visit_datetime(raw_dt)
    deleted = bool(data.get("deleted", False)) or bool(payload.get("deleted", False))
    status = payload.get("status", "")

    # Если это не удаление — дата и время визита строго обязательны
    if not (deleted or status == "delete") and not parsed_dt:
        return None

    visit_date, visit_time = parsed_dt if parsed_dt else ("", "")

    # Услуги
    services = data.get("services")
    if not isinstance(services, list):
        services = []

    service_titles = []
    service_ids = []
    total_price = 0
    cat_id = ""
    has_services_costs = False

    for s in services:
        if not isinstance(s, dict):
            continue
        title = str(s.get("title") or "").strip()
        if title:
            service_titles.append(title)
        sid = s.get("id")
        if sid is not None and str(sid).strip():
            service_ids.append(str(sid).strip())

        # Приоритет стоимости: cost_to_pay (к оплате с учетом скидок) -> cost -> first_cost -> price
        s_cost = None
        for k in ("cost_to_pay", "cost", "first_cost", "price_max", "price_min"):
            val = s.get(k)
            if val is not None and val != "":
                s_cost = _parse_cost(val)
                break
        if s_cost is not None:
            total_price += s_cost
            has_services_costs = True

        if not cat_id and s.get("category_id"):
            cat_id = str(s.get("category_id")).strip()

    # Fallback если услуги не указаны в массиве, а пришли в корне data
    has_root_cost = False
    if not has_services_costs:
        for k in ("cost_to_pay", "cost", "first_cost", "price"):
            val = data.get(k)
            if val is not None and val != "":
                total_price = _parse_cost(val)
                has_root_cost = True
                break

    has_explicit_cost = has_services_costs or has_root_cost

    # Длительность: в YCLIENTS seance_length в корне задает общую длительность слота в журнале
    root_dur = _parse_duration(data.get("seance_length") or data.get("length"))
    services_dur = sum(_parse_duration(s.get("seance_length") or s.get("length")) for s in services if isinstance(s, dict))
    if root_dur > 0:
        total_duration = root_dur
    elif services_dur > 0:
        total_duration = services_dur
    else:
        total_duration = 30

    has_explicit_services = bool(service_titles or data.get("service_title"))
    services_list = ", ".join(service_titles) if service_titles else str(data.get("service_title") or "Услуга из CRM")
    service_id = ",".join(service_ids) if service_ids else str(record_id)

    # Мастер / специалист
    staff = data.get("staff")
    if not isinstance(staff, dict):
        staff = {}
    has_explicit_staff = bool((staff and (staff.get("name") or staff.get("id"))) or data.get("staff_name") or data.get("staff_id"))
    master_id = str(staff.get("id") or data.get("staff_id") or "any")
    master_name = str(staff.get("name") or data.get("staff_name") or "Любой специалист").strip()
    if not master_name:
        master_name = "Любой специалист"

    # Удаление и attendance
    try:
        attendance = int(data.get("attendance", 0))
    except (ValueError, TypeError):
        attendance = 0

    return {
        "yclients_record_id": str(record_id),
        "company_id": company_id,
        "client_phone": client_phone,
        "client_name": str(client_name).strip() or "Гость",
        "visit_date": visit_date,
        "visit_time": visit_time,
        "services_list": services_list,
        "service_title": services_list,
        "service_id": service_id,
        "cat_id": cat_id or "hair_removal",
        "total_price": total_price,
        "total_duration": total_duration,
        "master_id": master_id,
        "master_name": master_name,
        "has_explicit_services": has_explicit_services,
        "has_explicit_staff": has_explicit_staff,
        "has_explicit_cost": has_explicit_cost,
        "deleted": deleted,
        "attendance": attendance,
    }


async def _get_appointment_by_yclients_id(yclients_record_id: str | int) -> dict | None:
    """Ищет запись в таблице appointments по yclients_record_id."""
    async with db._connect() as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM appointments WHERE yclients_record_id = ?",
            (str(yclients_record_id),),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def _find_duplicate_appointment(
    telegram_id: int | None,
    branch_id: str,
    visit_date: str,
    visit_time: str,
    client_phone: str | None = None,
) -> dict | None:
    """
    Ищет существующую активную запись клиента на это же время.
    Для MAX-only записей (telegram_id IS NULL) избегает ловушки `NULL = NULL` и ищет по client_phone.
    Также находит существующую MAX-only запись, если клиент позже зарегистрировался в Telegram.
    """
    norm_time = db.normalize_time_str(visit_time)
    norm_phone = db.normalize_client_phone(client_phone)
    async with db._connect() as conn:
        conn.row_factory = aiosqlite.Row
        if telegram_id is not None and int(telegram_id) > 0:
            cur = await conn.execute(
                """SELECT * FROM appointments
                   WHERE telegram_id = ? AND branch_id = ?
                     AND visit_date = ? AND substr('0' || visit_time, -5) = ?
                     AND status = 'active'
                   LIMIT 1""",
                (int(telegram_id), branch_id, visit_date, norm_time),
            )
            row = await cur.fetchone()
            if row:
                return dict(row)
        if norm_phone:
            cur = await conn.execute(
                """SELECT * FROM appointments
                   WHERE client_phone = ? AND branch_id = ?
                     AND visit_date = ? AND substr('0' || visit_time, -5) = ?
                     AND status = 'active'
                   LIMIT 1""",
                (norm_phone, branch_id, visit_date, norm_time),
            )
            row = await cur.fetchone()
            return dict(row) if row else None
        return None


def _is_past_datetime(visit_date: str, visit_time: str) -> bool:
    """Проверяет, прошло ли время визита относительно Московского времени."""
    now = db.now_msk()
    try:
        norm_time = db.normalize_time_str(visit_time)
        v_dt = datetime.fromisoformat(f"{visit_date}T{norm_time}:00")
        return v_dt < now
    except Exception:
        return False


def _get_branch_title(branch_id: str) -> str:
    branch = bs.get_branches().get(branch_id, {})
    return branch.get("title", "Кудрово")


def _get_branch_address(branch_id: str) -> str:
    branch = bs.get_branches().get(branch_id, {})
    return branch.get("address", "г. Кудрово")


async def handle_webhook_event(payload: dict, bot: Bot):
    """
    Главный диспетчер входящих вебхуков YCLIENTS.
    Запускается в фоновой задаче через asyncio.create_task.
    """
    resource = payload.get("resource", "")
    status = payload.get("status", "")

    # Принимаем только события типа "record" / "records"
    if resource not in ("record", "records"):
        logger.info("Webhook: событие с resource='%s' пропущено", resource)
        return

    record_data = _extract_record_data(payload)
    if not record_data:
        logger.warning("Webhook: невозможно извлечь корректные данные записи")
        return

    branch_id = _resolve_branch_id(record_data["company_id"])
    if not branch_id:
        logger.info(
            "Webhook: неизвестный company_id=%s, филиал не относится к Sahar&Vosk Кудрово",
            record_data["company_id"],
        )
        return

    yclients_record_id = record_data["yclients_record_id"]

    # Антидубль и защита от race conditions через per-record блокировку
    lock_key = f"wh_{yclients_record_id}"
    lock = _dedup_locks.setdefault(lock_key, asyncio.Lock())
    _dedup_waiters[lock_key] = _dedup_waiters.get(lock_key, 0) + 1

    try:
        async with lock:
            if record_data["deleted"] or record_data["attendance"] == -1 or status == "delete":
                await _handle_delete(record_data, branch_id, bot)
            elif status == "create":
                await _handle_create(record_data, branch_id, bot)
            elif status == "update":
                await _handle_update(record_data, branch_id, bot)
            else:
                existing = await _get_appointment_by_yclients_id(yclients_record_id)
                if existing:
                    await _handle_update(record_data, branch_id, bot)
                else:
                    await _handle_create(record_data, branch_id, bot)
    finally:
        # Корректная очистка лока: удаляем только когда все задачи на этом ключе завершились
        _dedup_waiters[lock_key] = max(0, _dedup_waiters.get(lock_key, 1) - 1)
        if _dedup_waiters.get(lock_key, 0) <= 0:
            _dedup_waiters.pop(lock_key, None)
            _dedup_locks.pop(lock_key, None)


async def _handle_create(record_data: dict, branch_id: str, bot: Bot):
    """Обработка события status == 'create'."""
    yclients_record_id = record_data["yclients_record_id"]

    # Задержка 2.5 сек для защиты от гонки с записью, создаваемой самим ботом
    await asyncio.sleep(2.5)

    # 1. Если запись помечена на удаление или клиент не пришёл — игнорируем
    if record_data["deleted"] or record_data["attendance"] == -1:
        logger.info("Webhook CREATE: запись #%s отменена/удалена, игнорируем", yclients_record_id)
        return

    # 2. Если визит уже в прошлом — игнорируем
    if _is_past_datetime(record_data["visit_date"], record_data["visit_time"]):
        logger.info(
            "Webhook CREATE: визит #%s (%s %s) уже в прошлом, игнорируем",
            yclients_record_id,
            record_data["visit_date"],
            record_data["visit_time"],
        )
        return

    # 3. Проверяем, есть ли уже запись по yclients_record_id
    existing = await _get_appointment_by_yclients_id(yclients_record_id)
    if existing:
        logger.info(
            "Webhook CREATE: запись yclients_record_id=%s уже существует в БД (#%s), дедупликация",
            yclients_record_id,
            existing["id"],
        )
        return

    # 4. Нормализация телефона и поиск пользователя в Telegram-боте
    phone_last10 = normalize_phone(record_data["client_phone"])
    client_phone_e164 = db.normalize_client_phone(record_data["client_phone"])
    if not phone_last10:
        logger.info(
            "Webhook CREATE: не распознан телефон '%s' для записи #%s",
            record_data["client_phone"],
            yclients_record_id,
        )
        return

    user = await find_user_by_phone(phone_last10)
    if not user:
        if not config.MAX_ENABLED or not client_phone_e164:
            logger.info(
                "Webhook CREATE: клиент с телефоном ***%s не зарегистрирован в боте Sahar&Vosk",
                phone_last10[-4:],
            )
            return

        # Клиент без Telegram, но включён мессенджер MAX: сохраняем запись только для напоминаний (без приветственного сообщения)
        dup_max = await _find_duplicate_appointment(
            None,
            branch_id,
            record_data["visit_date"],
            record_data["visit_time"],
            client_phone=client_phone_e164,
        )
        if dup_max:
            logger.info(
                "Webhook CREATE (MAX): обнаружена существующая запись #%s на то же время для ***%s",
                dup_max["id"],
                phone_last10[-4:],
            )
            if not dup_max.get("yclients_record_id"):
                async with db._connect() as conn:
                    await conn.execute(
                        "UPDATE appointments SET yclients_record_id = ? WHERE id = ?",
                        (yclients_record_id, dup_max["id"]),
                    )
                    await conn.commit()
            return

        branch_title = _get_branch_title(branch_id)
        max_app_data = {
            "telegram_id": None,
            "client_name": record_data["client_name"] or "Гость",
            "phone": client_phone_e164,
            "client_phone": client_phone_e164,
            "branch_id": branch_id,
            "branch_title": branch_title,
            "cat_id": record_data["cat_id"],
            "service_id": record_data["service_id"],
            "service_title": record_data["services_list"],
            "price": record_data["total_price"],
            "duration": record_data["total_duration"],
            "master_id": record_data["master_id"],
            "master_name": record_data["master_name"],
            "visit_date": record_data["visit_date"],
            "visit_time": record_data["visit_time"],
            "yclients_record_id": yclients_record_id,
        }
        appointment_id = await db.create_appointment(max_app_data)
        logger.info(
            "Webhook CREATE (MAX): сохранена запись #%s (YCLIENTS #%s) без Telegram для ***%s",
            appointment_id,
            yclients_record_id,
            phone_last10[-4:],
        )
        try:
            await schedule_appointment_reminders(bot, appointment_id)
        except Exception as e:
            logger.error("Webhook (MAX): ошибка постановки напоминаний для записи #%s: %s", appointment_id, e)
        return

    telegram_id = user["telegram_id"]

    # 5. Проверка дубля по (telegram_id, branch_id, visit_date, visit_time) или client_phone
    dup = await _find_duplicate_appointment(
        telegram_id,
        branch_id,
        record_data["visit_date"],
        record_data["visit_time"],
        client_phone=client_phone_e164,
    )
    if dup:
        logger.info(
            "Webhook CREATE: обнаружена существующая запись #%s на то же время у user=%s",
            dup["id"],
            telegram_id,
        )
        dup_updates = {}
        if not dup.get("yclients_record_id"):
            dup_updates["yclients_record_id"] = yclients_record_id
        if not dup.get("telegram_id"):
            dup_updates["telegram_id"] = telegram_id
        if dup_updates:
            async with db._connect() as conn:
                set_clause = ", ".join(f"{k} = ?" for k in dup_updates)
                vals = list(dup_updates.values()) + [dup["id"]]
                await conn.execute(f"UPDATE appointments SET {set_clause} WHERE id = ?", vals)
                await conn.commit()
            if "telegram_id" in dup_updates:
                try:
                    await schedule_appointment_reminders(bot, dup["id"])
                except Exception as e:
                    logger.error("Webhook: ошибка обновления напоминаний #%s: %s", dup["id"], e)
        return

    # 6. Сохраняем новую запись в БД
    branch_title = _get_branch_title(branch_id)
    branch_address = _get_branch_address(branch_id)
    user_phone = user.get("phone") or record_data["client_phone"]
    client_phone_norm = db.normalize_client_phone(user_phone) or client_phone_e164

    app_data = {
        "telegram_id": telegram_id,
        "client_name": record_data["client_name"] or user.get("real_name") or user.get("first_name", "Гость"),
        "phone": user_phone,
        "client_phone": client_phone_norm or None,
        "branch_id": branch_id,
        "branch_title": branch_title,
        "cat_id": record_data["cat_id"],
        "service_id": record_data["service_id"],
        "service_title": record_data["services_list"],
        "price": record_data["total_price"],
        "duration": record_data["total_duration"],
        "master_id": record_data["master_id"],
        "master_name": record_data["master_name"],
        "visit_date": record_data["visit_date"],
        "visit_time": record_data["visit_time"],
        "yclients_record_id": yclients_record_id,
    }
    appointment_id = await db.create_appointment(app_data)
    logger.info(
        "Webhook CREATE: сохранена внешняя запись #%s (YCLIENTS #%s) для user=%s",
        appointment_id,
        yclients_record_id,
        telegram_id,
    )

    # 7. Планируем напоминания в APScheduler
    try:
        await schedule_appointment_reminders(bot, appointment_id)
    except Exception as e:
        logger.error("Webhook: ошибка постановки напоминаний для записи #%s: %s", appointment_id, e)

    # 8. Отправляем карточку визита гостю
    try:
        formatted_date = date.fromisoformat(record_data["visit_date"]).strftime("%d.%m.%Y")
        total_price_str = (
            f'{record_data["total_price"]:,} ₽'.replace(",", " ")
            if record_data["total_price"]
            else "уточняется при визите"
        )

        google_cal_url = ch.generate_google_calendar_url(
            title=f"Sahar&Vosk — {record_data['services_list']}",
            visit_date=record_data["visit_date"],
            visit_time=record_data["visit_time"],
            duration_min=record_data["total_duration"],
            location=branch_address,
            description=(
                f"Специалист: {record_data['master_name']}\n"
                f"Студия: Sahar&Vosk · {branch_title}\n"
                f"Адрес: {branch_address}\n"
                f"Телефон: +7 (812) 988-98-53"
            ),
        )

        kb = ikb.appointment_manage_kb(
            appointment_id,
            branch_id=branch_id,
            google_cal_url=google_cal_url,
        )

        await db.render_step_to_chat(
            bot,
            chat_id=telegram_id,
            step_key="webhook_record_created",
            reply_markup=kb,
            appointment_id=appointment_id,
            branch_title=db.escape_md(branch_title),
            services_list=db.escape_md(record_data["services_list"]),
            service_title=db.escape_md(record_data["services_list"]),
            master_name=db.escape_md(record_data["master_name"]),
            visit_date=formatted_date,
            formatted_date=formatted_date,
            visit_time=record_data["visit_time"],
            total_price=total_price_str,
            price=total_price_str,
            total_duration=record_data["total_duration"],
            duration=record_data["total_duration"],
        )
    except Exception as e:
        logger.warning(
            "Webhook: не удалось доставить сообщение гостю %s (возможно, бот заблокирован): %s",
            telegram_id,
            e,
        )


async def _handle_update(record_data: dict, branch_id: str, bot: Bot):
    """Обработка события status == 'update'."""
    yclients_record_id = record_data["yclients_record_id"]

    # 1. Если в update пришло deleted или attendance == -1 — это отмена
    if record_data["deleted"] or record_data["attendance"] == -1:
        await _handle_delete(record_data, branch_id, bot)
        return

    existing = await _get_appointment_by_yclients_id(yclients_record_id)
    if not existing:
        # Если записи ещё нет в БД, но телефон найден — подхватываем как новую
        if not _is_past_datetime(record_data["visit_date"], record_data["visit_time"]):
            logger.info("Webhook UPDATE: запись #%s не найдена в БД, подхватываем как новую", yclients_record_id)
            await _handle_create(record_data, branch_id, bot)
        return

    # Если запись уже отменена или завершена
    if existing["status"] in ("cancelled", "rescheduled", "completed"):
        logger.info("Webhook UPDATE: запись #%s имеет статус '%s', пропускаем", existing["id"], existing["status"])
        return

    norm_rec_time = db.normalize_time_str(record_data["visit_time"])
    record_data["visit_time"] = norm_rec_time
    norm_exist_time = db.normalize_time_str(existing["visit_time"])

    incoming_phone = record_data.get("client_phone", "").strip()
    existing_phone = (existing.get("phone") or existing.get("client_phone") or "").strip()
    phone_changed = bool(incoming_phone) and (normalize_phone(incoming_phone) != normalize_phone(existing_phone))

    existing_tg = existing.get("telegram_id")
    has_existing_tg = bool(existing_tg is not None and int(existing_tg) > 0)
    attach_tg_user = None
    if not phone_changed and not has_existing_tg:
        lookup_phone = incoming_phone or existing_phone
        if lookup_phone:
            attach_tg_user = await find_user_by_phone(lookup_phone)

    # Проверяем изменения ключевых полей
    date_changed = record_data["visit_date"] != existing["visit_date"]
    time_changed = norm_rec_time != norm_exist_time
    attendance_confirmed = record_data["attendance"] == 2 and not existing.get("confirmed")
    master_changed = (
        record_data["has_explicit_staff"]
        and bool(record_data["master_name"])
        and record_data["master_name"] != existing["master_name"]
        and record_data["master_name"] != "Любой специалист"
    )
    service_changed = (
        record_data["has_explicit_services"]
        and bool(record_data["services_list"])
        and record_data["services_list"] != existing["service_title"]
        and record_data["services_list"] != "Услуга из CRM"
    )
    price_changed = record_data["has_explicit_cost"] and record_data["total_price"] != existing.get("price", 0)
    duration_changed = bool(record_data["total_duration"]) and record_data["total_duration"] != existing.get("duration", 30)

    # Если ничего не поменялось
    if not (
        date_changed
        or time_changed
        or master_changed
        or service_changed
        or price_changed
        or attendance_confirmed
        or phone_changed
        or duration_changed
        or attach_tg_user
    ):
        logger.info("Webhook UPDATE: запись #%s без изменений расписания", existing["id"])
        return

    # Если перенос на дату в прошлом — не применяем
    if (date_changed or time_changed) and _is_past_datetime(record_data["visit_date"], record_data["visit_time"]):
        logger.info("Webhook UPDATE: новая дата визита #%s уже в прошлом, пропускаем", existing["id"])
        return

    # Обновляем БД
    update_fields = {}
    reassigned_reminder_channel = False
    if phone_changed:
        norm_incoming_phone = db.normalize_client_phone(incoming_phone)
        new_user = await find_user_by_phone(incoming_phone)
        if new_user:
            update_fields["telegram_id"] = new_user["telegram_id"]
            update_fields["client_name"] = record_data["client_name"] or new_user.get("real_name") or "Гость"
            update_fields["phone"] = incoming_phone
            update_fields["client_phone"] = db.normalize_client_phone(new_user.get("phone") or incoming_phone) or norm_incoming_phone or None
            existing["telegram_id"] = new_user["telegram_id"]
            existing["phone"] = incoming_phone
            existing["client_phone"] = update_fields["client_phone"]
            reassigned_reminder_channel = True
            logger.info("Webhook UPDATE: запись #%s переписана на клиента telegram_id=%s (тел %s)", existing["id"], new_user["telegram_id"], incoming_phone)
        else:
            update_fields["telegram_id"] = None
            update_fields["client_name"] = record_data["client_name"]
            update_fields["phone"] = incoming_phone
            update_fields["client_phone"] = norm_incoming_phone or None
            existing["telegram_id"] = None
            existing["phone"] = incoming_phone
            existing["client_phone"] = update_fields["client_phone"]
            remove_appointment_reminders(existing["id"])
            if config.MAX_ENABLED and norm_incoming_phone:
                reassigned_reminder_channel = True
                logger.info("Webhook UPDATE (MAX): запись #%s переписана на телефон %s, запланируем напоминания MAX", existing["id"], norm_incoming_phone)
            else:
                logger.info("Webhook UPDATE: запись #%s переписана на незарегистрированный телефон %s, напоминания сняты", existing["id"], incoming_phone)
    else:
        if attach_tg_user:
            update_fields["telegram_id"] = attach_tg_user["telegram_id"]
            if record_data["client_name"] and record_data["client_name"] != "Гость":
                update_fields["client_name"] = record_data["client_name"]
            existing["telegram_id"] = attach_tg_user["telegram_id"]
            reassigned_reminder_channel = True
            logger.info(
                "Webhook UPDATE: MAX-only запись #%s привязана к новому Telegram-профилю user=%s",
                existing["id"],
                attach_tg_user["telegram_id"],
            )
        if not existing.get("client_phone"):
            backfill_phone = db.normalize_client_phone(incoming_phone or existing.get("phone"))
            if backfill_phone:
                update_fields["client_phone"] = backfill_phone
                existing["client_phone"] = backfill_phone

    if attendance_confirmed:
        update_fields["confirmed"] = 1
        logger.info("Webhook UPDATE: запись #%s отмечена подтверждённой в CRM", existing["id"])
    if date_changed:
        update_fields["visit_date"] = record_data["visit_date"]
    if time_changed:
        update_fields["visit_time"] = norm_rec_time
    if master_changed:
        update_fields["master_name"] = record_data["master_name"]
        update_fields["master_id"] = record_data["master_id"]
    if service_changed:
        update_fields["service_title"] = record_data["services_list"]
        update_fields["service_id"] = record_data["service_id"]
    if price_changed:
        update_fields["price"] = record_data["total_price"]
    if duration_changed:
        update_fields["duration"] = record_data["total_duration"]

    if not update_fields:
        logger.info("Webhook UPDATE: запись #%s без обновляемых полей в БД", existing["id"])
        return

    async with db._connect() as conn:
        set_clause = ", ".join(f"{k} = ?" for k in update_fields)
        vals = list(update_fields.values()) + [existing["id"]]
        await conn.execute(f"UPDATE appointments SET {set_clause} WHERE id = ?", vals)
        await conn.commit()

    logger.info("Webhook UPDATE: запись #%s обновлена: %s", existing["id"], list(update_fields.keys()))

    # Если изменились дата, время или канал доставки — пересоздаём таймеры напоминаний
    has_tg = bool(existing.get("telegram_id") and int(existing["telegram_id"]) > 0)
    has_max = bool(config.MAX_ENABLED and db.normalize_client_phone(existing.get("client_phone") or existing.get("phone")))
    if (date_changed or time_changed or reassigned_reminder_channel) and (has_tg or has_max):
        try:
            await schedule_appointment_reminders(bot, existing["id"])
        except Exception as e:
            logger.error("Webhook: ошибка пересоздания напоминаний #%s: %s", existing["id"], e)

    # Уведомляем клиента, только если поменялись дата, время или специалист
    if (date_changed or time_changed or master_changed) and has_tg:
        telegram_id = int(existing["telegram_id"])
        branch_title = _get_branch_title(branch_id)
        new_date = record_data["visit_date"] if date_changed else existing["visit_date"]
        new_time = record_data["visit_time"] if time_changed else existing["visit_time"]
        new_master = record_data["master_name"] if master_changed else existing["master_name"]
        new_services = record_data["services_list"] if service_changed else existing["service_title"]
        formatted_date = date.fromisoformat(new_date).strftime("%d.%m.%Y")

        try:
            branch_address = _get_branch_address(branch_id)
            google_cal_url = ch.generate_google_calendar_url(
                title=f"Sahar&Vosk — {new_services}",
                visit_date=new_date,
                visit_time=new_time,
                duration_min=existing.get("duration", 30),
                location=branch_address,
                description=(
                    f"Специалист: {new_master}\n"
                    f"Студия: Sahar&Vosk · {branch_title}\n"
                    f"Адрес: {branch_address}\n"
                    f"Телефон: +7 (812) 988-98-53"
                ),
            )

            kb = ikb.appointment_manage_kb(
                existing["id"],
                branch_id=branch_id,
                google_cal_url=google_cal_url,
            )

            await db.render_step_to_chat(
                bot,
                chat_id=telegram_id,
                step_key="webhook_record_updated",
                reply_markup=kb,
                appointment_id=existing["id"],
                branch_title=db.escape_md(branch_title),
                services_list=db.escape_md(new_services),
                service_title=db.escape_md(new_services),
                master_name=db.escape_md(new_master),
                visit_date=formatted_date,
                formatted_date=formatted_date,
                visit_time=new_time,
            )
        except Exception as e:
            logger.warning("Webhook: ошибка отправки уведомления об обновлении user=%s: %s", telegram_id, e)


async def _handle_delete(record_data: dict, branch_id: str, bot: Bot):
    """
    Обработка события status == 'delete' или отмены визита.
    Если запись уже имеет статус cancelled или rescheduled (например, отменена
    или перенесена самим клиентом через бота) — уведомление НЕ отправляется (защита от эхо-петель).
    """
    yclients_record_id = record_data["yclients_record_id"]

    existing = await _get_appointment_by_yclients_id(yclients_record_id)
    if not existing:
        logger.info("Webhook DELETE: запись yclients_id=%s не найдена в БД, пропускаем", yclients_record_id)
        return

    # Защита от эхо-петель: если клиент сам отменил или перенёс через бота
    if existing["status"] in ("cancelled", "rescheduled"):
        logger.info(
            "Webhook DELETE: запись #%s уже имеет статус '%s', пропускаем повторное уведомление",
            existing["id"],
            existing["status"],
        )
        return

    # Помечаем запись как отменённую
    async with db._connect() as conn:
        await conn.execute("UPDATE appointments SET status = 'cancelled' WHERE id = ?", (existing["id"],))
        await conn.commit()

    # Снимаем все напоминания в планировщике
    remove_appointment_reminders(existing["id"])
    logger.info("Webhook DELETE: запись #%s успешно отменена", existing["id"])

    # Уведомляем гостя
    telegram_id = existing.get("telegram_id")
    if not telegram_id or int(telegram_id) <= 0:
        return
    branch_title = _get_branch_title(branch_id)
    formatted_date = date.fromisoformat(existing["visit_date"]).strftime("%d.%m.%Y")

    try:
        await db.render_step_to_chat(
            bot,
            chat_id=int(telegram_id),
            step_key="webhook_record_cancelled",
            reply_markup=ikb.repeat_booking_kb(has_past_visit=True),
            appointment_id=existing["id"],
            branch_title=db.escape_md(branch_title),
            services_list=db.escape_md(existing["service_title"]),
            service_title=db.escape_md(existing["service_title"]),
            master_name=db.escape_md(existing["master_name"]),
            visit_date=formatted_date,
            formatted_date=formatted_date,
            visit_time=existing["visit_time"],
        )
    except Exception as e:
        logger.warning("Webhook: ошибка уведомления об отмене user=%s: %s", telegram_id, e)
