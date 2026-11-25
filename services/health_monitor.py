"""
Модуль мониторинга и диагностики подсистем бота Sahar&Vosk («Статус систем»).

Работает исключительно на стандартной библиотеке Python и уже установленных
асинхронных пакетах (aiohttp / aiosqlite), с безопасной поддержкой как Linux VPS
(/proc/*, os.getloadavg), так и локального запуска на Windows (для тестов).
"""

from __future__ import annotations

import asyncio
import ctypes
import html
import logging
import os
import platform
import re
import shutil
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import config
from database import db
from services import booking_service as bs
from services import max_sender, webhook_handler, yclients_api
from services.scheduler import MSK_TZ, scheduler

BASE_DIR = Path(getattr(config, "BASE_DIR", Path(__file__).resolve().parent.parent))
MAX_LOG_ENTRIES = 20
MAX_LOG_MESSAGE_LEN = 220

_bot_started_at: datetime = db.now_msk()


def set_bot_started_at(dt: datetime | None = None) -> None:
    global _bot_started_at
    _bot_started_at = dt or db.now_msk()


def get_bot_started_at() -> datetime:
    return _bot_started_at


def _secret_literals() -> list[str]:
    candidates = [
        getattr(config, "BOT_TOKEN", ""),
        getattr(config, "YCLIENTS_PARTNER_TOKEN", ""),
        getattr(config, "YCLIENTS_USER_TOKEN", ""),
        getattr(config, "YCLIENTS_PASSWORD", ""),
        getattr(config, "YCLIENTS_WEBHOOK_SECRET", ""),
    ]
    secrets: list[str] = []
    for raw in candidates:
        s = str(raw or "").strip()
        if len(s) >= 6:
            secrets.append(s)
            encoded = quote(s, safe="")
            if encoded and encoded != s:
                secrets.append(encoded)
            if ":" in s:
                part = s.split(":", 1)[1].strip()
                if len(part) >= 8:
                    secrets.append(part)
    # Длинные значения заменяем первыми, чтобы не оставлять хвостов токена
    secrets.sort(key=len, reverse=True)
    return secrets


def sanitize_secrets(text: str | None) -> str:
    """Маскирует токены бота, ключи YCLIENTS, прокси-пароли и секреты вебхука в логах и отчётах."""
    if not text:
        return ""
    res = str(text)
    for sec in _secret_literals():
        if sec in res:
            res = res.replace(sec, "***")
    # Общие паттерны на случай динамически подставленных секретов (в т.ч. в URL bot<id>:<token>, прокси, заголовках)
    res = re.sub(r"(?:(?<=bot)|(?<!\w))\d{6,15}:[A-Za-z0-9_-]{20,}\b", "***", res)
    res = re.sub(r"(?i)(Bearer|User)\s+[A-Za-z0-9._\-]{8,}", r"\1 ***", res)
    res = re.sub(r"(?i)(https?|socks[45]h?)://[^:\s/@]+:[^@\s/]+@", r"\1://***:***@", res)
    res = re.sub(r"(?i)([?&](?:secret|token|password|access_token|api_key|apikey|auth)=)[^&\s]+", r"\1***", res)
    res = re.sub(r"(/yclients/webhook/)[^/?\s]+", r"\1***", res)
    return res


class RecentErrorsHandler(logging.Handler):
    """Потокобезопасный кольцевой буфер последних WARNING/ERROR записей в MSK с защитой от рекурсии."""

    def __init__(self, maxlen: int = MAX_LOG_ENTRIES) -> None:
        super().__init__(level=logging.WARNING)
        self._records: deque[dict[str, str]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._local = threading.local()
        self.warning_count: int = 0
        self.error_count: int = 0

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._local, "emitting", False):
            return
        self._local.emitting = True
        try:
            ts = datetime.fromtimestamp(record.created, MSK_TZ).strftime("%d.%m %H:%M:%S")
            try:
                msg = record.getMessage()
            except Exception:
                msg = f"{record.msg} (args={record.args})"

            exc_str = record.exc_text or ""
            if not exc_str and record.exc_info:
                try:
                    exc_str = logging.Formatter().formatException(record.exc_info)
                except Exception:
                    exc_str = ""
            if exc_str:
                last_line = exc_str.strip().splitlines()[-1]
                msg = f"{msg} | {last_line}" if msg else last_line

            msg_clean = sanitize_secrets(" ".join(str(msg).split()))
            if len(msg_clean) > MAX_LOG_MESSAGE_LEN:
                msg_clean = msg_clean[: MAX_LOG_MESSAGE_LEN - 3] + "..."

            entry = {
                "ts": ts,
                "level": str(record.levelname or "WARNING").upper(),
                "logger_name": sanitize_secrets(str(record.name or "root"))[:40],
                "message": msg_clean,
            }
            with self._lock:
                self._records.append(entry)
                if record.levelno >= logging.ERROR:
                    self.error_count += 1
                elif record.levelno >= logging.WARNING:
                    self.warning_count += 1
        except Exception:
            self.handleError(record)
        finally:
            self._local.emitting = False

    def get_records(self) -> list[dict[str, str]]:
        with self._lock:
            return list(reversed(self._records))

    def get_counts(self) -> tuple[int, int]:
        with self._lock:
            return self.warning_count, self.error_count

    def buffered_len(self) -> int:
        with self._lock:
            return len(self._records)

    def clear_buffer(self) -> None:
        with self._lock:
            self._records.clear()


_errors_handler: RecentErrorsHandler | None = None
_handler_lock = threading.Lock()


def get_error_handler() -> RecentErrorsHandler:
    global _errors_handler
    with _handler_lock:
        if _errors_handler is None:
            _errors_handler = RecentErrorsHandler(maxlen=MAX_LOG_ENTRIES)
        return _errors_handler


def install_error_handler() -> RecentErrorsHandler:
    """Подключает кольцевой буфер ошибок к корневому логгеру (идемпотентно)."""
    handler = get_error_handler()
    root_logger = logging.getLogger()
    if handler not in root_logger.handlers:
        root_logger.addHandler(handler)
    return handler


def clear_error_buffer() -> None:
    get_error_handler().clear_buffer()


def _format_duration(seconds: float) -> str:
    sec = max(0, int(seconds))
    days, rem = divmod(sec, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}д")
    if hours or days:
        parts.append(f"{hours}ч")
    if minutes or hours or days:
        parts.append(f"{minutes}м")
    if not parts:
        parts.append(f"{secs}с")
    return " ".join(parts)


def _format_size(num_bytes: int | float) -> str:
    size = max(0.0, float(num_bytes))
    if size >= 1024 ** 3:
        return f"{size / (1024 ** 3):.2f} ГБ"
    if size >= 1024 ** 2:
        return f"{size / (1024 ** 2):.2f} МБ"
    if size >= 1024:
        return f"{size / 1024:.1f} КБ"
    return f"{int(size)} Б"


def _read_git_info_sync() -> str:
    git_entry = BASE_DIR / ".git"
    git_dir = git_entry
    if git_entry.is_file():
        try:
            raw_ptr = git_entry.read_text(encoding="utf-8", errors="replace").strip()
            if raw_ptr.startswith("gitdir:"):
                ptr_path = Path(raw_ptr.split(":", 1)[1].strip())
                git_dir = ptr_path if ptr_path.is_absolute() else (BASE_DIR / ptr_path)
        except Exception:
            return "недоступно"

    head_file = git_dir / "HEAD"
    if not head_file.exists():
        return "не git-репозиторий"
    try:
        head_raw = head_file.read_text(encoding="utf-8", errors="replace").strip()
        if head_raw.startswith("ref:"):
            ref_rel = head_raw.split(":", 1)[1].strip()
            branch = ref_rel.replace("refs/heads/", "", 1)
            ref_path = git_dir / ref_rel
            commit = ""
            if ref_path.exists():
                commit = ref_path.read_text(encoding="utf-8", errors="replace").strip()[:7]
            elif (git_dir / "packed-refs").exists():
                for line in (git_dir / "packed-refs").read_text(encoding="utf-8", errors="replace").splitlines():
                    if not line or line.startswith(("#", "^")):
                        continue
                    parts = line.split(" ", 1)
                    if len(parts) == 2 and parts[1].strip() == ref_rel:
                        commit = parts[0].strip()[:7]
                        break
            return f"{branch}@{commit}" if commit else branch
        return f"detached@{head_raw[:7]}"
    except Exception:
        return "недоступно"


def _read_process_rss_mb_sync() -> float | None:
    proc_status = Path("/proc/self/status")
    if proc_status.exists():
        try:
            for line in proc_status.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        return round(int(parts[1]) / 1024.0, 1)
        except Exception:
            pass

    if os.name == "nt":
        try:
            class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_ulong),
                    ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = _PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(counters)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return round(counters.WorkingSetSize / (1024.0 * 1024.0), 1)
        except Exception:
            pass
    else:
        try:
            import resource
            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            if rss > 0:
                return round(rss / 1024.0, 1)
        except Exception:
            pass
    return None


def _read_vps_metrics_sync() -> dict[str, Any]:
    # 1. Load Average
    load_str = "N/A (Windows)"
    if hasattr(os, "getloadavg"):
        try:
            l1, l5, l15 = os.getloadavg()
            load_str = f"{l1:.2f} / {l5:.2f} / {l15:.2f}"
        except OSError:
            pass
    elif Path("/proc/loadavg").exists():
        try:
            parts = Path("/proc/loadavg").read_text(encoding="utf-8", errors="replace").split()
            if len(parts) >= 3:
                load_str = f"{parts[0]} / {parts[1]} / {parts[2]}"
        except Exception:
            pass

    # 2. RAM & Swap
    ram_info: tuple[float, float, float] | None = None
    swap_info: tuple[float, float, float] | None = None
    meminfo_path = Path("/proc/meminfo")
    if meminfo_path.exists():
        try:
            kv: dict[str, int] = {}
            for line in meminfo_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if ":" in line:
                    k, rest = line.split(":", 1)
                    num = rest.strip().split(" ", 1)[0]
                    if num.isdigit():
                        kv[k] = int(num)
            mem_total_kb = kv.get("MemTotal", 0)
            mem_avail_kb = kv.get("MemAvailable", kv.get("MemFree", 0) + kv.get("Buffers", 0) + kv.get("Cached", 0))
            if mem_total_kb > 0:
                mem_used_kb = max(0, mem_total_kb - mem_avail_kb)
                ram_info = (
                    round(mem_used_kb / 1024.0, 0),
                    round(mem_total_kb / 1024.0, 0),
                    round(mem_used_kb / mem_total_kb * 100.0, 1),
                )
            swap_total_kb = kv.get("SwapTotal", 0)
            swap_free_kb = kv.get("SwapFree", 0)
            if swap_total_kb > 0:
                swap_used_kb = max(0, swap_total_kb - swap_free_kb)
                swap_info = (
                    round(swap_used_kb / 1024.0, 0),
                    round(swap_total_kb / 1024.0, 0),
                    round(swap_used_kb / swap_total_kb * 100.0, 1),
                )
            else:
                swap_info = (0.0, 0.0, 0.0)
        except Exception:
            pass
    elif os.name == "nt":
        try:
            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = _MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(stat)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)) and stat.ullTotalPhys > 0:
                total_mb = stat.ullTotalPhys / (1024.0 * 1024.0)
                used_mb = (stat.ullTotalPhys - stat.ullAvailPhys) / (1024.0 * 1024.0)
                pct = used_mb / total_mb * 100.0
                ram_info = (round(used_mb, 0), round(total_mb, 0), round(pct, 1))
        except Exception:
            pass

    # 3. Диск
    try:
        disk_target = BASE_DIR if BASE_DIR.exists() else Path.cwd()
        disk_usage = shutil.disk_usage(disk_target)
        disk_total_gb = round(disk_usage.total / (1024 ** 3), 1)
        disk_free_gb = round(disk_usage.free / (1024 ** 3), 1)
        disk_used_pct = round(disk_usage.used / disk_usage.total * 100.0, 1) if disk_usage.total > 0 else 0.0
    except OSError:
        disk_total_gb, disk_free_gb, disk_used_pct = 0.0, 0.0, 0.0

    # 4. Аптайм сервера
    server_uptime_str = "недоступно"
    if Path("/proc/uptime").exists():
        try:
            raw_up = Path("/proc/uptime").read_text(encoding="utf-8", errors="replace").split()[0]
            server_uptime_str = _format_duration(float(raw_up))
        except Exception:
            pass
    elif os.name == "nt":
        try:
            ticks_ms = ctypes.windll.kernel32.GetTickCount64()
            server_uptime_str = _format_duration(ticks_ms / 1000.0)
        except Exception:
            pass

    return {
        "load_avg": load_str,
        "ram": ram_info,
        "swap": swap_info,
        "disk_free_gb": disk_free_gb,
        "disk_total_gb": disk_total_gb,
        "disk_used_pct": disk_used_pct,
        "server_uptime": server_uptime_str,
    }


def _read_db_and_catalog_files_sync() -> dict[str, Any]:
    raw_db = Path(db.DB_NAME)
    db_path = raw_db if raw_db.is_absolute() else (BASE_DIR / raw_db)
    wal_path = Path(f"{db_path}-wal")
    shm_path = Path(f"{db_path}-shm")

    db_size = db_path.stat().st_size if db_path.exists() else 0
    wal_size = wal_path.stat().st_size if wal_path.exists() else 0
    shm_size = shm_path.stat().st_size if shm_path.exists() else 0

    # Бэкапы
    backups_dir = BASE_DIR / "backups"
    latest_backup = None
    if backups_dir.is_dir():
        newest_stat = None
        newest_name = ""
        try:
            for f in backups_dir.iterdir():
                if f.name.startswith("."):
                    continue
                try:
                    if not f.is_file():
                        continue
                    st = f.stat()
                except OSError:
                    continue
                if newest_stat is None or st.st_mtime > newest_stat.st_mtime:
                    newest_stat = st
                    newest_name = f.name
        except OSError:
            newest_stat = None
        if newest_stat is not None:
            mtime_msk = datetime.fromtimestamp(newest_stat.st_mtime, MSK_TZ).replace(tzinfo=None)
            age_hours = max(0.0, (db.now_msk() - mtime_msk).total_seconds() / 3600.0)
            latest_backup = {
                "name": newest_name,
                "size": newest_stat.st_size,
                "mtime_str": mtime_msk.strftime("%d.%m.%Y %H:%M"),
                "age_hours": age_hours,
            }

    # Каталог services/booking_data.py
    bdata_path = BASE_DIR / "services" / "booking_data.py"
    bdata_mtime = "отсутствует"
    if bdata_path.exists():
        bdata_mtime = datetime.fromtimestamp(bdata_path.stat().st_mtime, MSK_TZ).strftime("%d.%m.%Y %H:%M")

    services = bs.SERVICES or {}
    masters = bs.MASTERS or {}

    def _cnt_srv(br: str) -> int:
        return sum(1 for s in services.values() if br in s.get("branches", [s.get("branch_id")]))

    def _cnt_mst(br: str) -> int:
        return sum(1 for m in masters.values() if br in m.get("branches", []))

    return {
        "db_size": db_size,
        "wal_size": wal_size,
        "shm_size": shm_size,
        "latest_backup": latest_backup,
        "bdata_mtime": bdata_mtime,
        "srv_total": len(services),
        "srv_pr": _cnt_srv("prazhskaya"),
        "srv_len": _cnt_srv("leningradskaya"),
        "mst_total": len(masters),
        "mst_pr": _cnt_mst("prazhskaya"),
        "mst_len": _cnt_mst("leningradskaya"),
    }


def _collect_sync_env_and_files() -> dict[str, Any]:
    return {
        "pid": os.getpid(),
        "python_ver": platform.python_version(),
        "git_info": _read_git_info_sync(),
        "rss_mb": _read_process_rss_mb_sync(),
        "vps": _read_vps_metrics_sync(),
        "files": _read_db_and_catalog_files_sync(),
    }


async def _collect_db_health() -> dict[str, Any]:
    t0 = time.monotonic()
    async with db._connect() as conn:
        await conn.execute("SELECT 1")
        ping_ms = round((time.monotonic() - t0) * 1000.0, 2)

        jm_row = await (await conn.execute("PRAGMA journal_mode;")).fetchone()
        fk_row = await (await conn.execute("PRAGMA foreign_keys;")).fetchone()
        bt_row = await (await conn.execute("PRAGMA busy_timeout;")).fetchone()

        await conn.execute("PRAGMA busy_timeout=3000;")
        users_cnt = (await (await conn.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
        active_total = (await (await conn.execute("SELECT COUNT(*) FROM appointments WHERE status = 'active'")).fetchone())[0]
        active_max_only = (
            await (
                await conn.execute(
                    "SELECT COUNT(*) FROM appointments WHERE status = 'active' AND (telegram_id IS NULL OR telegram_id <= 0)"
                )
            ).fetchone()
        )[0]
        completed_cnt = (
            await (await conn.execute("SELECT COUNT(*) FROM appointments WHERE status = 'completed'")).fetchone()
        )[0]
        steps_cnt = (await (await conn.execute("SELECT COUNT(*) FROM step_content")).fetchone())[0]

    return {
        "ok": True,
        "ping_ms": ping_ms,
        "journal_mode": str(jm_row[0]) if jm_row else "?",
        "foreign_keys": int(fk_row[0]) if fk_row else 0,
        "busy_timeout": int(bt_row[0]) if bt_row else 0,
        "users_cnt": int(users_cnt),
        "active_total": int(active_total),
        "active_max_only": int(active_max_only),
        "completed_cnt": int(completed_cnt),
        "steps_cnt": int(steps_cnt),
    }


async def _ping_yclients(include_network_ping: bool) -> dict[str, Any]:
    host = "api.yclients.ru"
    if not include_network_ping:
        return {"checked": False, "host": host, "status_code": None, "latency_ms": None, "error": None}

    company_id = next((v for v in (config.YCLIENTS_COMPANIES or {}).values() if v), 0)
    url = f"{yclients_api.BASE_URL}/company/{company_id}" if company_id else yclients_api.BASE_URL
    headers = yclients_api._get_headers() if getattr(config, "YCLIENTS_PARTNER_TOKEN", "") else {"Accept": "application/json"}

    t0 = time.monotonic()

    async def _do_req() -> int:
        session = await yclients_api.get_http_session()
        async with session.get(url, headers=headers, timeout=yclients_api.aiohttp.ClientTimeout(total=4)) as resp:
            return resp.status

    try:
        status_code = await asyncio.wait_for(_do_req(), timeout=4.5)
        latency_ms = round((time.monotonic() - t0) * 1000.0, 1)
        return {
            "checked": True,
            "host": host,
            "status_code": status_code,
            "latency_ms": latency_ms,
            "error": None,
        }
    except Exception as e:
        return {
            "checked": True,
            "host": host,
            "status_code": None,
            "latency_ms": round((time.monotonic() - t0) * 1000.0, 1),
            "error": sanitize_secrets(f"{type(e).__name__}: {e}")[:120],
        }


async def _ping_local_webhook(include_network_ping: bool) -> dict[str, Any]:
    if not config.YCLIENTS_WEBHOOK_ENABLED:
        return {"enabled": False, "checked": False, "ok": False, "status_code": None, "latency_ms": None, "error": None}
    if not include_network_ping:
        return {"enabled": True, "checked": False, "ok": True, "status_code": None, "latency_ms": None, "error": None}

    url = f"http://127.0.0.1:{config.YCLIENTS_WEBHOOK_PORT}/health"
    t0 = time.monotonic()

    async def _do_req() -> int:
        session = await yclients_api.get_http_session()
        async with session.get(url, timeout=yclients_api.aiohttp.ClientTimeout(total=3)) as resp:
            return resp.status

    try:
        status_code = await asyncio.wait_for(_do_req(), timeout=3.5)
        latency_ms = round((time.monotonic() - t0) * 1000.0, 1)
        return {
            "enabled": True,
            "checked": True,
            "ok": status_code == 200,
            "status_code": status_code,
            "latency_ms": latency_ms,
            "error": None,
        }
    except Exception as e:
        return {
            "enabled": True,
            "checked": True,
            "ok": False,
            "status_code": None,
            "latency_ms": round((time.monotonic() - t0) * 1000.0, 1),
            "error": sanitize_secrets(f"{type(e).__name__}: {e}")[:120],
        }


def _collect_scheduler_status() -> dict[str, Any]:
    running = bool(getattr(scheduler, "running", False))
    tz_str = str(getattr(scheduler, "timezone", "UTC+03:00"))
    jobs = scheduler.get_jobs()

    counts = {"24h": 0, "1h": 0, "nps": 0, "repeat": 0, "digest": 0, "other": 0}
    nearest_dt: datetime | None = None
    nearest_id: str | None = None

    for job in jobs:
        jid = str(job.id or "")
        if jid.startswith("rem_24h_"):
            counts["24h"] += 1
        elif jid.startswith("rem_1h_"):
            counts["1h"] += 1
        elif jid.startswith("rem_after_"):
            counts["nps"] += 1
        elif jid.startswith("rem_rep_"):
            counts["repeat"] += 1
        elif jid == "weekly_management_digest":
            counts["digest"] += 1
        else:
            counts["other"] += 1

        nrt = getattr(job, "next_run_time", None)
        if nrt is not None:
            nrt_msk = nrt.astimezone(MSK_TZ) if nrt.tzinfo else nrt.replace(tzinfo=MSK_TZ)
            if nearest_dt is None or nrt_msk < nearest_dt:
                nearest_dt = nrt_msk
                nearest_id = jid

    return {
        "running": running,
        "tz": tz_str,
        "total": len(jobs),
        "counts": counts,
        "nearest_str": nearest_dt.strftime("%d.%m %H:%M МСК") if nearest_dt else None,
        "nearest_id": nearest_id,
    }


def clip_html_report(html_text: str, max_units: int = 4096) -> str:
    """Аккуратно ограничивает HTML-отчёт по строкам без разрыва открытых HTML-тегов и сущностей."""
    if db._utf16_len(html_text) <= max_units:
        return html_text
    lines = html_text.splitlines()
    suffix = "\n<i>…отчёт сокращён до лимита Telegram</i>"
    acc: list[str] = []
    for line in lines:
        candidate = "\n".join(acc + [line]) + suffix
        if db._utf16_len(candidate) > max_units:
            break
        acc.append(line)
    if acc:
        return "\n".join(acc) + suffix
    plain = db._clip_utf16(re.sub(r"<[^>]+>", "", html_text), max_units)
    return re.sub(r"&[A-Za-z0-9#]*$", "", plain)


def _esc(val: Any) -> str:
    return html.escape(sanitize_secrets(str(val)))


async def collect_system_status(
    bot,
    include_network_ping: bool = True,
    check_max_live: bool = False,
) -> str:
    """
    Собирает комплексную диагностику всех 7 подсистем Sahar&Vosk в виде HTML-отчёта.
    Все I/O и сетевые проверки выполняются параллельно через asyncio.gather с таймаутами.
    """
    now = db.now_msk()
    proc_uptime = _format_duration((now - get_bot_started_at()).total_seconds())
    async_tasks_count = len(asyncio.all_tasks())
    warn_total, err_total = get_error_handler().get_counts()

    sync_res, db_res, yc_res, wh_ping_res, max_conn_res = await asyncio.gather(
        asyncio.to_thread(_collect_sync_env_and_files),
        asyncio.wait_for(_collect_db_health(), timeout=4.0),
        _ping_yclients(include_network_ping),
        _ping_local_webhook(include_network_ping),
        max_sender.check_max_connection(active_ping=check_max_live),
        return_exceptions=True,
    )

    if isinstance(sync_res, Exception):
        sync_res = {"pid": os.getpid(), "python_ver": platform.python_version(), "git_info": "?", "rss_mb": None, "vps": {}, "files": {}}
    if isinstance(db_res, Exception):
        db_res = {"ok": False, "error": sanitize_secrets(f"{type(db_res).__name__}: {db_res}")}
    if isinstance(yc_res, Exception):
        yc_res = {"checked": True, "host": "api.yclients.ru", "status_code": None, "latency_ms": None, "error": sanitize_secrets(str(yc_res))}
    if isinstance(wh_ping_res, Exception):
        wh_ping_res = {"enabled": config.YCLIENTS_WEBHOOK_ENABLED, "checked": True, "ok": False, "status_code": None, "latency_ms": None, "error": sanitize_secrets(str(wh_ping_res))}
    if isinstance(max_conn_res, Exception):
        max_conn_res = {"ok": False, "session_exists": False, "live_checked": check_max_live, "error": sanitize_secrets(str(max_conn_res))}

    wh_tel = webhook_handler.get_webhook_telemetry()
    max_tel = max_sender.get_max_telemetry()
    sched_info = _collect_scheduler_status()

    vps = sync_res.get("vps") or {}
    files = sync_res.get("files") or {}

    # 1. Бот и среда
    rss_str = f"{sync_res['rss_mb']} МБ" if sync_res.get("rss_mb") is not None else "N/A"
    bot_dot = "🟢" if err_total == 0 else "🟡"
    sec1 = (
        f"1️⃣ <b>🤖 Бот и среда</b> {bot_dot}\n"
        f"• Аптайм: <code>{_esc(proc_uptime)}</code> | PID: <code>{sync_res['pid']}</code> | Python <code>{_esc(sync_res['python_ver'])}</code>\n"
        f"• Git: <code>{_esc(sync_res.get('git_info'))}</code> | RSS: <code>{_esc(rss_str)}</code> | Tasks: <code>{async_tasks_count}</code>\n"
        f"• Логи с запуска: <code>{warn_total}</code> warn / <code>{err_total}</code> err"
    )

    # 2. Сервер (VPS)
    ram = vps.get("ram")
    swap = vps.get("swap")
    disk_pct = float(vps.get("disk_used_pct") or 0.0)
    disk_dot = "🔴" if disk_pct >= 90.0 else ("🟡" if disk_pct >= 80.0 else "🟢")
    ram_str = f"{int(ram[0])}/{int(ram[1])} МБ ({ram[2]}%)" if ram else "N/A"
    if swap:
        swap_str = f"{int(swap[0])}/{int(swap[1])} МБ ({swap[2]}%)" if swap[1] > 0 else "0 МБ"
    else:
        swap_str = "N/A"
    sec2 = (
        f"2️⃣ <b>🖥 Сервер (VPS)</b> {disk_dot}\n"
        f"• LA: <code>{_esc(vps.get('load_avg', 'N/A'))}</code> | Up: <code>{_esc(vps.get('server_uptime', 'N/A'))}</code>\n"
        f"• RAM: <code>{_esc(ram_str)}</code> | Swap: <code>{_esc(swap_str)}</code>\n"
        f"• Диск: своб. <code>{vps.get('disk_free_gb', 0)}/{vps.get('disk_total_gb', 0)} ГБ</code> (занято <code>{disk_pct}%</code>)"
    )

    # 3. БД и бэкапы
    bkp = files.get("latest_backup")
    if not bkp:
        bkp_dot = "🟡"
        bkp_str = "нет файлов в backups/"
    else:
        bkp_dot = "🟡" if bkp["age_hours"] > 26.0 else "🟢"
        bkp_str = f"{_esc(bkp['name'])} ({bkp['mtime_str']}, {_format_size(bkp['size'])})"

    if db_res.get("ok"):
        db_dot = "🟡" if bkp_dot == "🟡" else "🟢"
        fk_str = "ON" if db_res.get("foreign_keys") else "OFF"
        sec3 = (
            f"3️⃣ <b>🗄 БД SQLite и бэкапы</b> {db_dot}\n"
            f"• Ping: <code>{db_res['ping_ms']} мс</code> | <code>{_esc(db_res['journal_mode'])}</code> | FK:<code>{fk_str}</code> | busy:<code>{db_res['busy_timeout']}</code>\n"
            f"• Файлы: DB <code>{_format_size(files.get('db_size', 0))}</code> | WAL <code>{_format_size(files.get('wal_size', 0))}</code> | SHM <code>{_format_size(files.get('shm_size', 0))}</code>\n"
            f"• Записи: TG-юзеры <code>{db_res['users_cnt']}</code> | активных <code>{db_res['active_total']}</code> (MAX-only <code>{db_res['active_max_only']}</code>) | заверш. <code>{db_res['completed_cnt']}</code> | шагов <code>{db_res['steps_cnt']}</code>\n"
            f"• Бэкап {bkp_dot}: <code>{bkp_str}</code>"
        )
    else:
        sec3 = (
            f"3️⃣ <b>🗄 БД SQLite и бэкапы</b> 🔴\n"
            f"• Ошибка БД: <code>{_esc(db_res.get('error', 'unknown'))}</code>\n"
            f"• Бэкап {bkp_dot}: <code>{bkp_str}</code>"
        )

    # 4. YCLIENTS API и каталог
    if not yc_res.get("checked"):
        yc_dot = "🟡"
        yc_ping_str = "пинг пропущен"
    elif yc_res.get("error"):
        yc_dot = "🔴"
        yc_ping_str = f"ошибка ({yc_res['latency_ms']} мс): {_esc(yc_res['error'])}"
    else:
        code = int(yc_res["status_code"])
        yc_dot = "🟢" if 200 <= code < 300 else ("🟡" if code in (401, 403, 404, 429) else "🔴")
        yc_ping_str = f"HTTP {code} ({yc_res['latency_ms']} мс)"
    sec4 = (
        f"4️⃣ <b>🔗 YCLIENTS API и каталог</b> {yc_dot}\n"
        f"• <code>{_esc(yc_res.get('host', 'api.yclients.ru'))}</code>: <code>{yc_ping_str}</code> (API: {'вкл' if config.USE_YCLIENTS_API else 'выкл'})\n"
        f"• <code>booking_data.py</code>: <code>{_esc(files.get('bdata_mtime', '?'))}</code>\n"
        f"• Пражская: <code>{files.get('srv_pr', 0)}</code> усл. / <code>{files.get('mst_pr', 0)}</code> маст. | Ленингр.: <code>{files.get('srv_len', 0)}</code> усл. / <code>{files.get('mst_len', 0)}</code> маст."
    )

    # 5. Вебхук-сервер
    port = getattr(config, "YCLIENTS_WEBHOOK_PORT", 8080)
    if not wh_ping_res.get("enabled"):
        wh_dot = "🟡"
        wh_health_str = "отключён"
    elif not wh_ping_res.get("checked"):
        wh_dot = "🟢"
        wh_health_str = "пинг пропущен"
    elif wh_ping_res.get("ok"):
        wh_dot = "🟢"
        wh_health_str = f"OK HTTP {wh_ping_res['status_code']} ({wh_ping_res['latency_ms']} мс)"
    else:
        wh_dot = "🔴"
        wh_health_str = _esc(wh_ping_res.get("error") or f"HTTP {wh_ping_res.get('status_code')}")
    last_wh = f"{wh_tel['last_webhook_at']} ({wh_tel['last_webhook_event']})" if wh_tel.get("last_webhook_at") else "ещё не было"
    sec5 = (
        f"5️⃣ <b>🌐 Вебхук-сервер (:{port})</b> {wh_dot}\n"
        f"• /health: <code>{wh_health_str}</code> | Последний: <code>{_esc(last_wh)}</code>\n"
        f"• C/U/D: <code>{wh_tel['created']}/{wh_tel['updated']}/{wh_tel['deleted']}</code> | 403: <code>{wh_tel['auth_rejected']}</code> | err: <code>{wh_tel['errors']}</code> | locks/bg: <code>{wh_tel['dedup_locks']}/{wh_tel['background_tasks']}</code>"
    )

    # 6. Мессенджер MAX
    if not config.MAX_ENABLED:
        max_dot = "🟡"
    elif max_conn_res.get("live_checked"):
        max_dot = "🟢" if max_conn_res.get("ok") else "🔴"
    elif not max_conn_res.get("session_exists"):
        max_dot = "🔴"
    elif max_tel.get("session_expired_count", 0) > 0 and not max_tel.get("sent_ok"):
        max_dot = "🔴"
    else:
        max_dot = "🟢"

    sess_info = (
        f"есть ({_format_size(max_conn_res.get('session_size_bytes', 0))}, {max_conn_res.get('client_type', 'auto')})"
        if max_conn_res.get("session_exists")
        else "отсутствует"
    )
    if max_conn_res.get("live_checked"):
        if max_conn_res.get("ok"):
            live_str = f"пинг OK ({max_conn_res.get('latency_ms', 0)} мс)"
        else:
            live_str = f"пинг ERR: {max_conn_res.get('error') or 'fail'}"
    else:
        live_str = max_conn_res.get("note") or "живой пинг не запускался"
    last_max_str = max_tel.get("last_sent_at") or "ещё не было"
    queue_str = "занята" if max_tel.get("lock_busy") else "свободна"
    max_err_line = f"\n• Посл. ошибка MAX: <code>{_esc(max_tel['last_error'])}</code>" if max_tel.get("last_error") else ""
    sec6 = (
        f"6️⃣ <b>💬 Мессенджер MAX (pymax)</b> {max_dot}\n"
        f"• Режим: <code>{'вкл' if config.MAX_ENABLED else 'выкл'}</code> / <code>{_esc(config.MAX_MODE)}</code> / <code>{_esc(config.MAX_BACKEND)}</code> | Сессия: <code>{_esc(sess_info)}</code>\n"
        f"• Коннект: <code>{_esc(live_str)}</code> | Очередь: <code>{queue_str}</code>\n"
        f"• Отпр/НеНайд/Ош/Ист: <code>{max_tel['sent_ok']}/{max_tel['not_found']}/{max_tel['sent_failed']}/{max_tel['session_expired_count']}</code> | Посл.: <code>{_esc(last_max_str)}</code>{max_err_line}"
    )

    # 7. Планировщик
    sched_dot = "🟢" if sched_info["running"] else "🔴"
    c = sched_info["counts"]
    nearest_txt = (
        f"{sched_info['nearest_str']} ({sched_info['nearest_id']})"
        if sched_info.get("nearest_str")
        else "нет задач"
    )
    sec7 = (
        f"7️⃣ <b>⏰ Планировщик (APScheduler)</b> {sched_dot}\n"
        f"• Статус: <code>{'running' if sched_info['running'] else 'stopped'}</code> | TZ: <code>{_esc(sched_info['tz'])}</code> | Всего: <code>{sched_info['total']}</code>\n"
        f"• По типам: 24ч:<code>{c['24h']}</code> | 1ч:<code>{c['1h']}</code> | NPS:<code>{c['nps']}</code> | повтор:<code>{c['repeat']}</code> | дайджест:<code>{c['digest']}</code> | др:<code>{c['other']}</code>\n"
        f"• Ближайшая: <code>{_esc(nearest_txt)}</code>"
    )

    header = (
        f"🩺 <b>Статус систем · Sahar&amp;Vosk</b>\n"
        f"<i>Обновлено: {now.strftime('%d.%m.%Y %H:%M:%S')} МСК</i>\n"
        f"──────────────\n"
    )
    body = "\n\n".join([sec1, sec2, sec3, sec4, sec5, sec6, sec7])
    report = sanitize_secrets(header + body)
    return clip_html_report(report, 4096)


def get_recent_errors_text() -> str:
    """Форматирует HTML-отчёт последних записей WARNING/ERROR из кольцевого буфера."""
    handler = get_error_handler()
    records = handler.get_records()
    warn_cnt, err_cnt = handler.get_counts()

    if not records:
        return (
            "🚨 <b>Журнал ошибок и предупреждений · Sahar&amp;Vosk</b>\n"
            "──────────────\n"
            "Ошибок и предупреждений с момента запуска нет ✅"
        )

    header = (
        f"🚨 <b>Журнал ошибок · Sahar&amp;Vosk</b> (в буфере: <code>{len(records)}</code> | с запуска: <code>{warn_cnt}</code> warn / <code>{err_cnt}</code> err)\n"
        "──────────────"
    )
    suffix = "\n<i>…журнал сокращён до лимита Telegram</i>"
    acc = [header]
    truncated = False
    for item in records:
        icon = "🔴" if item["level"] in ("ERROR", "CRITICAL") else "🟡"
        block = (
            f"{icon} <code>{_esc(item['ts'])}</code> [<b>{_esc(item['level'])}</b>] <code>{_esc(item['logger_name'])}</code>\n"
            f"<code>{_esc(item['message'])}</code>"
        )
        candidate = sanitize_secrets("\n".join(acc + [block]))
        if db._utf16_len(candidate + suffix) > 4096:
            truncated = True
            break
        acc.append(block)

    full_html = sanitize_secrets("\n".join(acc))
    if truncated:
        full_html += suffix
    return clip_html_report(full_html, 4096)
