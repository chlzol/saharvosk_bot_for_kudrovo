import os
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_ENV_PATH = os.path.join(BASE_DIR, ".env")
if os.path.exists(_ENV_PATH):
    load_dotenv(_ENV_PATH)
else:
    load_dotenv()


def _parse_bool(env_name: str, default: bool = False) -> bool:
    val = os.getenv(env_name)
    if val is None or not val.strip():
        return default
    return val.strip().lower() in ("1", "true", "yes", "on", "y")


def _parse_int(env_name: str, default: int = 0) -> int:
    val = os.getenv(env_name)
    if val is None or not val.strip():
        return default
    try:
        return int(val.strip())
    except ValueError:
        return default


def _parse_admin_ids(raw: str) -> list[int]:
    seen = set()
    result = []
    for item in raw.split(","):
        token = item.strip()
        if token.isdigit():
            uid = int(token)
            if uid > 0 and uid not in seen:
                seen.add(uid)
                result.append(uid)
    return result


BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
if not BOT_TOKEN:
    raise ValueError("Токен бота не найден! Укажите BOT_TOKEN в файле .env")

ADMIN_IDS: list[int] = _parse_admin_ids(os.getenv("ADMIN_IDS", ""))

# Настройки YCLIENTS API
USE_YCLIENTS_API = _parse_bool("USE_YCLIENTS_API", default=False)
YCLIENTS_PARTNER_TOKEN = os.getenv("YCLIENTS_PARTNER_TOKEN", "").strip()
YCLIENTS_USER_TOKEN = os.getenv("YCLIENTS_USER_TOKEN", "").strip()
# Опциональные legacy-поля (для совместимости; маскируются в health_monitor)
YCLIENTS_LOGIN = os.getenv("YCLIENTS_LOGIN", "").strip()
YCLIENTS_PASSWORD = os.getenv("YCLIENTS_PASSWORD", "")

YCLIENTS_COMPANIES = {
    "prazhskaya": _parse_int("YCLIENTS_COMPANY_PRAZHSKAYA", 0),
    "leningradskaya": _parse_int("YCLIENTS_COMPANY_LENINGRADSKAYA", 0),
}

# --- Настройки встроенного HTTP-сервера для приёма вебхуков YCLIENTS ---
YCLIENTS_WEBHOOK_ENABLED = _parse_bool("YCLIENTS_WEBHOOK_ENABLED", default=True)
YCLIENTS_WEBHOOK_HOST = (os.getenv("YCLIENTS_WEBHOOK_HOST") or "0.0.0.0").strip()
_raw_wh_port = _parse_int("YCLIENTS_WEBHOOK_PORT", 8080)
YCLIENTS_WEBHOOK_PORT = _raw_wh_port if 1 <= _raw_wh_port <= 65535 else 8080
YCLIENTS_WEBHOOK_SECRET = (os.getenv("YCLIENTS_WEBHOOK_SECRET") or "").strip()

# Обратный маппинг company_id → branch_id для вебхуков
YCLIENTS_COMPANY_TO_BRANCH: dict[int, str] = {
    v: k for k, v in YCLIENTS_COMPANIES.items() if v
}

# ID закрытых групп для архива переписок (можно привязать командой /bind_... прямо в группе)
ARCHIVE_GROUPS_ENV = {
    "prazhskaya": _parse_int("ARCHIVE_GROUP_PRAZHSKAYA", 0),
    "leningradskaya": _parse_int("ARCHIVE_GROUP_LENINGRADSKAYA", 0),
}

# --- Настройки прямого клиента мессенджера MAX (напоминания о визитах) ---
MAX_ENABLED = _parse_bool("MAX_ENABLED", default=False)
_raw_max_mode = (os.getenv("MAX_MODE") or "fallback").strip().lower()
MAX_MODE = _raw_max_mode if _raw_max_mode in ("fallback", "both") else "fallback"
MAX_SESSION_PATH = (os.getenv("MAX_SESSION_PATH") or "max_session").strip() or "max_session"
_raw_max_backend = (os.getenv("MAX_BACKEND") or "ws").strip().lower()
MAX_BACKEND = _raw_max_backend if _raw_max_backend in ("ws", "playwright") else "ws"