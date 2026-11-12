import urllib.parse
from datetime import datetime, timezone, timedelta


def _parse_visit_dt(visit_date: str, visit_time: str) -> datetime:
    parts = str(visit_time).split(":")
    norm_time = f"{int(parts[0]):02d}:{int(parts[1]):02d}"
    return datetime.fromisoformat(f"{visit_date}T{norm_time}:00")


def generate_google_calendar_url(
    title: str,
    visit_date: str,
    visit_time: str,
    duration_min: int,
    location: str,
    description: str
) -> str:
    """Генерирует прямую ссылку для мгновенного добавления визита в Google Календарь."""
    start_dt = _parse_visit_dt(visit_date, visit_time)
    end_dt = start_dt + timedelta(minutes=duration_min)

    dates_str = f"{start_dt.strftime('%Y%m%dT%H%M%S')}/{end_dt.strftime('%Y%m%dT%H%M%S')}"

    base_url = "https://calendar.google.com/calendar/render?"
    desc_text = description or ""
    title_text = title or ""

    def _build_url(t: str, d: str) -> str:
        params = {
            "action": "TEMPLATE",
            "text": t,
            "dates": dates_str,
            "ctz": "Europe/Moscow",
            "details": d,
            "location": location
        }
        return base_url + urllib.parse.urlencode(params)

    url = _build_url(title_text, desc_text)
    if len(url) > 1900:
        while desc_text and len(url) > 1900:
            cut = max(0, len(desc_text) - max(10, (len(url) - 1900) // 4))
            desc_text = (desc_text[:cut].rstrip() + "...") if cut > 0 else ""
            url = _build_url(title_text, desc_text)
        while title_text and len(url) > 1900:
            title_text = title_text[:-10].rstrip() + "..." if len(title_text) > 15 else title_text[:10]
            url = _build_url(title_text, desc_text)
            if len(title_text) <= 10:
                break
    return url


def generate_ics_content(
    app_id: int,
    title: str,
    visit_date: str,
    visit_time: str,
    duration_min: int,
    location: str,
    description: str
) -> bytes:
    """Генерирует файл формата .ics со встроенным напоминанием за 2 часа для Apple Calendar и смартфонов."""
    start_dt = _parse_visit_dt(visit_date, visit_time)
    end_dt = start_dt + timedelta(minutes=duration_min)
    now_utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    dtstart_str = start_dt.strftime("%Y%m%dT%H%M%S")
    dtend_str = end_dt.strftime("%Y%m%dT%H%M%S")

    clean_desc = (description or "").replace("\r", "").replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")
    clean_title = (title or "").replace("\r", "").replace("\\", "\\\\").replace("\n", " ").replace(",", "\\,").replace(";", "\\;")
    clean_loc = (location or "").replace("\r", "").replace("\\", "\\\\").replace("\n", " ").replace(",", "\\,").replace(";", "\\;")

    ics_text = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//Sahar&Vosk Bot//RU\r\n"
        "CALSCALE:GREGORIAN\r\n"
        "METHOD:PUBLISH\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:saharvosk-{app_id}-{start_dt.strftime('%Y%m%d%H%M')}@saharvosk.ru\r\n"
        "DTSTAMP:" + now_utc + "\r\n"
        f"DTSTART;TZID=Europe/Moscow:{dtstart_str}\r\n"
        f"DTEND;TZID=Europe/Moscow:{dtend_str}\r\n"
        f"SUMMARY:{clean_title}\r\n"
        f"DESCRIPTION:{clean_desc}\r\n"
        f"LOCATION:{clean_loc}\r\n"
        "STATUS:CONFIRMED\r\n"
        "BEGIN:VALARM\r\n"
        "TRIGGER:-PT2H\r\n"
        "ACTION:DISPLAY\r\n"
        "DESCRIPTION:Напоминание о визите в студию Sahar&Vosk\r\n"
        "END:VALARM\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    return ics_text.encode("utf-8")