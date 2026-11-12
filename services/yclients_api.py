import logging
from datetime import datetime, date, timedelta, timezone
import aiohttp
from config import (
    USE_YCLIENTS_API,
    YCLIENTS_PARTNER_TOKEN,
    YCLIENTS_USER_TOKEN,
    YCLIENTS_COMPANIES
)

BASE_URL = "https://api.yclients.com/api/v1"
MSK_TZ = timezone(timedelta(hours=3))

_http_session: aiohttp.ClientSession | None = None


def now_msk() -> datetime:
    """Возвращает текущее время по Санкт-Петербургу / Москве (UTC+3) без таймзоны для корректного сравнения."""
    return datetime.now(MSK_TZ).replace(tzinfo=None)


async def get_http_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        timeout = aiohttp.ClientTimeout(total=12)
        connector = aiohttp.TCPConnector(limit=100, ttl_dns_cache=300)
        _http_session = aiohttp.ClientSession(timeout=timeout, connector=connector)
    return _http_session


async def close_http_session():
    global _http_session
    if _http_session and not _http_session.closed:
        await _http_session.close()


def _get_headers() -> dict:
    auth = f"Bearer {YCLIENTS_PARTNER_TOKEN}"
    if YCLIENTS_USER_TOKEN:
        auth += f", User {YCLIENTS_USER_TOKEN}"
    return {
        "Accept": "application/vnd.yclients.v2+json",
        "Content-Type": "application/json",
        "Authorization": auth
    }


def _normalize_service_ids(service_ids: str | list[str]) -> list[str]:
    if isinstance(service_ids, str):
        return [s.strip() for s in service_ids.split(",") if s.strip()]
    return [str(s).strip() for s in service_ids if str(s).strip()]


async def fetch_masters_for_services(branch_id: str, service_ids: str | list[str]) -> list[dict] | None:
    if not USE_YCLIENTS_API:
        return None

    company_id = YCLIENTS_COMPANIES.get(branch_id, 0)
    srv_list = _normalize_service_ids(service_ids)
    if not company_id or not srv_list:
        return None

    url = f"{BASE_URL}/book_staff/{company_id}"
    params = [("service_ids[]", sid) for sid in srv_list]

    try:
        session = await get_http_session()
        async with session.get(url, headers=_get_headers(), params=params) as resp:
            if resp.status == 200:
                resp_json = await resp.json()
                staff_list = resp_json if isinstance(resp_json, list) else resp_json.get("data", [])
                result = []
                for st in staff_list:
                    if not isinstance(st, dict):
                        continue
                    if not st.get("bookable", True) or st.get("fired") or st.get("is_deleted"):
                        continue
                    result.append({
                        "id": str(st["id"]),
                        "name": st.get("name", "Специалист"),
                        "specialization": st.get("specialization", "")
                    })
                return result
    except Exception as e:
        logging.error(f"Ошибка получения мастеров для услуг {srv_list}: {e}")

    return None


async def fetch_available_dates(branch_id: str, master_id: str, service_ids: str | list[str]) -> list[str] | None:
    if not USE_YCLIENTS_API:
        return None

    company_id = YCLIENTS_COMPANIES.get(branch_id, 0)
    if not company_id:
        return None

    url = f"{BASE_URL}/book_dates/{company_id}"
    srv_list = _normalize_service_ids(service_ids)
    params = [("service_ids[]", sid) for sid in srv_list]
    if master_id and master_id != "any":
        params.append(("staff_id", str(master_id)))

    try:
        session = await get_http_session()
        async with session.get(url, headers=_get_headers(), params=params) as resp:
            if resp.status == 200:
                resp_json = await resp.json()
                data = resp_json.get("data", resp_json) if isinstance(resp_json, dict) else {}
                booking_dates = data.get("booking_dates", [])
                return [str(d) for d in booking_dates]
            logging.warning(f"YCLIENTS book_dates ({resp.status}): {await resp.text()}")
    except Exception as e:
        logging.error(f"Сетевая ошибка при запросе рабочих дат YCLIENTS: {e}")

    return None


async def _fetch_available_times_raw(
    branch_id: str,
    master_id: str,
    service_ids: str | list[str],
    date_str: str
) -> list[str] | None:
    """Возвращает список свободных окон или None в случае сетевого сбоя."""
    if not USE_YCLIENTS_API:
        return []

    company_id = YCLIENTS_COMPANIES.get(branch_id, 0)
    if not company_id:
        return None

    staff_param = 0 if master_id == "any" else master_id
    url = f"{BASE_URL}/book_times/{company_id}/{staff_param}/{date_str}"
    srv_list = _normalize_service_ids(service_ids)
    params = [("service_ids[]", sid) for sid in srv_list]

    try:
        session = await get_http_session()
        async with session.get(url, headers=_get_headers(), params=params) as resp:
            if resp.status == 200:
                data = await resp.json()
                raw_slots = data if isinstance(data, list) else data.get("data", [])

                slots = []
                for item in (raw_slots or []):
                    t = item.get("time") if isinstance(item, dict) else str(item)
                    if t and ":" in t:
                        parts = t.split(":")[:2]
                        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                            slots.append(f"{int(parts[0]):02d}:{int(parts[1]):02d}")

                now = now_msk()
                target_date = date.fromisoformat(date_str)
                if target_date < now.date():
                    return []
                if target_date == now.date():
                    min_dt = now + timedelta(minutes=30)
                    filtered = []
                    for s in slots:
                        h, m = map(int, s.split(":"))
                        if datetime(now.year, now.month, now.day, h, m) >= min_dt:
                            filtered.append(s)
                    return filtered

                return slots
            logging.warning(f"YCLIENTS слоты ({resp.status}): {await resp.text()}")
    except Exception as e:
        logging.error(f"Сетевая ошибка при запросе слотов YCLIENTS: {e}")

    return None


async def fetch_available_times(branch_id: str, master_id: str, service_ids: str | list[str], date_str: str) -> list[str]:
    res = await _fetch_available_times_raw(branch_id, master_id, service_ids, date_str)
    return res if res is not None else []


async def check_slot_available(
    branch_id: str,
    master_id: str,
    service_ids: str | list[str],
    date_str: str,
    visit_time: str
) -> tuple[bool, list[str], str | None]:
    """
    Проверяет доступность слота.
    При временном сетевом сбое НЕ сбрасывает клиента, а доверяет финальному запросу book_record.
    """
    if not USE_YCLIENTS_API:
        return True, [], None

    srv_list = _normalize_service_ids(service_ids)
    fresh_times = await _fetch_available_times_raw(branch_id, master_id, srv_list, date_str)

    # Если сервер YCLIENTS ответил успешно и слота больше нет в списке
    if fresh_times is not None and visit_time not in fresh_times:
        return False, fresh_times, f"Время {visit_time} уже заняли!"

    company_id = YCLIENTS_COMPANIES.get(branch_id, 0)
    if not company_id:
        return True, fresh_times or [], None

    url = f"{BASE_URL}/book_check/{company_id}"
    staff_id = 0 if master_id == "any" else int(master_id)
    payload = {
        "appointments": [
            {
                "id": 1,
                "services": [int(sid) for sid in srv_list],
                "staff_id": staff_id,
                "datetime": f"{date_str}T{visit_time}:00"
            }
        ]
    }

    try:
        session = await get_http_session()
        async with session.post(url, json=payload, headers=_get_headers()) as resp:
            if resp.status in (200, 201):
                return True, fresh_times or [], None
            if resp.status in (400, 422):
                resp_json = await resp.json()
                meta = resp_json.get("meta", {})
                err_msg = (meta.get("message") if isinstance(meta, dict) else None) or f"Время {visit_time} больше недоступно."
                updated_times = [t for t in (fresh_times or []) if t != visit_time]
                return False, updated_times, err_msg
    except Exception as e:
        logging.error(f"Сетевая ошибка при проверке book_check (пропускаем к финальной записи): {e}")

    return True, fresh_times or [], None


async def is_yclients_record_active(branch_id: str, yclients_record_id: str | None) -> bool:
    if not USE_YCLIENTS_API or not yclients_record_id or yclients_record_id == "mock_record_id":
        return True

    company_id = YCLIENTS_COMPANIES.get(branch_id, 0)
    if not company_id:
        return True

    url = f"{BASE_URL}/record/{company_id}/{yclients_record_id}"
    try:
        session = await get_http_session()
        async with session.get(url, headers=_get_headers()) as resp:
            if resp.status == 404:
                return False
            if resp.status == 200:
                resp_json = await resp.json()
                data = (resp_json.get("data") or {}) if isinstance(resp_json, dict) else {}
                if data.get("deleted") is True:
                    return False
                if data.get("attendance") == -1 or data.get("visit_attendance") == -1:
                    return False
                return True
    except Exception as e:
        logging.error(f"Ошибка проверки статуса записи #{yclients_record_id} в YCLIENTS: {e}")

    return True


async def fetch_record_assigned_staff_id(branch_id: str, yclients_record_id: str) -> str | None:
    """Узнает ID мастера, которого автоматически назначил YCLIENTS при выборе 'Любой свободный специалист'."""
    if not USE_YCLIENTS_API or not yclients_record_id or yclients_record_id == "mock_record_id":
        return None

    company_id = YCLIENTS_COMPANIES.get(branch_id, 0)
    if not company_id:
        return None

    url = f"{BASE_URL}/record/{company_id}/{yclients_record_id}"
    try:
        session = await get_http_session()
        async with session.get(url, headers=_get_headers()) as resp:
            if resp.status == 200:
                resp_json = await resp.json()
                data = (resp_json.get("data") or {}) if isinstance(resp_json, dict) else {}
                staff_id = data.get("staff_id") or (data.get("staff", {}) or {}).get("id")
                if staff_id:
                    return str(staff_id)
    except Exception as e:
        logging.error(f"Не удалось получить назначенного мастера для записи #{yclients_record_id}: {e}")
    return None


async def create_yclients_record(data: dict) -> tuple[str | None, str | None, str | None]:
    """
    Создает бронь в YCLIENTS.
    Возвращает кортеж: (record_id, error_message, assigned_staff_id).
    """
    if not USE_YCLIENTS_API:
        return "mock_record_id", None, None

    company_id = YCLIENTS_COMPANIES.get(data["branch_id"], 0)
    if not company_id or not YCLIENTS_PARTNER_TOKEN:
        return None, "Не настроен ID филиала или токен YCLIENTS", None

    url = f"{BASE_URL}/book_record/{company_id}"
    datetime_iso = f"{data['visit_date']}T{data['visit_time']}:00"

    staff_id = 0 if data["master_id"] == "any" else int(data["master_id"])
    srv_list = _normalize_service_ids(data.get("service_ids") or data.get("service_id", ""))

    payload = {
        "phone": data["phone"],
        "fullname": data["client_name"],
        "email": "",
        "comment": "Онлайн-запись через Telegram-бота Sahar&Vosk",
        "appointments": [
            {
                "id": 1,
                "services": [int(sid) for sid in srv_list],
                "staff_id": staff_id,
                "datetime": datetime_iso
            }
        ]
    }

    try:
        session = await get_http_session()
        async with session.post(url, json=payload, headers=_get_headers()) as resp:
            resp_json = await resp.json()
            if resp.status in (200, 201) and resp_json.get("success"):
                records = resp_json.get("data", [])
                if records:
                    rec_obj = records[0] if isinstance(records, list) else records
                    raw_rec_id = rec_obj.get("record_id") or rec_obj.get("id") if isinstance(rec_obj, dict) else None
                    if not raw_rec_id:
                        return None, "Сервер расписания не вернул номер записи.", None
                    rec_id = str(raw_rec_id)
                    assigned_staff = str(rec_obj.get("staff_id")) if rec_obj.get("staff_id") else None
                    if not assigned_staff and data["master_id"] == "any":
                        assigned_staff = await fetch_record_assigned_staff_id(data["branch_id"], rec_id)
                    logging.info(f"Запись #{rec_id} успешно создана в YCLIENTS ({data['branch_id']})")
                    return rec_id, None, assigned_staff

            meta = resp_json.get("meta", {})
            err_msg = meta.get("message") if isinstance(meta, dict) else "Выбранное время уже занято или недоступно."
            logging.error(f"Отказ создания записи в YCLIENTS: {resp_json}")
            return None, err_msg, None
    except Exception as e:
        logging.error(f"Сетевой сбой при создании бронирования в YCLIENTS: {e}")
        return None, "Временная ошибка связи с сервером расписания.", None


async def delete_yclients_record(branch_id: str, yclients_record_id: str | None) -> bool:
    if not USE_YCLIENTS_API or not yclients_record_id or yclients_record_id == "mock_record_id":
        return True

    company_id = YCLIENTS_COMPANIES.get(branch_id, 0)
    if not company_id:
        return False

    url = f"{BASE_URL}/record/{company_id}/{yclients_record_id}"
    try:
        session = await get_http_session()
        async with session.delete(url, headers=_get_headers()) as resp:
            if resp.status in (200, 204):
                logging.info(f"Запись #{yclients_record_id} удалена из YCLIENTS.")
                return True
            logging.error(f"Ошибка отмены в YCLIENTS ({resp.status}): {await resp.text()}")
    except Exception as e:
        logging.error(f"Ошибка при отмене записи в YCLIENTS: {e}")

    return False