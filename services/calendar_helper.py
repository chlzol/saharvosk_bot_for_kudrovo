import urllib.parse
from datetime import datetime, timezone, timedelta


def generate_google_calendar_url(
    title: str,
    visit_date: str,
    visit_time: str,
    duration_min: int,
    location: str,
    description: str
) -> str:
    """Генерирует прямую ссылку для мгновенного добавления визита в Google Календарь."""
    start_dt = datetime.fromisoformat(f"{visit_date}T{visit_time}:00")
    end_dt = start_dt + timedelta(minutes=duration_min)

    dates_str = f"{start_dt.strftime('%Y%m%dT%H%M%S')}/{end_dt.strftime('%Y%m%dT%H%M%S')}"

    params = {
        "action": "TEMPLATE",
        "text": title,
        "dates": dates_str,
        "ctz": "Europe/Moscow",
        "details": description,
        "location": location
    }
    return "https://calendar.google.com/calendar/render?" + urllib.parse.urlencode(params)


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
    start_dt = datetime.fromisoformat(f"{visit_date}T{visit_time}:00")
    end_dt = start_dt + timedelta(minutes=duration_min)
    now_utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    dtstart_str = start_dt.strftime("%Y%m%dT%H%M%S")
    dtend_str = end_dt.strftime("%Y%m%dT%H%M%S")

    clean_desc = description.replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")
    clean_title = title.replace("\\", "\\\\").replace("\n", " ").replace(",", "\\,").replace(";", "\\;")
    clean_loc = location.replace("\\", "\\\\").replace("\n", " ").replace(",", "\\,").replace(";", "\\;")

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