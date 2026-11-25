#!/usr/bin/env bash
# ==============================================================================
# Скрипт развертывания и обновления Sahar&Vosk Telegram Bot (Ubuntu 22.04 / 24.04)
# ==============================================================================
set -euo pipefail

APP_DIR="/opt/saharvosk_bot_for_kudrovo"
SERVICE_NAME="saharvosk"
BACKUP_DIR="${APP_DIR}/backups"
BACKUP_SCRIPT="/usr/local/bin/saharvosk_backup.sh"
CRON_FILE="/etc/cron.d/saharvosk_backup"

echo "=========================================================="
echo "🚀 Начало установки / обновления Sahar&Vosk Bot"
echo "=========================================================="

# 1. Проверка прав root
if [ "$(id -u)" -ne 0 ]; then
    echo "❌ Ошибка: скрипт должен быть запущен с правами root (sudo ./deploy.sh)" >&2
    exit 1
fi

# 2. Настройка системного часового пояса Europe/Moscow (UTC+3)
echo "🕒 Настройка часового пояса Europe/Moscow..."
timedatectl set-timezone Europe/Moscow
echo "✓ Часовой пояс: $(timedatectl | grep 'Time zone' || date)"

# 3. Установка системных зависимостей
echo "📦 Обновление пакетов и установка системных утилит..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip sqlite3 git curl rsync

# 4. Проверка доступности внешних шлюзов Telegram API и YCLIENTS API
echo "🌐 Проверка сетевого доступа к шлюзам Telegram и YCLIENTS..."

# 4.1 Telegram API
TG_STATUS=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 8 https://api.telegram.org || echo "000")
if [ "$TG_STATUS" != "000" ]; then
    echo "✓ Telegram API доступен напрямую (HTTP статус: ${TG_STATUS})"
else
    echo "⚠️ ПРЕДУПРЕЖДЕНИЕ: Не удалось подключиться к api.telegram.org (код ${TG_STATUS}). Проверьте исходящий трафик сервера."
fi

# 4.2 YCLIENTS API v1
YC_STATUS=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 8 https://api.yclients.ru/api/v1 || echo "000")
if [ "$YC_STATUS" == "200" ] || [ "$YC_STATUS" == "401" ] || [ "$YC_STATUS" == "403" ]; then
    echo "✓ YCLIENTS API шлюз доступен (HTTP статус: ${YC_STATUS})"
    if [ "$YC_STATUS" == "403" ]; then
        echo "ℹ️ Примечание: статус 403 от Cloudflare/YCLIENTS может означать необходимость белых заголовков или партнерского токена."
    fi
elif [ "$YC_STATUS" == "000" ]; then
    echo "❌ ОШИБКА: Сервер не может достучаться до api.yclients.ru. Возможна блокировка на уровне дата-центра."
else
    echo "✓ YCLIENTS API вернул HTTP статус: ${YC_STATUS}"
fi

# 5. Подготовка рабочей директории
# Если скрипт запущен прямо из склонированного репозитория вне /opt, синхронизируем в /opt/saharvosk_bot_for_kudrovo
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ "$SCRIPT_DIR" != "$APP_DIR" ]; then
    echo "📂 Синхронизация файлов проекта в ${APP_DIR}..."
    mkdir -p "$APP_DIR"
    # Исключаем локальные секреты, БД, сессию MAX, виртуальное окружение и бэкапы при синхронизации
    if command -v rsync >/dev/null 2>&1; then
        rsync -av \
            --exclude='.git' \
            --exclude='.venv' \
            --exclude='venv' \
            --exclude='env' \
            --exclude='__pycache__' \
            --exclude='backups' \
            --exclude='.env' \
            --exclude='.env.local' \
            --exclude='.env.*.local' \
            --exclude='*.db' \
            --exclude='*.db-wal' \
            --exclude='*.db-shm' \
            --exclude='*.db-journal' \
            --exclude='*.sqlite3' \
            --exclude='max_session*' \
            --exclude='.idea' \
            --exclude='.vscode' \
            --exclude='*.log' \
            "${SCRIPT_DIR}/" "${APP_DIR}/"
    else
        tar -C "$SCRIPT_DIR" \
            --exclude='./.git' \
            --exclude='./.venv' \
            --exclude='./venv' \
            --exclude='./env' \
            --exclude='__pycache__' \
            --exclude='./backups' \
            --exclude='./.env' \
            --exclude='./.env.local' \
            --exclude='./.env.*.local' \
            --exclude='*.db' \
            --exclude='*.db-wal' \
            --exclude='*.db-shm' \
            --exclude='*.db-journal' \
            --exclude='*.sqlite3' \
            --exclude='max_session*' \
            --exclude='./.idea' \
            --exclude='./.vscode' \
            --exclude='*.log' \
            -cf - . | tar -C "$APP_DIR" -xf -
    fi
fi

cd "$APP_DIR"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

# 6. Виртуальное окружение и зависимости Python
echo "🐍 Настройка виртуального окружения Python..."
if [ ! -d "${APP_DIR}/.venv" ]; then
    python3 -m venv "${APP_DIR}/.venv"
    echo "✓ Виртуальное окружение создано"
fi

echo "📥 Установка / обновление библиотек из requirements.txt..."
"${APP_DIR}/.venv/bin/pip" install --upgrade pip
"${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

# 7. Проверка конфигурации .env и прав доступа к чувствительным файлам
ENV_READY=true
if [ ! -f "${APP_DIR}/.env" ]; then
    if [ -f "${APP_DIR}/.env.example" ]; then
        cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
        echo "⚠️ Файл .env отсутствовал. Скопирован шаблон .env.example -> .env."
    else
        touch "${APP_DIR}/.env"
        echo "⚠️ Создан пустой .env"
    fi
    ENV_READY=false
else
    # Проверяем, заменён ли дефолтный BOT_TOKEN
    if grep -q "your_telegram_bot_token_here" "${APP_DIR}/.env" || [ ! -s "${APP_DIR}/.env" ]; then
        ENV_READY=false
    fi
fi
chmod 600 "${APP_DIR}/.env"

# Принудительно закрываем права на БД и сессии MAX (идемпотентно при каждом запуске)
for db_artifact in "${APP_DIR}"/saharvosk.db "${APP_DIR}"/saharvosk.db-wal "${APP_DIR}"/saharvosk.db-shm; do
    if [ -f "$db_artifact" ]; then
        chmod 600 "$db_artifact"
    fi
done
find "$APP_DIR" -maxdepth 1 -name "max_session*" -exec chmod u=rwX,go= {} + 2>/dev/null || true

# 8. Настройка ночного горячего бэкапа SQLite (через cron)
echo "💾 Настройка ежедневного ночного бэкапа базы данных..."
cat > "$BACKUP_SCRIPT" << 'EOF'
#!/usr/bin/env bash
set -euo pipefail
umask 077
APP_DIR="/opt/saharvosk_bot_for_kudrovo"
BACKUP_DIR="${APP_DIR}/backups"
DB_FILE="${APP_DIR}/saharvosk.db"
DATE_STR=$(date +%Y-%m-%d_%H-%M-%S)

if [ -f "$DB_FILE" ]; then
    mkdir -p "$BACKUP_DIR"
    chmod 700 "$BACKUP_DIR"
    # Горячий консистентный бэкап SQLite (безопасен при открытом WAL-соединении, ждёт до 25 сек при записи)
    sqlite3 -cmd ".timeout 25000" "$DB_FILE" ".backup '${BACKUP_DIR}/saharvosk_${DATE_STR}.db'"
    chmod 600 "${BACKUP_DIR}/saharvosk_${DATE_STR}.db"
    # Ротация: удаляем копии старше 14 дней
    find "$BACKUP_DIR" -type f -name "saharvosk_*.db" -mtime +14 -delete
fi
EOF
chmod 700 "$BACKUP_SCRIPT"

# Запуск каждую ночь в 03:30 по Москве
cat > "$CRON_FILE" << EOF
# Ежедневный бэкап базы данных Sahar&Vosk в 03:30 MSK
30 3 * * * root ${BACKUP_SCRIPT} >/dev/null 2>&1
EOF
chmod 644 "$CRON_FILE"
echo "✓ Горячий бэкап настроен (ежедневно в 03:30, хранение 14 дней в ${BACKUP_DIR})"

# 9. Установка и запуск systemd-сервиса
echo "⚙️ Настройка systemd-сервиса ${SERVICE_NAME}..."
if [ -f "${APP_DIR}/saharvosk.service" ]; then
    cp "${APP_DIR}/saharvosk.service" "/etc/systemd/system/${SERVICE_NAME}.service"
    chmod 644 "/etc/systemd/system/${SERVICE_NAME}.service"
    systemctl daemon-reload
    systemctl enable "${SERVICE_NAME}"

    if [ "$ENV_READY" = true ]; then
        if [ -f "${APP_DIR}/saharvosk.db" ]; then
            echo "💾 Выполняю горячий бэкап БД перед перезапуском сервиса..."
            "$BACKUP_SCRIPT" || echo "⚠️ Предупреждение: не удалось сделать pre-deploy бэкап"
        fi
        echo "🔄 Перезапуск службы ${SERVICE_NAME}..."
        systemctl restart "${SERVICE_NAME}"
        sleep 2
        if systemctl is-active --quiet "${SERVICE_NAME}"; then
            echo "✅ Служба ${SERVICE_NAME} успешно запущена и работает!"
        else
            echo "⚠️ Служба не смогла запуститься. Проверьте логи: journalctl -u ${SERVICE_NAME} -n 30 --no-pager"
        fi
    else
        echo "=========================================================="
        echo "⚠️ ВНИМАНИЕ: Файл ${APP_DIR}/.env не заполнен актуальными ключами!"
        echo "1. Откройте файл: nano ${APP_DIR}/.env"
        echo "2. Укажите реальные BOT_TOKEN, ADMIN_IDS и токены YCLIENTS"
        echo "3. Запустите сервис командой: systemctl start ${SERVICE_NAME}"
        echo "=========================================================="
    fi
else
    echo "❌ Ошибка: файл saharvosk.service не найден в ${APP_DIR}!" >&2
    exit 1
fi

echo "=========================================================="
echo "🎉 Развертывание завершено!"
echo "Полезные команды:"
echo "  • Статус сервиса: systemctl status ${SERVICE_NAME}"
echo "  • Логи в реальном времени: journalctl -u ${SERVICE_NAME} -f"
echo "  • Ручной бэкап БД: ${BACKUP_SCRIPT}"
echo "=========================================================="
