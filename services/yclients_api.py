import logging
import asyncio
from datetime import datetime, date, timedelta, timezone
import aiohttp
from config import (
    USE_YCLIENTS_API,
    YCLIENTS_PARTNER_TOKEN,
    YCLIENTS_USER_TOKEN,
    YCLIENTS_COMPANIES
)

BASE_URL = "https://api.yclients.ru/api/v1"
MSK_TZ = timezone(timedelta(hours=3))

_http_session: aiohttp.ClientSession | None = None
_session_locks: dict[int, asyncio.Lock] = {}


def _get_session_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    loop_id = id(loop)
    lock = _session_locks.get(loop_id)
    if lock is None:
        _session_locks.clear()
        lock = asyncio.Lock()
        _session_locks[loop_id] = lock
    return lock


def now_msk() -> datetime:
    """Возвращает текущее время по Санкт-Петербургу / Москве (UTC+3) без таймзоны для корректного сравнения."""
    return datetime.now(MSK_TZ).replace(tzinfo=None)


async def get_http_session() -> aiohttp.ClientSession:
    global _http_session
    loop = asyncio.get_running_loop()
    lock = _get_session_lock()
    async with lock:
        if (
            _http_session is None
            or _http_session.closed
            or getattr(_http_session, "_loop", None) != loop
            or _http_session._loop.is_closed()
        ):
            old_sess = _http_session
            _http_session = None
            if old_sess and not old_sess.closed:
                try:
                    await asyncio.wait_for(old_sess.close(), timeout=2.0)
                except Exception:
                    pass
            timeout = aiohttp.ClientTimeout(total=12)
            connector = aiohttp.TCPConnector(limit=100, ttl_dns_cache=300)
            _http_session = aiohttp.ClientSession(timeout=timeout, connector=connector)
        return _http_session


async def close_http_session():
    global _http_session
    sess = _http_session
    _http_session = None
    if sess and not sess.closed:
        try:
            await asyncio.wait_for(sess.close(), timeout=3.0)
        except Exception:
            pass


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


def _to_int_staff_id(master_id: str | int | None) -> int:
    raw = str(master_id or "").strip()
    if raw in ("", "any", "0") or not raw.isdigit():
        return 0
    return int(raw)


def _to_int_service_ids(srv_list: list[str]) -> list[int]:
    return [int(sid) for sid in srv_list if str(sid).strip().isdigit()]


_TRANSIENT_HTTP_STATUSES = (429, 500, 502, 503, 504)


async def _parse_json_safe(resp) -> tuple[bool, dict | list | None]:
    try:
        data = await resp.json()
        return True, data
    except Exception:
        return False, None


async def _get_with_retry(
    url: str,
    *,
    params=None,
    ok_statuses: tuple[int, ...] = (200,),
    max_attempts: int = 2,
) -> tuple[int | None, bool, dict | list | None, str]:
    """
    Выполняет идемпотентный GET-запрос с 1 коротким повтором при 429/5xx/сетевых обрывах.
    Возвращает (status_code, is_valid_json, parsed_json, raw_text).
    """
    last_err: Exception | None = None
    for attempt in range(max_attempts):
        try:
            session = await get_http_session()
            req_ctx = session.get(url, headers=_get_headers(), params=params) if params is not None else session.get(url, headers=_get_headers())
            async with req_ctx as resp:
                status = resp.status
                if status in ok_statuses:
                    is_json, parsed = await _parse_json_safe(resp)
                    if not is_json:
                        try:
                            raw = (await resp.text())[:200]
                        except Exception:
                            raw = ""
                        return status, False, None, raw
                    return status, True, parsed, ""
                if status in _TRANSIENT_HTTP_STATUSES and attempt + 1 < max_attempts:
                    await asyncio.sleep(0.15)
                    continue
                if status in _TRANSIENT_HTTP_STATUSES:
                    return status, False, None, ""
                try:
                    raw = (await resp.text())[:300]
                except Exception:
                    raw = ""
                return status, False, None, raw
        except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError, OSError) as e:
            last_err = e
            if attempt + 1 < max_attempts:
                await asyncio.sleep(0.15)
                continue
        except Exception as e:
            last_err = e
            break
    if last_err is not None:
        raise last_err
    return None, False, None, ""


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
        status, is_json, resp_json, raw_text = await _get_with_retry(url, params=params)
        if status == 200:
            if not is_json or resp_json is None:
                logging.warning(f"YCLIENTS book_staff вернул некорректный не-JSON ответ: {raw_text}")
                return None
            if isinstance(resp_json, dict) and resp_json.get("success") is False:
                logging.warning(f"YCLIENTS book_staff вернул success=False: {resp_json}")
                return None
            staff_list = resp_json if isinstance(resp_json, list) else (resp_json.get("data", []) if isinstance(resp_json, dict) else [])
            result = []
            for st in (staff_list or []):
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
        elif status == 429:
            logging.warning(f"YCLIENTS book_staff: 429 Too Many Requests (company={company_id})")
        elif status in (500, 502, 503, 504):
            logging.warning(f"YCLIENTS book_staff: {status} Server Error (company={company_id})")
        else:
            logging.warning(f"YCLIENTS book_staff ({status}): {raw_text}")
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
    params = [("service_ids[]", sid) for sid in srv_list if str(sid).strip().isdigit()]
    staff_int = _to_int_staff_id(master_id)
    if staff_int > 0:
        params.append(("staff_id", str(staff_int)))

    try:
        status, is_json, resp_json, raw_text = await _get_with_retry(url, params=params)
        if status == 200:
            if not is_json or resp_json is None:
                logging.warning(f"YCLIENTS book_dates вернул некорректный не-JSON ответ: {raw_text}")
                return None
            if isinstance(resp_json, dict) and resp_json.get("success") is False:
                logging.warning(f"YCLIENTS book_dates вернул success=False: {resp_json}")
                return None
            data = resp_json.get("data", resp_json) if isinstance(resp_json, dict) else {}
            booking_dates = data.get("booking_dates", []) if isinstance(data, dict) else []
            return [str(d) for d in booking_dates]
        elif status == 429:
            logging.warning(f"YCLIENTS book_dates: 429 Too Many Requests (company={company_id})")
        elif status in (500, 502, 503, 504):
            logging.warning(f"YCLIENTS book_dates: {status} Server Error (company={company_id})")
        else:
            logging.warning(f"YCLIENTS book_dates ({status}): {raw_text}")
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

    staff_param = _to_int_staff_id(master_id)
    url = f"{BASE_URL}/book_times/{company_id}/{staff_param}/{date_str}"
    srv_list = _normalize_service_ids(service_ids)
    params = [("service_ids[]", sid) for sid in srv_list if str(sid).strip().isdigit()]

    try:
        status, is_json, data, raw_text = await _get_with_retry(url, params=params)
        if status == 200:
            if not is_json or data is None:
                logging.warning(f"YCLIENTS book_times вернул некорректный не-JSON ответ: {raw_text}")
                return None
            if isinstance(data, dict) and data.get("success") is False:
                logging.warning(f"YCLIENTS book_times вернул success=False: {data}")
                return None
            raw_slots = data if isinstance(data, list) else (data.get("data", []) if isinstance(data, dict) else [])

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
        elif status == 429:
            logging.warning(f"YCLIENTS book_times: 429 Too Many Requests (company={company_id})")
        elif status in (500, 502, 503, 504):
            logging.warning(f"YCLIENTS book_times: {status} Server Error (company={company_id})")
        else:
            logging.warning(f"YCLIENTS слоты ({status}): {raw_text}")
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
    staff_id = _to_int_staff_id(master_id)
    payload = {
        "appointments": [
            {
                "id": 1,
                "services": _to_int_service_ids(srv_list),
                "staff_id": staff_id,
                "datetime": f"{date_str}T{visit_time}:00"
            }
        ]
    }

    try:
        session = await get_http_session()
        async with session.post(url, json=payload, headers=_get_headers()) as resp:
            try:
                resp_json = await resp.json()
            except Exception:
                resp_json = {}

            if resp.status in (200, 201):
                if isinstance(resp_json, dict) and resp_json.get("success") is False:
                    meta = resp_json.get("meta", {})
                    err_msg = (meta.get("message") if isinstance(meta, dict) else None) or f"Время {visit_time} больше недоступно."
                    updated_times = [t for t in (fresh_times or []) if t != visit_time]
                    return False, updated_times, err_msg
                return True, fresh_times or [], None
            if resp.status in (400, 422):
                meta = resp_json.get("meta", {})
                err_msg = (meta.get("message") if isinstance(meta, dict) else None) or f"Время {visit_time} больше недоступно."
                updated_times = [t for t in (fresh_times or []) if t != visit_time]
                return False, updated_times, err_msg
            if resp.status == 429:
                logging.warning("YCLIENTS book_check: 429 Too Many Requests, доверяем финальной записи")
                return True, fresh_times or [], None
            if resp.status in (500, 502, 503, 504):
                logging.warning(f"YCLIENTS book_check: {resp.status} Server Error, доверяем финальной записи")
                return True, fresh_times or [], None
    except Exception as e:
        logging.error(f"Сетевая ошибка при проверке book_check (пропускаем к финальной записи): {e}")

    return True, fresh_times or [], None


async def is_yclients_record_active(branch_id: str, yclients_record_id: str | None) -> bool:
    if not USE_YCLIENTS_API or not yclients_record_id or str(yclients_record_id) == "mock_record_id":
        return True

    company_id = YCLIENTS_COMPANIES.get(branch_id, 0)
    if not company_id:
        return True

    url = f"{BASE_URL}/record/{company_id}/{yclients_record_id}"
    try:
        status, is_json, resp_json, raw_text = await _get_with_retry(url, ok_statuses=(200, 201))
        if status == 404:
            return False
        if status == 429:
            logging.warning(f"YCLIENTS 429 при проверке записи #{yclients_record_id}")
            return True
        if status in (500, 502, 503, 504):
            logging.warning(f"YCLIENTS {status} при проверке записи #{yclients_record_id}")
            return True
        if status in (200, 201):
            if not is_json or not isinstance(resp_json, dict):
                logging.warning(f"YCLIENTS record #{yclients_record_id}: некорректный не-JSON ответ: {raw_text}")
                return True
            data = resp_json.get("data") or {}
            if not isinstance(data, dict):
                return True
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
    if not USE_YCLIENTS_API or not yclients_record_id or str(yclients_record_id) == "mock_record_id":
        return None

    company_id = YCLIENTS_COMPANIES.get(branch_id, 0)
    if not company_id:
        return None

    url = f"{BASE_URL}/record/{company_id}/{yclients_record_id}"
    try:
        status, is_json, resp_json, _ = await _get_with_retry(url, ok_statuses=(200, 201))
        if status in (200, 201) and is_json and isinstance(resp_json, dict):
            data = resp_json.get("data") or {}
            if isinstance(data, dict):
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

    staff_id = _to_int_staff_id(data.get("master_id"))
    srv_list = _normalize_service_ids(data.get("service_ids") or data.get("service_id", ""))

    payload = {
        "phone": data["phone"],
        "fullname": data["client_name"],
        "email": "",
        "comment": "Онлайн-запись через Telegram-бота Sahar&Vosk",
        "appointments": [
            {
                "id": 1,
                "services": _to_int_service_ids(srv_list),
                "staff_id": staff_id,
                "datetime": datetime_iso
            }
        ]
    }

    try:
        session = await get_http_session()
        async with session.post(url, json=payload, headers=_get_headers()) as resp:
            is_json, parsed_json = await _parse_json_safe(resp)
            resp_json = parsed_json if isinstance(parsed_json, dict) else {}

            if resp.status in (200, 201):
                if not is_json:
                    logging.warning(f"YCLIENTS create_record вернул не-JSON ответ ({resp.status}) ({data['branch_id']})")
                    return None, "Некорректный ответ сервера расписания. Повторите попытку чуть позже.", None
                if resp_json.get("success"):
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

            if resp.status == 429:
                logging.warning(f"YCLIENTS create_record: 429 Too Many Requests ({data['branch_id']})")
                return None, "Сервер расписания временно перегружен (429). Пожалуйста, повторите попытку через минуту.", None

            if resp.status in (500, 502, 503, 504):
                logging.warning(f"YCLIENTS create_record: {resp.status} Server Error ({data['branch_id']})")
                return None, "Сервер расписания временно недоступен. Пожалуйста, повторите попытку через пару минут.", None

            meta = resp_json.get("meta", {}) if isinstance(resp_json, dict) else {}
            err_msg = (meta.get("message") if isinstance(meta, dict) else None) or "Выбранное время уже занято или недоступно."
            logging.error(f"Отказ создания записи в YCLIENTS ({resp.status}): {resp_json}")
            return None, err_msg, None
    except Exception as e:
        logging.error(f"Сетевой сбой при создании бронирования в YCLIENTS: {e}")
        return None, "Временная ошибка связи с сервером расписания.", None


async def delete_yclients_record(branch_id: str, yclients_record_id: str | None) -> bool:
    if not USE_YCLIENTS_API or not yclients_record_id or str(yclients_record_id) == "mock_record_id":
        return True

    company_id = YCLIENTS_COMPANIES.get(branch_id, 0)
    if not company_id:
        return False

    url = f"{BASE_URL}/record/{company_id}/{yclients_record_id}"
    try:
        session = await get_http_session()
        async with session.delete(url, headers=_get_headers()) as resp:
            if resp.status in (200, 204, 404):
                logging.info(f"Запись #{yclients_record_id} успешно отменена/удалена в YCLIENTS (status {resp.status}).")
                return True
            if resp.status == 429:
                logging.warning(f"YCLIENTS delete_record: 429 Too Many Requests #{yclients_record_id}")
            elif resp.status in (500, 502, 503, 504):
                logging.warning(f"YCLIENTS delete_record: {resp.status} Server Error #{yclients_record_id}")
            else:
                logging.error(f"Ошибка отмены в YCLIENTS ({resp.status}): {await resp.text()}")
    except Exception as e:
        logging.error(f"Ошибка при отмене записи в YCLIENTS: {e}")

    return False


async def confirm_yclients_record(
    branch_id: str,
    yclients_record_id: str | None,
    expected_date: str | None = None,
    expected_time: str | None = None
) -> tuple[str, str | None]:
    """
    Проверяет актуальность записи в CRM YCLIENTS и передает статус подтверждения (attendance: 2).

    Возвращает (status, error_message):
    - ("ok", None) — визит успешно подтверждён в журнале YCLIENTS
    - ("cancelled", message) — запись удалена или отменена в YCLIENTS
    - ("rescheduled", message) — дата или время записи изменены в YCLIENTS
    - ("api_error", message) — сервер YCLIENTS недоступен (таймаут, сетевой сбой, HTTP 5xx, 429)
    """
    if not USE_YCLIENTS_API or not yclients_record_id or str(yclients_record_id) == "mock_record_id":
        return "ok", None

    company_id = YCLIENTS_COMPANIES.get(branch_id, 0)
    if not company_id:
        return "ok", None

    url = f"{BASE_URL}/record/{company_id}/{yclients_record_id}"

    # 1. Проверяем актуальность и параметры времени записи в CRM-журнале
    try:
        status, is_json, resp_json, raw_text = await _get_with_retry(url, ok_statuses=(200, 201))
        if status == 404:
            return "cancelled", f"Запись #{yclients_record_id} не найдена в YCLIENTS (404)."
        if status == 429:
            logging.warning(f"YCLIENTS confirm: 429 Too Many Requests GET #{yclients_record_id}")
            return "api_error", "Сервер YCLIENTS временно перегружен (429 Too Many Requests)."
        if status in (500, 502, 503, 504):
            logging.warning(f"YCLIENTS GET record #{yclients_record_id} вернул серверную ошибку {status}")
            return "api_error", f"Серверная ошибка YCLIENTS ({status})"
        if status in (200, 201):
            if not is_json or not isinstance(resp_json, dict):
                logging.warning(f"YCLIENTS GET record #{yclients_record_id}: некорректный не-JSON ответ: {raw_text}")
                return "api_error", "Некорректный ответ сервера YCLIENTS (ожидался JSON)."
            data = resp_json.get("data") or {}
            if not isinstance(data, dict):
                data = {}

            # Проверка флага удаления и статуса отмены/неявки
            if data.get("deleted") is True or data.get("attendance") == -1 or data.get("visit_attendance") == -1:
                return "cancelled", f"Запись #{yclients_record_id} удалена или отменена в журнале YCLIENTS."

            # Проверка переноса даты или времени
            rec_dt = str(data.get("datetime") or "")
            if expected_date and expected_date not in rec_dt:
                return "rescheduled", f"Дата записи #{yclients_record_id} изменена в YCLIENTS ({rec_dt})."
            if expected_time:
                norm_time = expected_time.zfill(5)
                if norm_time not in rec_dt and expected_time not in rec_dt:
                    return "rescheduled", f"Время записи #{yclients_record_id} изменено в YCLIENTS ({rec_dt})."
        else:
            logging.warning(f"YCLIENTS GET record #{yclients_record_id} статус {status}")
            return "api_error", f"Ошибка YCLIENTS ({status})"
    except Exception as e:
        logging.error(f"Сетевой сбой при проверке записи #{yclients_record_id} в YCLIENTS: {e}")
        return "api_error", str(e)

    # 2. Отправляем статус подтверждения визита (attendance: 2)
    payload = {"attendance": 2}
    try:
        session = await get_http_session()
        async with session.put(url, json=payload, headers=_get_headers()) as resp:
            if resp.status in (200, 201):
                logging.info(f"Запись #{yclients_record_id} успешно подтверждена в YCLIENTS (attendance=2).")
                return "ok", None
            if resp.status == 404:
                return "cancelled", f"Запись #{yclients_record_id} не найдена при подтверждении (404)."
            if resp.status == 429:
                return "api_error", "Сервер YCLIENTS временно перегружен (429 Too Many Requests)."
            if resp.status in (500, 502, 503, 504):
                return "api_error", f"Серверная ошибка YCLIENTS ({resp.status})."
            logging.warning(f"Ошибка ответа YCLIENTS PUT record #{yclients_record_id} ({resp.status}): {await resp.text()}")
            return "api_error", f"Ошибка сохранения в YCLIENTS ({resp.status})"
    except Exception as e:
        logging.error(f"Сетевой сбой при подтверждении записи #{yclients_record_id} в YCLIENTS: {e}")
        return "api_error", str(e)
