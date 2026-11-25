"""
Прямой Python-клиент для отправки одиночных напоминаний о визитах через мессенджер MAX.

Использует WebSocket/TCP-транспорт библиотеки maxapi-python (pymax) с сохранённой
пользовательской сессией (через max_auth.py) либо опциональный fallback на Playwright
для web.max.ru. Содержит встроенную защиту от антиспама (очередь + умная пауза 15–35 сек),
таймауты на все сетевые вызовы и rate-limit уведомлений администраторам при истечении сессии.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import random
import threading
import time
from pathlib import Path
from typing import Any

import config
from database import db

try:
    from websockets.exceptions import WebSocketException as _WebSocketException
    _WS_EXTRA_EXCEPTIONS: tuple[type[BaseException], ...] = (_WebSocketException,)
except Exception:
    _WS_EXTRA_EXCEPTIONS = ()

logger = logging.getLogger(__name__)

MIN_SEND_DELAY_SEC = 15.0
MAX_SEND_DELAY_SEC = 35.0
EXPIRED_ALERT_COOLDOWN_SEC = 6 * 3600  # Не чаще 1 раза в 6 часов
CONNECT_TIMEOUT_SEC = 20.0
REQUEST_TIMEOUT_SEC = 15.0
CLOSE_TIMEOUT_SEC = 5.0
IDLE_RECONNECT_SEC = 1800.0  # Пересоздаём соединение после 30 мин простоя (защита от тихого обрыва NAT/WS)
MAX_CLIENT_LIFETIME_SEC = 6 * 3600.0  # Периодический сброс внутреннего кэша pymax.App (users/messages)

_WS_RECONNECT_ERRORS: tuple[type[BaseException], ...] = (
    asyncio.TimeoutError,
    TimeoutError,
    ConnectionError,
    EOFError,
    OSError,
    *_WS_EXTRA_EXCEPTIONS,
)

_EXPIRED_AUTH_CODES = {
    "FAIL_LOGIN_TOKEN",
    "FAIL_LOGOUT_ALL",
    "INVALID_TOKEN",
    "SESSION_EXPIRED",
    "UNAUTHORIZED",
    "AUTH_REQUIRED",
}

_client: Any = None
_client_loop: asyncio.AbstractEventLoop | None = None
_client_created_monotonic: float = 0.0
_last_client_activity_monotonic: float = 0.0
_fallback_bot: Any = None
_send_locks: dict[int, asyncio.Lock] = {}
_connect_locks: dict[int, asyncio.Lock] = {}
_last_send_monotonic: float = 0.0
_last_expired_alert_at: float = 0.0

_telemetry: dict[str, Any] = {
    "sent_ok": 0,
    "sent_failed": 0,
    "not_found": 0,
    "session_expired_count": 0,
    "last_sent_at": None,
    "last_error": None,
}


def _record_error_msg(msg: str) -> None:
    txt = str(msg or "").strip()
    if len(txt) > 180:
        txt = txt[:177] + "..."
    _telemetry["last_error"] = txt or None


def get_max_telemetry() -> dict[str, Any]:
    """Возвращает текущую диагностику и телеметрию клиента MAX (без секретов)."""
    work_dir, session_name = resolve_session_location()
    session_file = Path(work_dir) / session_name
    meta = _load_auth_meta(work_dir, session_name)
    backend = str(config.MAX_BACKEND or "ws").strip().lower()
    if backend == "playwright":
        pw_dir = resolve_playwright_profile_dir()
        session_exists = pw_dir.exists()
        session_size = 0
    else:
        session_exists = session_file.exists()
        session_size = session_file.stat().st_size if session_exists else 0

    lock_busy = any(l.locked() for l in _send_locks.values())
    return {
        "sent_ok": int(_telemetry["sent_ok"]),
        "sent_failed": int(_telemetry["sent_failed"]),
        "not_found": int(_telemetry["not_found"]),
        "session_expired_count": int(_telemetry["session_expired_count"]),
        "last_sent_at": _telemetry["last_sent_at"],
        "last_error": _telemetry["last_error"],
        "lock_busy": lock_busy,
        "session_exists": session_exists,
        "session_size_bytes": session_size,
        "client_type": (meta or {}).get("client_type") or ("playwright" if backend == "playwright" else "auto"),
        "auth_method": (meta or {}).get("auth_method"),
        "is_connected": bool(_client is not None and getattr(_client, "is_connected", False)),
    }


class MaxSessionExpiredError(RuntimeError):
    """Сессия MAX отсутствует, невалидна или истекла (нужно переавторизоваться)."""


def register_bot(bot: Any) -> None:
    """Регистрирует экземпляр Telegram-бота для алертов при протухшей сессии MAX."""
    global _fallback_bot
    _fallback_bot = bot


def normalize_phone_e164(phone: str | None) -> str:
    """Приводит номер телефона к формату +7XXXXXXXXXX (или международному +...)."""
    return db.normalize_client_phone(phone)


def _get_base_dir() -> Path:
    base = getattr(config, "BASE_DIR", None)
    if base:
        return Path(base)
    return Path(__file__).resolve().parent.parent


def _resolve_raw_session_path(session_path: str | None = None) -> Path:
    raw = (session_path or config.MAX_SESSION_PATH or "max_session").strip() or "max_session"
    p = Path(raw)
    if not p.is_absolute():
        p = _get_base_dir() / p
    return p


def resolve_session_location(session_path: str | None = None) -> tuple[str, str]:
    """
    Преобразует MAX_SESSION_PATH в (work_dir, session_name) для pymax SessionStore.
    Относительные пути приводятся к корню проекта (BASE_DIR).
    """
    p = _resolve_raw_session_path(session_path)
    if p.suffix.lower() in (".db", ".sqlite", ".sqlite3"):
        return str(p.parent), p.name
    return str(p), "session.db"


def resolve_playwright_profile_dir(session_path: str | None = None) -> Path:
    """Возвращает абсолютный путь директории профиля Playwright относительно BASE_DIR."""
    p = _resolve_raw_session_path(session_path)
    if p.suffix.lower() in (".db", ".sqlite", ".sqlite3"):
        return p.parent / f"{p.stem}_pw"
    return p


def _meta_file_path(work_dir: str, session_name: str) -> Path:
    return Path(work_dir) / f"{session_name}.meta.json"


def save_auth_meta(
    work_dir: str,
    session_name: str,
    *,
    auth_method: str,
    client_type: str,
) -> None:
    """Сохраняет метаданные типа клиента (WebClient vs Client) рядом с SQLite-сессией."""
    Path(work_dir).mkdir(parents=True, exist_ok=True)
    meta_path = _meta_file_path(work_dir, session_name)
    meta_path.write_text(
        json.dumps({"auth_method": auth_method, "client_type": client_type}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load_auth_meta(work_dir: str, session_name: str) -> dict[str, Any] | None:
    meta_path = _meta_file_path(work_dir, session_name)
    if not meta_path.exists():
        return None
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _get_send_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    loop_id = id(loop)
    lock = _send_locks.get(loop_id)
    if lock is None:
        _send_locks.clear()
        lock = asyncio.Lock()
        _send_locks[loop_id] = lock
    return lock


def _get_connect_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    loop_id = id(loop)
    lock = _connect_locks.get(loop_id)
    if lock is None:
        _connect_locks.clear()
        lock = asyncio.Lock()
        _connect_locks[loop_id] = lock
    return lock


async def _load_saved_session_info(work_dir: str, session_name: str):
    db_file = Path(work_dir) / session_name
    if not db_file.exists():
        return None
    from pymax.session.store import SessionStore

    store = SessionStore(work_dir, session_name)
    try:
        return await store.load_session()
    finally:
        await asyncio.shield(_close_store_safely(store))


def _is_session_expired_error(exc: Exception) -> bool:
    if isinstance(exc, MaxSessionExpiredError):
        return True
    try:
        from pymax.exceptions import ApiError
        from pymax.protocol.enums import Opcode
    except Exception:
        ApiError = None
        Opcode = None

    if ApiError is not None and isinstance(exc, ApiError):
        err_code = str(getattr(exc, "error", "") or "").strip().upper()
        err_msg = str(getattr(exc, "message", "") or "").strip().upper()
        if Opcode is not None and getattr(exc, "opcode", None) == Opcode.LOGIN:
            return True
        if err_code in _EXPIRED_AUTH_CODES or err_msg in _EXPIRED_AUTH_CODES:
            return True
        tokens = set(err_code.replace(".", ":").split(":")) | set(err_msg.replace(".", ":").split(":"))
        return bool(tokens & _EXPIRED_AUTH_CODES)

    low = str(exc).lower()
    return "session missing or expired" in low or "fail_login_token" in low or "fail_logout_all" in low


async def _handle_expired_session(bot=None) -> None:
    """Логирует истечение сессии MAX и не чаще раза в 6 часов уведомляет админов в Telegram."""
    global _last_expired_alert_at
    _telemetry["session_expired_count"] = int(_telemetry["session_expired_count"]) + 1
    _record_error_msg("MAX session expired — run max_auth.py")
    logger.error("MAX session expired — run max_auth.py")

    target_bot = bot or _fallback_bot
    if target_bot is None:
        return

    now = time.monotonic()
    if _last_expired_alert_at > 0 and (now - _last_expired_alert_at) < EXPIRED_ALERT_COOLDOWN_SEC:
        return
    _last_expired_alert_at = now

    try:
        admin_ids = await db.get_all_admin_ids(config.ADMIN_IDS)
    except Exception as e:
        logger.warning("Не удалось получить список администраторов для алерта MAX: %s", e)
        return

    alert_text = (
        "⚠️ Сессия мессенджера MAX истекла или не выполнена авторизация.\n\n"
        "Напоминания о визитах через MAX временно не доставляются.\n"
        "Запустите на сервере: python max_auth.py"
    )
    for admin_id in admin_ids:
        try:
            await asyncio.wait_for(
                target_bot.send_message(chat_id=admin_id, text=alert_text),
                timeout=10.0,
            )
        except Exception as e:
            logger.warning("Не удалось отправить Telegram-алерт администратору %s: %s", admin_id, e)


async def _create_ws_client():
    from pymax import Client, ExtraConfig, WebClient
    from pymax.api.session.enums import DeviceType
    from pymax.auth.base import AuthFlow

    class _NonInteractiveAuthFlow(AuthFlow):
        async def authenticate(self, app):
            raise MaxSessionExpiredError("MAX session missing or expired — run max_auth.py")

    work_dir, session_name = resolve_session_location()
    session_info = await _load_saved_session_info(work_dir, session_name)
    if session_info is None or not getattr(session_info, "token", None):
        raise MaxSessionExpiredError("MAX session not found — run max_auth.py")

    extra = ExtraConfig(
        reconnect=False,
        relogin=False,
        telemetry=False,
        log_level="WARNING",
    )

    meta = _load_auth_meta(work_dir, session_name)
    client_type = str((meta or {}).get("client_type") or "").strip().lower()
    if client_type not in ("client", "web_client"):
        stored_ua = getattr(session_info, "user_agent", None)
        device_type = getattr(stored_ua, "device_type", None)
        dt_val = str(getattr(device_type, "value", device_type) or "").strip().upper()
        if device_type == DeviceType.ANDROID or dt_val == "ANDROID" or (stored_ua is None and bool(getattr(session_info, "phone", ""))):
            client_type = "client"
        else:
            client_type = "web_client"

    if client_type == "client":
        return Client(
            phone=getattr(session_info, "phone", "") or "",
            session_name=session_name,
            work_dir=work_dir,
            extra_config=extra,
            auth_flow=_NonInteractiveAuthFlow(),
        )

    return WebClient(
        session_name=session_name,
        work_dir=work_dir,
        extra_config=extra,
        auth_flow=_NonInteractiveAuthFlow(),
    )


def _get_pymax_app(client: Any) -> Any:
    if client is None:
        return None
    if hasattr(client, "__dict__"):
        app = client.__dict__.get("_BaseClient__app")
        if app is not None:
            return app
    return None


def _force_abort_transport(transport: Any) -> None:
    if transport is None:
        return
    try:
        ws = getattr(transport, "ws", None)
        if ws is not None:
            ws_tr = getattr(ws, "transport", None)
            if ws_tr is not None and hasattr(ws_tr, "abort"):
                ws_tr.abort()
        writer = getattr(transport, "_writer", None)
        if writer is not None:
            w_tr = getattr(writer, "transport", None)
            if w_tr is not None and hasattr(w_tr, "abort"):
                w_tr.abort()
            elif hasattr(writer, "close"):
                writer.close()
    except Exception:
        pass


def _force_stop_aiosqlite_conn(conn: Any) -> None:
    """
    Гарантированно останавливает рабочий non-daemon поток aiosqlite.Connection,
    чтобы интерпретатор Python не зависал в threading._shutdown().
    """
    if conn is None:
        return
    try:
        if hasattr(conn, "stop"):
            conn.stop()
    except Exception:
        pass
    try:
        t = getattr(conn, "_thread", None)
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=2.0)
    except Exception:
        pass


def _sync_cleanup_client(client: Any) -> None:
    """Синхронная аварийная зачистка транспорта и aiosqlite-потока при остановке/выходе процесса."""
    app = _get_pymax_app(client)
    if app is None:
        return
    try:
        conn_mgr = getattr(app, "connection", None)
        if conn_mgr is not None:
            _force_abort_transport(getattr(conn_mgr, "transport", None))
    except Exception:
        pass
    try:
        store = getattr(app, "store", None)
        if store is not None:
            conn = getattr(store, "conn", None)
            if conn is not None:
                _force_stop_aiosqlite_conn(conn)
                store.conn = None
    except Exception:
        pass


async def _close_store_safely(store: Any) -> None:
    """Закрывает SessionStore и дожидается завершения рабочего non-daemon потока aiosqlite."""
    if store is None:
        return
    conn_ref = getattr(store, "conn", None)
    if conn_ref is None:
        return
    try:
        await asyncio.wait_for(store.close(), timeout=2.0)
    except BaseException:
        _force_stop_aiosqlite_conn(conn_ref)
        store.conn = None
        return
    t = getattr(conn_ref, "_thread", None)
    if t is not None and t.is_alive() and t is not threading.current_thread():
        try:
            await asyncio.to_thread(t.join, 2.0)
        except BaseException:
            _force_stop_aiosqlite_conn(conn_ref)


async def close_client_safely(client: Any) -> None:
    """
    Закрывает pymax-клиента с таймаутом и принудительно дочищает транспорт, задачи
    и SessionStore (aiosqlite worker thread), даже если client.close() завис или отменён.
    """
    if client is None:
        return
    app = _get_pymax_app(client)
    try:
        await asyncio.wait_for(client.close(), timeout=CLOSE_TIMEOUT_SEC)
    except (Exception, asyncio.CancelledError) as e:
        logger.debug("Ошибка/таймаут штатного закрытия клиента MAX: %s", e)
    finally:
        if app is not None:
            for task_attr in ("_ping_task",):
                t = getattr(app, task_attr, None)
                if t is not None and not t.done():
                    t.cancel()
            telem = getattr(app, "_telemetry", None)
            if telem is not None:
                tt = getattr(telem, "_task", None)
                if tt is not None and not tt.done():
                    tt.cancel()
            conn_mgr = getattr(app, "connection", None)
            if conn_mgr is not None:
                rt = getattr(conn_mgr, "_recv_task", None)
                if rt is not None and not rt.done():
                    rt.cancel()
                for et in tuple(getattr(conn_mgr, "_event_tasks", ()) or ()):
                    if not et.done():
                        et.cancel()
                reqs = getattr(conn_mgr, "requests", None)
                if reqs is not None and hasattr(reqs, "cancel_all"):
                    try:
                        reqs.cancel_all()
                    except Exception:
                        pass
                _force_abort_transport(getattr(conn_mgr, "transport", None))
            store = getattr(app, "store", None)
            await _close_store_safely(store)


async def close_max_client() -> None:
    """Безопасно закрывает активное соединение клиента MAX при завершении или пересоздании."""
    global _client, _client_loop, _client_created_monotonic, _last_client_activity_monotonic
    client = _client
    _client = None
    _client_loop = None
    _client_created_monotonic = 0.0
    _last_client_activity_monotonic = 0.0
    if client is None:
        return
    await close_client_safely(client)


def _atexit_cleanup_max_client() -> None:
    """Не даёт одноразовым CLI/тест-скриптам зависать на threading._shutdown из-за aiosqlite-потока."""
    global _client, _client_loop, _client_created_monotonic, _last_client_activity_monotonic
    client = _client
    _client = None
    _client_loop = None
    _client_created_monotonic = 0.0
    _last_client_activity_monotonic = 0.0
    if client is not None:
        _sync_cleanup_client(client)


atexit.register(_atexit_cleanup_max_client)


def _is_cached_client_healthy(client: Any, current_loop: asyncio.AbstractEventLoop) -> bool:
    if client is None or _client_loop is not current_loop:
        return False
    if not getattr(client, "is_connected", False) or getattr(client, "me", None) is None:
        return False

    now_mono = time.monotonic()
    if _last_client_activity_monotonic > 0 and (now_mono - _last_client_activity_monotonic) > IDLE_RECONNECT_SEC:
        return False
    if _client_created_monotonic > 0 and (now_mono - _client_created_monotonic) > MAX_CLIENT_LIFETIME_SEC:
        return False

    app = _get_pymax_app(client)
    if app is not None:
        if not getattr(app, "started", False):
            return False
        conn_mgr = getattr(app, "connection", None)
        if conn_mgr is None or not getattr(conn_mgr, "is_open", False):
            return False
        transport = getattr(conn_mgr, "transport", None)
        if transport is not None and not getattr(transport, "connected", False):
            return False
    return True


def _is_connection_lost_runtime_error(exc: Exception) -> bool:
    if not isinstance(exc, RuntimeError):
        return False
    low = str(exc).lower()
    return (
        "not connected" in low
        or "closed" in low
        or "not initialized" in low
        or "connection lost" in low
    )


async def _ensure_connected_client():
    global _client, _client_loop, _client_created_monotonic, _last_client_activity_monotonic
    current_loop = asyncio.get_running_loop()
    lock = _get_connect_lock()

    async with lock:
        if _is_cached_client_healthy(_client, current_loop):
            _last_client_activity_monotonic = time.monotonic()
            return _client

        await close_max_client()
        client = await _create_ws_client()
        try:
            await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT_SEC)
        except BaseException:
            try:
                await asyncio.shield(close_client_safely(client))
            except Exception:
                _sync_cleanup_client(client)
            raise

        me = getattr(client, "me", None)
        if not me or not getattr(me, "contact", None):
            try:
                await asyncio.shield(close_client_safely(client))
            except Exception:
                _sync_cleanup_client(client)
            raise MaxSessionExpiredError("MAX session login failed: profile not returned")

        # Закрываем SQLite-соединение SessionStore сразу после connect()/login():
        # токен уже сохранён на диск, дальнейшие RPC-запросы к store не обращаются
        # (при необходимости _get_connection() откроет его лениво заново), зато
        # не-демон рабочий поток aiosqlite не висит в фоне и не блокирует threading._shutdown().
        app = _get_pymax_app(client)
        store = getattr(app, "store", None) if app is not None else None
        await _close_store_safely(store)

        now_mono = time.monotonic()
        _client = client
        _client_loop = current_loop
        _client_created_monotonic = now_mono
        _last_client_activity_monotonic = now_mono
        return client


async def _send_via_ws(norm_phone: str, text: str, bot=None) -> bool:
    """Отправляет сообщение через pymax WebSocket/TCP клиент по номеру телефона."""
    global _last_client_activity_monotonic
    from pymax.exceptions import ApiError, PyMaxError

    for attempt in range(2):
        try:
            client = await _ensure_connected_client()
        except MaxSessionExpiredError:
            await _handle_expired_session(bot)
            return False
        except _WS_RECONNECT_ERRORS as e:
            logger.warning("MAX: таймаут/ошибка подключения к серверу (попытка %s/2): %s", attempt + 1, e)
            await close_max_client()
            if attempt == 0:
                continue
            _telemetry["sent_failed"] = int(_telemetry["sent_failed"]) + 1
            _record_error_msg(f"Ошибка подключения MAX: {type(e).__name__}: {e}")
            return False
        except Exception as e:
            if _is_session_expired_error(e):
                await _handle_expired_session(bot)
                return False
            logger.warning("MAX: ошибка подключения к серверу (попытка %s/2): %s", attempt + 1, e)
            await close_max_client()
            if attempt == 0:
                continue
            _telemetry["sent_failed"] = int(_telemetry["sent_failed"]) + 1
            _record_error_msg(f"Ошибка подключения MAX: {type(e).__name__}: {e}")
            return False

        try:
            user = await asyncio.wait_for(client.search_by_phone(norm_phone), timeout=REQUEST_TIMEOUT_SEC)
        except ApiError as e:
            if _is_session_expired_error(e):
                await close_max_client()
                await _handle_expired_session(bot)
                return False
            _telemetry["not_found"] = int(_telemetry["not_found"]) + 1
            logger.info("MAX: пользователь ***%s не найден по телефону или скрыт настройками (%s)", norm_phone[-4:], e)
            return False
        except PyMaxError as e:
            _telemetry["not_found"] = int(_telemetry["not_found"]) + 1
            logger.info("MAX: контакт ***%s не найден в мессенджере (%s)", norm_phone[-4:], e)
            return False
        except _WS_RECONNECT_ERRORS as e:
            logger.warning("MAX: обрыв/таймаут при поиске ***%s (попытка %s/2): %s", norm_phone[-4:], attempt + 1, e)
            await close_max_client()
            if attempt == 0:
                continue
            _telemetry["sent_failed"] = int(_telemetry["sent_failed"]) + 1
            _record_error_msg(f"Таймаут поиска MAX: {type(e).__name__}: {e}")
            return False
        except Exception as e:
            if _is_connection_lost_runtime_error(e):
                logger.warning("MAX: потеряно соединение при поиске ***%s (попытка %s/2): %s", norm_phone[-4:], attempt + 1, e)
                await close_max_client()
                if attempt == 0:
                    continue
                _telemetry["sent_failed"] = int(_telemetry["sent_failed"]) + 1
                _record_error_msg(f"Разрыв поиска MAX: {type(e).__name__}: {e}")
                return False
            raise

        user_id = getattr(user, "id", None) if user else None
        my_id = getattr(getattr(getattr(client, "me", None), "contact", None), "id", None)
        if not user_id or not my_id:
            _telemetry["not_found"] = int(_telemetry["not_found"]) + 1
            logger.info("MAX: не удалось определить ID диалога для ***%s", norm_phone[-4:])
            return False

        chat_id = client.get_chat_id(my_id, user_id)
        try:
            await asyncio.wait_for(
                client.send_message(chat_id=chat_id, text=text, notify=True),
                timeout=REQUEST_TIMEOUT_SEC,
            )
            _last_client_activity_monotonic = time.monotonic()
            logger.info("MAX: напоминание успешно отправлено на ***%s (chat_id=%s)", norm_phone[-4:], chat_id)
            return True
        except ApiError as e:
            if _is_session_expired_error(e):
                await close_max_client()
                await _handle_expired_session(bot)
                return False
            _telemetry["sent_failed"] = int(_telemetry["sent_failed"]) + 1
            _record_error_msg(f"Ошибка API MAX: {e}")
            logger.warning("MAX: ошибка API при отправке на ***%s: %s", norm_phone[-4:], e)
            return False
        except _WS_RECONNECT_ERRORS as e:
            logger.warning("MAX: обрыв/таймаут при отправке ***%s (попытка %s/2): %s", norm_phone[-4:], attempt + 1, e)
            await close_max_client()
            if attempt == 0:
                continue
            _telemetry["sent_failed"] = int(_telemetry["sent_failed"]) + 1
            _record_error_msg(f"Таймаут отправки MAX: {type(e).__name__}: {e}")
            return False
        except Exception as e:
            if _is_connection_lost_runtime_error(e):
                logger.warning("MAX: потеряно соединение при отправке ***%s (попытка %s/2): %s", norm_phone[-4:], attempt + 1, e)
                await close_max_client()
                if attempt == 0:
                    continue
                _telemetry["sent_failed"] = int(_telemetry["sent_failed"]) + 1
                _record_error_msg(f"Разрыв отправки MAX: {type(e).__name__}: {e}")
                return False
            raise

    return False


async def _send_via_playwright(norm_phone: str, text: str, bot=None) -> bool:
    """Резервный Playwright-транспорт для web.max.ru с persistent browser profile."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning(
            "MAX_BACKEND=playwright выбран, но пакет playwright не установлен (pip install playwright && playwright install chromium)"
        )
        return False

    profile_dir = resolve_playwright_profile_dir()
    if not profile_dir.exists():
        await _handle_expired_session(bot)
        return False

    context = None
    try:
        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=True,
            )
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://web.max.ru", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2.0)

            login_marker = page.locator("canvas, [data-testid*='qr'], input[type='tel']")
            search_input = page.locator("input[type='search'], input[placeholder*='Поиск'], input[placeholder*='поиск']").first

            if await search_input.count() == 0 and await login_marker.count() > 0:
                await _handle_expired_session(bot)
                return False

            if await search_input.count() == 0:
                logger.warning("MAX (playwright): не найдено поле поиска контакта на web.max.ru")
                return False

            await search_input.click()
            await search_input.fill(norm_phone)
            await asyncio.sleep(1.5)

            first_result = page.locator("[role='listitem'], [class*='chat'], [class*='contact']").first
            if await first_result.count() == 0:
                logger.info("MAX (playwright): пользователь ***%s не найден", norm_phone[-4:])
                return False
            await first_result.click()
            await asyncio.sleep(0.5)

            msg_input = page.locator("[contenteditable='true'], textarea").last
            if await msg_input.count() == 0:
                logger.warning("MAX (playwright): не найдено поле ввода сообщения")
                return False

            await msg_input.click()
            await msg_input.fill(text)
            await page.keyboard.press("Enter")
            await asyncio.sleep(1.0)
            logger.info("MAX (playwright): напоминание отправлено на ***%s", norm_phone[-4:])
            return True
    except Exception as e:
        logger.warning("MAX (playwright): ошибка отправки на ***%s: %s", norm_phone[-4:], e)
        return False
    finally:
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass


async def _wait_anti_spam_slot() -> None:
    """Выдерживает случайную паузу 15–35 секунд только в недостающей части перед отправкой."""
    if _last_send_monotonic <= 0:
        return
    target_delay = random.uniform(MIN_SEND_DELAY_SEC, MAX_SEND_DELAY_SEC)
    elapsed = time.monotonic() - _last_send_monotonic
    remaining = max(0.0, target_delay - elapsed)
    if remaining > 0:
        logger.debug("MAX anti-spam delay: ждём %.1f сек перед следующей отправкой", remaining)
        await asyncio.sleep(remaining)


async def check_max_connection(active_ping: bool = False) -> dict[str, Any]:
    """
    Проверяет наличие файлов сессии MAX (session.db + *.meta.json) и, при active_ping=True,
    выполняет лёгкое подключение/отключение pymax под asyncio.wait_for(timeout=15) без отправки сообщений.
    """
    work_dir, session_name = resolve_session_location()
    session_file = Path(work_dir) / session_name
    meta_file = _meta_file_path(work_dir, session_name)
    meta = _load_auth_meta(work_dir, session_name)
    backend = str(config.MAX_BACKEND or "ws").strip().lower()

    if backend == "playwright":
        pw_dir = resolve_playwright_profile_dir()
        session_exists = pw_dir.exists()
        result: dict[str, Any] = {
            "ok": session_exists,
            "backend": backend,
            "session_exists": session_exists,
            "session_size_bytes": 0,
            "meta_exists": False,
            "client_type": "playwright",
            "auth_method": "playwright",
            "live_checked": active_ping,
            "latency_ms": None,
            "account_id": None,
            "error": None if session_exists else "Профиль Playwright не найден",
            "note": "Проверка браузера без запуска headless-окна" if active_ping else None,
        }
        return result

    session_exists = session_file.exists()
    session_size = session_file.stat().st_size if session_exists else 0
    meta_exists = meta_file.exists()
    client_type = (meta or {}).get("client_type") or "auto"
    auth_method = (meta or {}).get("auth_method")

    result = {
        "ok": session_exists,
        "backend": backend,
        "session_exists": session_exists,
        "session_size_bytes": session_size,
        "meta_exists": meta_exists,
        "client_type": client_type,
        "auth_method": auth_method,
        "live_checked": False,
        "latency_ms": None,
        "account_id": None,
        "error": None if session_exists else "Файл сессии отсутствует (запустите max_auth.py)",
        "note": None,
    }
    if not active_ping:
        return result

    result["live_checked"] = True
    if not session_exists:
        return result

    if any(l.locked() for l in _send_locks.values()):
        result["live_checked"] = False
        result["ok"] = session_exists
        result["note"] = "Активна очередь отправки — пинг отложен, чтобы не мешать"
        return result

    async def _do_live_ping() -> dict[str, Any]:
        send_lock = _get_send_lock()
        async with send_lock:
            connect_lock = _get_connect_lock()
            async with connect_lock:
                await close_max_client()
                t0 = time.monotonic()
                temp_client = await _create_ws_client()
                try:
                    await temp_client.connect()
                    me = getattr(temp_client, "me", None)
                    contact = getattr(me, "contact", None)
                    acc_id = getattr(contact, "id", None)
                    latency_ms = round((time.monotonic() - t0) * 1000.0, 1)
                    result["ok"] = bool(acc_id)
                    result["latency_ms"] = latency_ms
                    result["account_id"] = acc_id
                    if acc_id:
                        _telemetry["last_error"] = None
                    else:
                        result["error"] = "Профиль MAX не получен"
                    return result
                finally:
                    try:
                        await asyncio.shield(close_client_safely(temp_client))
                    except Exception:
                        _sync_cleanup_client(temp_client)

    try:
        return await asyncio.wait_for(_do_live_ping(), timeout=15.0)
    except MaxSessionExpiredError:
        result["ok"] = False
        result["error"] = "Сессия истекла — запустите max_auth.py"
        return result
    except asyncio.TimeoutError:
        result["ok"] = False
        result["error"] = "Таймаут подключения к MAX (15с)"
        return result
    except Exception as e:
        if _is_session_expired_error(e):
            result["ok"] = False
            result["error"] = "Сессия истекла — запустите max_auth.py"
        else:
            result["ok"] = False
            result["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        return result


async def send_max_reminder(phone: str, text: str, bot=None) -> bool:
    """
    Отправляет одиночное текстовое напоминание о визите пользователю мессенджера MAX.

    - Приводит номер телефона к +7XXXXXXXXXX
    - Сериализует отправки через asyncio.Lock и выдерживает недостающую часть паузы 15–35 сек
    - Ограничивает все сетевые вызовы таймаутами asyncio.wait_for
    - Возвращает True при успешной доставке и False при любой ошибке (не бросает исключений наружу)
    """
    global _last_send_monotonic

    if not text or not str(text).strip():
        logger.warning("MAX: пустой текст напоминания, отправка пропущена")
        return False

    norm_phone = normalize_phone_e164(phone)
    if not norm_phone:
        _telemetry["sent_failed"] = int(_telemetry["sent_failed"]) + 1
        _record_error_msg("Некорректный номер телефона")
        logger.warning("MAX: некорректный номер телефона '%s', отправка пропущена", phone)
        return False

    backend = str(config.MAX_BACKEND or "ws").strip().lower()
    lock = _get_send_lock()

    async with lock:
        try:
            await _wait_anti_spam_slot()
            if backend == "playwright":
                sent = await _send_via_playwright(norm_phone, str(text), bot=bot)
                if not sent and not _telemetry["last_error"]:
                    _telemetry["sent_failed"] = int(_telemetry["sent_failed"]) + 1
                    _record_error_msg("Ошибка отправки через Playwright")
            else:
                sent = await _send_via_ws(norm_phone, str(text), bot=bot)
            if sent:
                _last_send_monotonic = time.monotonic()
                _telemetry["sent_ok"] = int(_telemetry["sent_ok"]) + 1
                _telemetry["last_sent_at"] = db.now_msk().isoformat(timespec="seconds")
                _telemetry["last_error"] = None
            return bool(sent)
        except MaxSessionExpiredError:
            await _handle_expired_session(bot)
            return False
        except Exception as e:
            _telemetry["sent_failed"] = int(_telemetry["sent_failed"]) + 1
            _record_error_msg(f"{type(e).__name__}: {e}")
            logger.warning("MAX: неожиданная ошибка при отправке ***%s: %s", norm_phone[-4:], e)
            return False
