"""
Одноразовый интерактивный скрипт авторизации аккаунта мессенджера MAX для бота Sahar&Vosk.

Запуск на сервере (один раз перед включением MAX_ENABLED=true):
    python max_auth.py

Сохраняет сессию в путь, указанный в MAX_SESSION_PATH (относительно корня проекта).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import config
from services.max_sender import (
    normalize_phone_e164,
    resolve_playwright_profile_dir,
    resolve_session_location,
    save_auth_meta,
)


def _clear_existing_ws_session(work_dir: str, session_name: str) -> None:
    session_file = Path(work_dir) / session_name
    for suffix in ("", "-wal", "-shm", "-journal", ".meta.json"):
        target = Path(f"{session_file}{suffix}")
        if target.exists():
            target.unlink()
            print(f"Удалён старый файл сессии: {target}")


async def _auth_ws_qr(work_dir: str, session_name: str) -> None:
    from pymax import ConsoleQrHandler, ExtraConfig, WebClient
    from pymax.auth import QrAuthFlow

    Path(work_dir).mkdir(parents=True, exist_ok=True)
    print("\n[MAX WebSocket] Авторизация по QR-коду...")
    print("Откройте приложение MAX на телефоне -> Профиль / Устройства -> Привязать устройство и отсканируйте QR-код ниже.\n")

    client = WebClient(
        session_name=session_name,
        work_dir=work_dir,
        extra_config=ExtraConfig(
            reconnect=False,
            relogin=True,
            telemetry=False,
            log_level="WARNING",
        ),
        auth_flow=QrAuthFlow(ConsoleQrHandler()),
    )
    try:
        await client.connect()
        save_auth_meta(work_dir, session_name, auth_method="qr", client_type="web_client")
        me = getattr(client, "me", None)
        contact = getattr(me, "contact", None)
        print(
            f"\n✅ Сессия MAX (WebClient) успешно сохранена в {Path(work_dir) / session_name}!"
            f"\n   Аккаунт: id={getattr(contact, 'id', '?')}, имя={getattr(contact, 'names', '')}"
        )
    finally:
        await client.close()


async def _auth_ws_sms(phone: str, work_dir: str, session_name: str) -> None:
    from pymax import Client, ConsoleSmsCodeProvider, ExtraConfig
    from pymax.auth import ConsolePasswordProvider, SmsAuthFlow

    norm_phone = normalize_phone_e164(phone)
    if not norm_phone:
        raise ValueError(f"Некорректный номер телефона: {phone!r}")

    Path(work_dir).mkdir(parents=True, exist_ok=True)
    print(f"\n[MAX TCP/SMS] Отправка кода подтверждения на {norm_phone}...")
    client = Client(
        phone=norm_phone,
        session_name=session_name,
        work_dir=work_dir,
        extra_config=ExtraConfig(
            reconnect=False,
            relogin=True,
            telemetry=False,
            log_level="WARNING",
        ),
        auth_flow=SmsAuthFlow(ConsoleSmsCodeProvider(), ConsolePasswordProvider()),
    )
    try:
        await client.connect()
        save_auth_meta(work_dir, session_name, auth_method="sms", client_type="client")
        me = getattr(client, "me", None)
        contact = getattr(me, "contact", None)
        print(
            f"\n✅ Сессия MAX (Client/SMS) успешно сохранена в {Path(work_dir) / session_name}!"
            f"\n   Аккаунт: {norm_phone} (id={getattr(contact, 'id', '?')})"
        )
    finally:
        await client.close()


async def _auth_playwright() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("❌ Пакет playwright не установлен. Установите его или используйте MAX_BACKEND=ws.")
        sys.exit(1)

    profile_dir = resolve_playwright_profile_dir()
    profile_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n[MAX Playwright] Открытие https://web.max.ru (профиль: {profile_dir})...")
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://web.max.ru", wait_until="domcontentloaded")
            await asyncio.to_thread(
                input,
                "\nАвторизуйтесь в открывшемся окне web.max.ru (по QR-коду или телефону), затем нажмите Enter здесь: ",
            )
            print(f"✅ Профиль браузера MAX сохранён в {profile_dir}")
        finally:
            await context.close()


async def run_auth(args: argparse.Namespace) -> None:
    backend = (args.backend or config.MAX_BACKEND or "ws").strip().lower()
    if backend == "playwright":
        await _auth_playwright()
        return

    work_dir, session_name = resolve_session_location()
    # При повторном запуске max_auth.py по умолчанию очищаем старый файл сессии,
    # чтобы connect() не пытался логиниться по протухшему токену.
    if not args.keep_existing:
        _clear_existing_ws_session(work_dir, session_name)

    method = args.method
    if not method:
        print("Выберите способ авторизации MAX:")
        print("  1 — QR-код (WebSocket, рекомендуется — как на web.max.ru)")
        print("  2 — Номер телефона + код из SMS/MAX")
        choice = input("Ваш выбор [1]: ").strip() or "1"
        method = "sms" if choice == "2" else "qr"

    if method == "sms":
        phone = args.phone or input("Введите номер телефона аккаунта MAX (например, +79991234567): ").strip()
        await _auth_ws_sms(phone, work_dir, session_name)
    else:
        await _auth_ws_qr(work_dir, session_name)

    print(
        "\nТеперь можете включить напоминания в .env:\n"
        f"  MAX_ENABLED=true\n"
        f"  MAX_MODE=fallback  # или both\n"
        f"  MAX_SESSION_PATH={config.MAX_SESSION_PATH}\n"
        f"  MAX_BACKEND={backend}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Интерактивная авторизация аккаунта MAX для Sahar&Vosk Bot")
    parser.add_argument("--backend", choices=["ws", "playwright"], default=None, help="Транспорт MAX (по умолчанию из MAX_BACKEND)")
    parser.add_argument("--method", choices=["qr", "sms"], default=None, help="Способ входа для ws-клиента (qr или sms)")
    parser.add_argument("--phone", default=None, help="Номер телефона (+79...) для входа по SMS")
    parser.add_argument("--relogin", action="store_true", help="Удалить старую сессию перед авторизацией (включено по умолчанию)")
    parser.add_argument("--keep-existing", action="store_true", help="Проверить/использовать существующую сессию без сброса")
    args = parser.parse_args()
    asyncio.run(run_auth(args))


if __name__ == "__main__":
    main()
