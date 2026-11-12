import re
import json
import logging
import asyncio
import aiohttp
from config import (
    YCLIENTS_PARTNER_TOKEN,
    YCLIENTS_USER_TOKEN,
    YCLIENTS_COMPANIES
)
from services import booking_service as bs

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_URL = "https://api.yclients.com/api/v1"


def _get_headers() -> dict:
    return {
        "Accept": "application/vnd.yclients.v2+json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {YCLIENTS_PARTNER_TOKEN}, User {YCLIENTS_USER_TOKEN}"
    }


def parse_duration_from_title(title: str, default_min: int = 30) -> int:
    """Распознает длительность из названия услуги (например: '1 ч. 30 мин.', '50 мин.', '2-хчасовой')."""
    t = (title or "").lower()
    if "2-хчасовой" in t or "2 часа" in t:
        return 120

    hm_match = re.search(r"(\d+)\s*ч\.?\s*(\d+)\s*мин", t)
    if hm_match:
        return int(hm_match.group(1)) * 60 + int(hm_match.group(2))

    h_match = re.search(r"(\d+)\s*ч(?:\.|ас|\b)", t)
    if h_match:
        return int(h_match.group(1)) * 60

    m_match = re.search(r"(\d+)\s*мин", t)
    if m_match:
        val = int(m_match.group(1))
        if 10 <= val <= 300:
            return val

    return default_min


async def fetch_branch_info(session: aiohttp.ClientSession, company_id: int) -> dict:
    url = f"{BASE_URL}/company/{company_id}"
    try:
        async with session.get(url, headers=_get_headers()) as resp:
            if resp.status == 200:
                data = (await resp.json()).get("data", {})
                return {
                    "title": data.get("title", f"Филиал {company_id}"),
                    "address": data.get("address", "г. Кудрово"),
                    "phone": data.get("phone", "+7 (812) 988-98-53")
                }
    except Exception as e:
        logging.error(f"Ошибка загрузки инфо о филиале {company_id}: {e}")
    return {"title": f"Филиал {company_id}", "address": "г. Кудрово", "phone": "+7 (812) 988-98-53"}


async def fetch_staff(session: aiohttp.ClientSession, company_id: int) -> list[dict]:
    url = f"{BASE_URL}/book_staff/{company_id}"
    try:
        async with session.get(url, headers=_get_headers()) as resp:
            if resp.status == 200:
                return (await resp.json()).get("data", [])
            logging.error(f"Ошибка загрузки сотрудников компании {company_id}: {await resp.text()}")
    except Exception as e:
        logging.error(f"Сетевая ошибка при загрузке сотрудников {company_id}: {e}")
    return []


async def fetch_services(session: aiohttp.ClientSession, company_id: int, staff_id: str | None = None) -> dict | None:
    url = f"{BASE_URL}/book_services/{company_id}"
    params = {"staff_id": staff_id} if staff_id else {}
    try:
        async with session.get(url, headers=_get_headers(), params=params) as resp:
            if resp.status == 200:
                return (await resp.json()).get("data") or {}
            logging.error(f"Ошибка загрузки услуг компании {company_id}: {await resp.text()}")
    except Exception as e:
        logging.error(f"Сетевая ошибка при загрузке услуг {company_id}: {e}")
    return None


async def run_yclients_sync() -> dict:
    if not YCLIENTS_PARTNER_TOKEN or not YCLIENTS_USER_TOKEN:
        raise ValueError("Не заданы YCLIENTS_PARTNER_TOKEN или YCLIENTS_USER_TOKEN в .env")

    branches_result = {}
    categories_raw = {}
    services_result = {}
    masters_result = {}

    timeout = aiohttp.ClientTimeout(total=35)
    semaphore = asyncio.Semaphore(6)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for branch_key, comp_id in YCLIENTS_COMPANIES.items():
            if not comp_id:
                continue

            logging.info(f"Синхронизация точки Sahar&Vosk: {branch_key} (ID: {comp_id})...")
            info = await fetch_branch_info(session, comp_id)
            branches_result[branch_key] = {
                "id": branch_key,
                "company_id": comp_id,
                "title": info["title"],
                "address": info["address"],
                "phone": info["phone"]
            }

            staff_list = await fetch_staff(session, comp_id)
            branch_active_staff_ids = []

            for st in staff_list:
                is_bookable = st.get("bookable", True)
                is_fired = st.get("fired", False)
                is_deleted = st.get("is_deleted", False)

                if not is_bookable or is_fired or is_deleted:
                    continue

                s_id = str(st["id"])
                branch_active_staff_ids.append(s_id)

                if s_id not in masters_result:
                    masters_result[s_id] = {
                        "id": s_id,
                        "name": st.get("name", "Специалист"),
                        "specialization": st.get("specialization", ""),
                        "branches": [branch_key],
                        "cats": []
                    }
                else:
                    if branch_key not in masters_result[s_id]["branches"]:
                        masters_result[s_id]["branches"].append(branch_key)

            srv_data = await fetch_services(session, comp_id)
            if srv_data is None:
                raise RuntimeError(f"Не удалось загрузить услуги филиала {branch_key} (company_id={comp_id})")
            services_list = srv_data.get("services", []) if isinstance(srv_data, dict) else srv_data
            categories_list = srv_data.get("category", []) if isinstance(srv_data, dict) else []

            for cat in categories_list:
                c_id = str(cat["id"])
                categories_raw[c_id] = cat.get("title", f"Категория {c_id}")

            service_to_branch_staff: dict[str, list[str]] = {}
            service_durations_sec: dict[str, int] = {}

            async def _fetch_master_services(st_id: str):
                async with semaphore:
                    m_data = await fetch_services(session, comp_id, staff_id=st_id)
                    m_srvs = ((m_data.get("services", []) if isinstance(m_data, dict) else m_data) if m_data is not None else []) or []
                    res_items = []
                    for x in m_srvs:
                        if isinstance(x, dict) and "id" in x:
                            sec = int(x.get("seance_length") or x.get("duration") or 0)
                            res_items.append((str(x["id"]), sec))
                    return st_id, res_items

            tasks = [_fetch_master_services(st_id) for st_id in branch_active_staff_ids]
            master_mappings = await asyncio.gather(*tasks, return_exceptions=True)

            for item in master_mappings:
                if isinstance(item, Exception):
                    continue
                st_id, srv_tuples = item
                for s_id, sec in srv_tuples:
                    service_to_branch_staff.setdefault(s_id, [])
                    if st_id not in service_to_branch_staff[s_id]:
                        service_to_branch_staff[s_id].append(st_id)
                    if sec > 0:
                        service_durations_sec[s_id] = max(service_durations_sec.get(s_id, 0), sec)

            for s in services_list:
                s_id = str(s["id"])
                cat_id = str(s.get("category_id", "0"))
                srv_title = s.get("title", f"Услуга {s_id}")

                raw_staff = s.get("staff", [])
                srv_staff_ids = list(service_to_branch_staff.get(s_id, []))
                max_seance_sec = service_durations_sec.get(s_id, 0)

                for st_obj in raw_staff:
                    if isinstance(st_obj, dict) and "id" in st_obj:
                        sid_str = str(st_obj["id"])
                        if sid_str in branch_active_staff_ids and sid_str not in srv_staff_ids:
                            srv_staff_ids.append(sid_str)
                        if st_obj.get("seance_length"):
                            max_seance_sec = max(max_seance_sec, int(st_obj["seance_length"]))
                    elif isinstance(st_obj, (int, str)):
                        sid_str = str(st_obj)
                        if sid_str in branch_active_staff_ids and sid_str not in srv_staff_ids:
                            srv_staff_ids.append(sid_str)

                api_sec = int(s.get("duration") or s.get("seance_length") or max_seance_sec or 0)
                api_min = api_sec // 60 if api_sec > 0 else 30

                # Если API вернул дефолтные 30 мин, проверяем точное время в названии услуги
                duration_min = parse_duration_from_title(srv_title, default_min=api_min)

                price_min = s.get("price_min", 0) or 0
                price_max = s.get("price_max", 0) or 0
                price = int(price_min or price_max or 0)

                if s_id not in services_result:
                    services_result[s_id] = {
                        "id": s_id,
                        "cat_id": cat_id,
                        "title": srv_title,
                        "price": price,
                        "duration": duration_min,
                        "branch_id": branch_key,
                        "branches": [branch_key],
                        "staff_ids": list(srv_staff_ids),
                        "staff_by_branch": {branch_key: list(srv_staff_ids)}
                    }
                else:
                    if branch_key not in services_result[s_id]["branches"]:
                        services_result[s_id]["branches"].append(branch_key)
                    services_result[s_id].setdefault("staff_by_branch", {})[branch_key] = list(srv_staff_ids)
                    for st_id in srv_staff_ids:
                        if st_id not in services_result[s_id]["staff_ids"]:
                            services_result[s_id]["staff_ids"].append(st_id)

    used_category_ids = {s["cat_id"] for s in services_result.values()}
    categories_result = {
        c_id: title for c_id, title in categories_raw.items()
        if c_id in used_category_ids
    }

    for m in masters_result.values():
        m["cats"] = list(categories_result.keys())

    if not services_result or not masters_result:
        raise RuntimeError("Пустой результат синхронизации YCLIENTS — каталог booking_data.py не перезаписан.")

    output_path = "services/booking_data.py"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("# Файл сгенерирован автоматически из YCLIENTS API для Sahar&Vosk\n\n")
        f.write(f"BRANCHES = {json.dumps(branches_result, ensure_ascii=False, indent=4)}\n\n")
        f.write(f"CATEGORIES = {json.dumps(categories_result, ensure_ascii=False, indent=4)}\n\n")
        f.write(f"SERVICES = {json.dumps(services_result, ensure_ascii=False, indent=4)}\n\n")
        f.write(f"MASTERS = {json.dumps(masters_result, ensure_ascii=False, indent=4)}\n")

    bs.reload_booking_data()

    prazh_total = sum(1 for s in services_result.values() if "prazhskaya" in s["branches"])
    len_total = sum(1 for s in services_result.values() if "leningradskaya" in s["branches"])
    prazh_masters = sum(1 for m in masters_result.values() if "prazhskaya" in m["branches"])
    len_masters = sum(1 for m in masters_result.values() if "leningradskaya" in m["branches"])

    return {
        "total_services": len(services_result),
        "prazh_services": prazh_total,
        "len_services": len_total,
        "total_masters": len(masters_result),
        "prazh_masters": prazh_masters,
        "len_masters": len_masters,
        "categories": len(categories_result)
    }


async def main():
    print("🚀 Подключение к YCLIENTS API и синхронизация каталога Sahar&Vosk...")
    stats = await run_yclients_sync()
    print("\n✅ Синхронизация завершена! Файл 'services/booking_data.py' обновлен.")
    print(
        f"📊 Пражская: {stats['prazh_services']} услуг, {stats['prazh_masters']} специалистов | "
        f"Ленинградская: {stats['len_services']} услуг, {stats['len_masters']} специалистов."
    )


if __name__ == "__main__":
    asyncio.run(main())