import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Токен бота не найден! Укажите BOT_TOKEN в файле .env")

raw_admins = os.getenv("ADMIN_IDS", "")
ADMIN_IDS: list[int] = [
    int(x.strip()) for x in raw_admins.split(",") if x.strip().isdigit()
]

# Настройки YCLIENTS API
USE_YCLIENTS_API = os.getenv("USE_YCLIENTS_API", "false").lower() == "true"
YCLIENTS_PARTNER_TOKEN = os.getenv("YCLIENTS_PARTNER_TOKEN", "")
YCLIENTS_USER_TOKEN = os.getenv("YCLIENTS_USER_TOKEN", "")
YCLIENTS_LOGIN = os.getenv("YCLIENTS_LOGIN", "")
YCLIENTS_PASSWORD = os.getenv("YCLIENTS_PASSWORD", "")

YCLIENTS_COMPANIES = {
    "prazhskaya": int(os.getenv("YCLIENTS_COMPANY_PRAZHSKAYA", "0") or 0),
    "leningradskaya": int(os.getenv("YCLIENTS_COMPANY_LENINGRADSKAYA", "0") or 0),
}

# ID закрытых групп для архива переписок (можно привязать командой /bind_... прямо в группе)
ARCHIVE_GROUPS_ENV = {
    "prazhskaya": int(os.getenv("ARCHIVE_GROUP_PRAZHSKAYA", "0") or 0),
    "leningradskaya": int(os.getenv("ARCHIVE_GROUP_LENINGRADSKAYA", "0") or 0),
}