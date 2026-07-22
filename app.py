import csv
import difflib
import hmac
import ipaddress
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

from flask import (
    Flask, Response, abort, flash, g, jsonify, redirect, render_template, request,
    send_file, session, url_for,
)
from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from backup import (
    backup_path,
    backup_status,
    create_backup,
    list_backups,
    mirror_backup,
    verify_database,
)
from migrations import LATEST_SCHEMA_VERSION, run_migrations, schema_version


APP_NAME = "S50TTT Dnevnik skedov"
BASE_VERSION = "1.24.0"
RELEASE_CHANNEL = os.environ.get("RELEASE_CHANNEL", "stable").strip().lower()
if RELEASE_CHANNEL not in {"stable", "alpha"}:
    RELEASE_CHANNEL = "stable"
APP_VERSION = (
    f"{BASE_VERSION}-alpha" if RELEASE_CHANNEL == "alpha" else BASE_VERSION
)
DB_PATH = os.environ.get("DATABASE_PATH", "/app/data/skedi.db")
TIMEZONE = ZoneInfo(os.environ.get("TZ", "Europe/Ljubljana"))
TRUST_PROXY = os.environ.get("TRUST_PROXY", "0").strip() == "1"
TRUSTED_PROXY_NETWORKS = []
for proxy_network in os.environ.get("TRUSTED_PROXY_NETWORKS", "").split(","):
    proxy_network = proxy_network.strip()
    if proxy_network:
        TRUSTED_PROXY_NETWORKS.append(ipaddress.ip_network(proxy_network, strict=False))
TRUSTED_HOSTS = [
    host.strip()
    for host in os.environ.get(
        "TRUSTED_HOSTS",
        "skedi.s57zm.eu,localhost,127.0.0.1" if RELEASE_CHANNEL == "stable" else "",
    ).split(",")
    if host.strip()
]
SCHEDULE_MONTHLY = "monthly"
SCHEDULE_SATURDAY = "saturday"
SCHEDULE_TYPES = {SCHEDULE_MONTHLY, SCHEDULE_SATURDAY}
SATURDAY_SERIES_START = date(2019, 1, 5)
LOGIN_MAX_FAILURES = 5
LOGIN_LOCK_MINUTES = 15
LOGIN_IP_MAX_FAILURES = 20
LOGIN_ATTEMPT_RETENTION = 5000
PASSWORD_MIN_LENGTH = 15
PASSWORD_MAX_LENGTH = 128
CALLSIGN_PATTERN = re.compile(r"^[A-Z0-9](?:[A-Z0-9/-]{0,22}[A-Z0-9])?$")
DUMMY_PASSWORD_HASH = generate_password_hash(secrets.token_urlsafe(32))

APP_ROOT = Path(__file__).resolve().parent
app = Flask(__name__)
secret_key = os.environ.get("SECRET_KEY", "").strip()
if not secret_key:
    raise RuntimeError("SECRET_KEY mora biti nastavljen.")
if RELEASE_CHANNEL == "stable" and (
    len(secret_key) < 32 or secret_key == "replace-with-a-long-random-value"
):
    raise RuntimeError("SECRET_KEY mora biti naključen in dolg najmanj 32 znakov.")
app.secret_key = secret_key
app.config.update(
    SESSION_COOKIE_NAME=os.environ.get(
        "SESSION_COOKIE_NAME",
        "s50ttt_session" if RELEASE_CHANNEL == "stable" else "s50ttt_alpha_session",
    ).strip(),
    SESSION_COOKIE_PATH="/",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get(
        "SESSION_COOKIE_SECURE", "1" if RELEASE_CHANNEL == "stable" else "0"
    ).strip()
    == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(
        hours=max(1, int(os.environ.get("SESSION_HOURS", "12")))
    ),
    SESSION_REFRESH_EACH_REQUEST=True,
    MAX_CONTENT_LENGTH=1024 * 1024,
    TRUSTED_HOSTS=TRUSTED_HOSTS or None,
)
SESSION_ABSOLUTE_SECONDS = max(
    3600, int(os.environ.get("SESSION_ABSOLUTE_HOURS", "24")) * 3600
)


class TrustedProxyFix:
    """Uporabi posredovane glave samo, kadar zahteva pride iz dovoljene mreže."""

    def __init__(self, wrapped_app, trusted_networks):
        self.direct_app = wrapped_app
        self.proxy_app = ProxyFix(wrapped_app, x_for=1, x_proto=1)
        self.trusted_networks = trusted_networks

    def __call__(self, environ, start_response):
        try:
            remote = ipaddress.ip_address(environ.get("REMOTE_ADDR", ""))
        except ValueError:
            return self.direct_app(environ, start_response)
        if any(remote in network for network in self.trusted_networks):
            return self.proxy_app(environ, start_response)
        return self.direct_app(environ, start_response)


if TRUST_PROXY:
    if TRUSTED_PROXY_NETWORKS:
        app.wsgi_app = TrustedProxyFix(app.wsgi_app, TRUSTED_PROXY_NETWORKS)
    else:
        app.logger.warning(
            "TRUST_PROXY je vključen brez TRUSTED_PROXY_NETWORKS; "
            "posredovane glave bodo zaradi varnosti prezrte."
        )


def now_local():
    return datetime.now(TIMEZONE).replace(tzinfo=None)


def now_db():
    return now_local().strftime("%Y-%m-%d %H:%M:%S")


def normalize_callsign(value):
    return re.sub(r"\s+", "", str(value or "")).upper()


def valid_callsign(value):
    return bool(
        2 <= len(value) <= 24
        and CALLSIGN_PATTERN.fullmatch(value)
        and "//" not in value
        and "--" not in value
        and "/-" not in value
        and "-/" not in value
    )


def safe_local_redirect(target):
    """Dovoli samo nedvoumne absolutne poti znotraj portala."""
    if not target or any(ord(character) < 32 for character in target):
        return False
    decoded = target
    for _ in range(2):
        decoded = unquote(decoded)
    parsed = urlsplit(decoded)
    if (
        not decoded.startswith("/")
        or decoded.startswith("//")
        or "\\" in decoded
        or parsed.scheme
        or parsed.netloc
    ):
        return False
    return True


def metric_status(value, warning_at, danger_at):
    if value is None:
        return "unavailable"
    if value >= danger_at:
        return "danger"
    if value >= warning_at:
        return "warning"
    return "ok"


def collect_system_metrics():
    """Preberi omejen nabor gostiteljskih meritev brez izvajanja ukazov."""
    metrics = {
        "checked_at": now_db(),
        "temperature_c": None,
        "temperature_status": "unavailable",
        "disk_total_bytes": None,
        "disk_used_bytes": None,
        "disk_free_bytes": None,
        "disk_used_percent": None,
        "disk_status": "unavailable",
        "memory_total_bytes": None,
        "memory_used_bytes": None,
        "memory_available_bytes": None,
        "memory_used_percent": None,
        "memory_status": "unavailable",
        "load_1m": None,
        "cpu_count": os.cpu_count() or 1,
        "load_percent": None,
        "load_status": "unavailable",
        "uptime_seconds": None,
    }

    temperature_path = Path(
        os.environ.get(
            "CPU_TEMPERATURE_PATH", "/sys/class/thermal/thermal_zone0/temp"
        )
    )
    try:
        temperature = float(temperature_path.read_text(encoding="ascii").strip())
        if temperature > 1000:
            temperature /= 1000
        metrics["temperature_c"] = round(temperature, 1)
        metrics["temperature_status"] = metric_status(temperature, 70, 80)
    except (OSError, ValueError):
        pass

    try:
        usage = shutil.disk_usage(Path(DB_PATH).parent)
        disk_percent = usage.used * 100 / usage.total if usage.total else 0
        metrics.update(
            disk_total_bytes=usage.total,
            disk_used_bytes=usage.used,
            disk_free_bytes=usage.free,
            disk_used_percent=round(disk_percent, 1),
            disk_status=metric_status(disk_percent, 80, 90),
        )
    except OSError:
        pass

    try:
        memory_values = {}
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            key, separator, raw_value = line.partition(":")
            if separator:
                memory_values[key] = int(raw_value.strip().split()[0]) * 1024
        memory_total = memory_values["MemTotal"]
        memory_available = memory_values["MemAvailable"]
        memory_used = max(0, memory_total - memory_available)
        memory_percent = memory_used * 100 / memory_total if memory_total else 0
        metrics.update(
            memory_total_bytes=memory_total,
            memory_used_bytes=memory_used,
            memory_available_bytes=memory_available,
            memory_used_percent=round(memory_percent, 1),
            memory_status=metric_status(memory_percent, 80, 90),
        )
    except (OSError, ValueError, KeyError):
        pass

    try:
        load_1m = os.getloadavg()[0]
        load_percent = load_1m * 100 / metrics["cpu_count"]
        metrics.update(
            load_1m=round(load_1m, 2),
            load_percent=round(load_percent, 1),
            load_status=metric_status(load_percent, 75, 100),
        )
    except (AttributeError, OSError):
        pass

    try:
        uptime_value = Path("/proc/uptime").read_text(encoding="ascii").split()[0]
        metrics["uptime_seconds"] = int(float(uptime_value))
    except (OSError, ValueError, IndexError):
        pass
    return metrics


def first_weekday_of_month(year, month, weekday):
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7)


def next_month(year, month):
    return (year + 1, 1) if month == 12 else (year, month + 1)


def saturday_start_time(net_date):
    return "21:00" if 6 <= net_date.month <= 8 else "20:00"


def saturday_net_number(net_date):
    if net_date.weekday() != 5 or net_date < SATURDAY_SERIES_START:
        return None
    return ((net_date - SATURDAY_SERIES_START).days // 7) + 1


def scheduled_net_for_date(schedule_type, net_date):
    if schedule_type == SCHEDULE_MONTHLY:
        if net_date != first_weekday_of_month(net_date.year, net_date.month, 3):
            return None
        return {
            "schedule_type": SCHEDULE_MONTHLY,
            "label": "Mesečni sked Radiokluba Sevnica",
            "title": f"Mesečni sked S50TTT – {net_date.strftime('%d. %m. %Y')}",
            "date": net_date.isoformat(),
            "time": "19:00",
            "rule": "Prvi četrtek v mesecu ob 19.00",
            "repeater": None,
            "control_callsign": "S50TTT",
        }
    if schedule_type == SCHEDULE_SATURDAY:
        if net_date.weekday() != 5:
            return None
        sequence_number = saturday_net_number(net_date)
        if sequence_number is None:
            return None
        return {
            "schedule_type": SCHEDULE_SATURDAY,
            "label": f"Sobotni sked št. {sequence_number} prek repetitorja S55USX",
            "title": (
                f"Sobotni sked S50TTT št. {sequence_number} prek S55USX – "
                f"{net_date.strftime('%d. %m. %Y')}"
            ),
            "date": net_date.isoformat(),
            "time": saturday_start_time(net_date),
            "rule": "Vsako soboto; poleti ob 21.00, sicer ob 20.00",
            "repeater": "S55USX – Sv. Rok",
            "control_callsign": "S50TTT",
            "sequence_number": sequence_number,
        }
    return None


def schedule_start_datetime(scheduled):
    return datetime.strptime(
        f"{scheduled['date']} {scheduled['time']}", "%Y-%m-%d %H:%M"
    )


def next_scheduled_nets(reference=None, include_started_today=True):
    reference = reference or now_local()

    monthly_date = first_weekday_of_month(reference.year, reference.month, 3)
    monthly_info = scheduled_net_for_date(SCHEDULE_MONTHLY, monthly_date)
    if monthly_date < reference.date() or (
        not include_started_today and schedule_start_datetime(monthly_info) < reference
    ):
        year, month = next_month(reference.year, reference.month)
        monthly_date = first_weekday_of_month(year, month, 3)

    saturday_date = reference.date() + timedelta(days=(5 - reference.weekday()) % 7)
    saturday_info = scheduled_net_for_date(SCHEDULE_SATURDAY, saturday_date)
    if not include_started_today and schedule_start_datetime(saturday_info) < reference:
        saturday_date += timedelta(days=7)

    scheduled = [
        scheduled_net_for_date(SCHEDULE_MONTHLY, monthly_date),
        scheduled_net_for_date(SCHEDULE_SATURDAY, saturday_date),
    ]
    return sorted(scheduled, key=lambda item: (item["date"], item["time"]))


def next_countdown_net(reference=None):
    reference = reference or now_local()
    scheduled = next_scheduled_nets(reference, include_started_today=False)[0]
    scheduled["starts_at_iso"] = (
        schedule_start_datetime(scheduled).replace(tzinfo=TIMEZONE).isoformat()
    )
    return scheduled


def next_saturday_net(reference=None):
    return next(
        scheduled
        for scheduled in next_scheduled_nets(
            reference or now_local(), include_started_today=False
        )
        if scheduled["schedule_type"] == SCHEDULE_SATURDAY
    )


def get_schedule_exception(schedule_type, scheduled_date, db=None):
    db = db or get_db()
    return db.execute(
        """SELECT * FROM schedule_exceptions
           WHERE schedule_type=? AND scheduled_date=?""",
        (schedule_type, scheduled_date),
    ).fetchone()


def apply_schedule_exception(scheduled, db=None):
    """Return a scheduled slot with an optional cancellation/postponement applied."""
    result = dict(scheduled)
    result["original_date"] = scheduled["date"]
    result["original_time"] = scheduled["time"]
    result["exception_action"] = None
    result["exception_reason"] = None
    exception = get_schedule_exception(
        scheduled["schedule_type"], scheduled["date"], db
    )
    if not exception:
        return result

    result["exception_action"] = exception["action"]
    result["exception_reason"] = exception["reason"]
    if exception["action"] == "postponed":
        result["date"] = exception["new_date"]
        result["time"] = exception["new_time"]
    return result


def upcoming_effective_scheduled_nets(reference=None, horizon_days=100):
    """Return future regular nets, including postponed slots from recent weeks."""
    reference = reference or now_local()
    db = get_db()
    candidates = {}
    start_date = reference.date() - timedelta(days=31)
    end_date = reference.date() + timedelta(days=horizon_days)
    current_date = start_date
    while current_date <= end_date:
        schedule_types = []
        if current_date.weekday() == 5:
            schedule_types.append(SCHEDULE_SATURDAY)
        if current_date == first_weekday_of_month(
            current_date.year, current_date.month, 3
        ):
            schedule_types.append(SCHEDULE_MONTHLY)
        for schedule_type in schedule_types:
            scheduled = scheduled_net_for_date(schedule_type, current_date)
            effective = apply_schedule_exception(scheduled, db)
            if effective["exception_action"] == "canceled":
                continue
            if schedule_start_datetime(effective) > reference:
                candidates[(schedule_type, effective["original_date"])] = effective
        current_date += timedelta(days=1)

    # A long postponement must remain visible even after its original date is
    # outside the scan window.
    for exception in db.execute(
        """SELECT * FROM schedule_exceptions
           WHERE action='postponed' AND new_date IS NOT NULL AND new_time IS NOT NULL"""
    ).fetchall():
        try:
            original_date = date.fromisoformat(exception["scheduled_date"])
        except ValueError:
            continue
        scheduled = scheduled_net_for_date(exception["schedule_type"], original_date)
        if scheduled is None:
            continue
        effective = apply_schedule_exception(scheduled, db)
        if schedule_start_datetime(effective) > reference:
            candidates[(exception["schedule_type"], exception["scheduled_date"])] = effective
    return sorted(candidates.values(), key=schedule_start_datetime)


def dashboard_scheduled_nets(reference=None):
    reference = reference or now_local()
    db = get_db()
    scheduled = [
        apply_schedule_exception(item, db)
        for item in next_scheduled_nets(reference)
    ]
    known = {(item["schedule_type"], item["original_date"]) for item in scheduled}
    for item in upcoming_effective_scheduled_nets(reference):
        key = (item["schedule_type"], item["original_date"])
        if item["exception_action"] == "postponed" and key not in known:
            scheduled.append(item)
            known.add(key)
    return sorted(scheduled, key=schedule_start_datetime)


def next_effective_countdown_net(reference=None):
    scheduled = upcoming_effective_scheduled_nets(reference)[0]
    scheduled["starts_at_iso"] = (
        schedule_start_datetime(scheduled).replace(tzinfo=TIMEZONE).isoformat()
    )
    return scheduled


def next_effective_saturday_net(reference=None):
    return next(
        scheduled
        for scheduled in upcoming_effective_scheduled_nets(reference)
        if scheduled["schedule_type"] == SCHEDULE_SATURDAY
    )


def regular_net_open_date(net_date):
    if isinstance(net_date, str):
        net_date = date.fromisoformat(net_date)
    days_since_friday = (net_date.weekday() - 4) % 7
    return net_date - timedelta(days=days_since_friday)


def scheduled_net_participant_count(schedule_type, net_date):
    return get_db().execute(
        """SELECT COUNT(p.id) AS n
           FROM nets n LEFT JOIN participants p ON p.net_id=n.id
           WHERE n.schedule_type=? AND COALESCE(n.scheduled_date,n.net_date)=?""",
        (schedule_type, net_date),
    ).fetchone()["n"]


def recent_closed_saturday_summaries(limit=2):
    rows = get_db().execute(
        """SELECT n.net_date, COALESCE(n.scheduled_date,n.net_date) AS scheduled_date,
                  COUNT(p.id) AS participant_count
           FROM nets n LEFT JOIN participants p ON p.net_id=n.id
           WHERE n.schedule_type=? AND n.status='closed' AND n.net_date<=?
           GROUP BY n.id ORDER BY n.net_date DESC LIMIT ?""",
        (SCHEDULE_SATURDAY, now_local().date().isoformat(), limit),
    ).fetchall()
    summaries = []
    for row in rows:
        summary = dict(row)
        summary["sequence_number"] = saturday_net_number(
            date.fromisoformat(row["scheduled_date"])
        )
        summaries.append(summary)
    return summaries


def public_schedule_rows(reference=None, limit=10):
    reference = reference or now_local()
    db = get_db()
    items = {
        (item["schedule_type"], item["original_date"]): dict(item)
        for item in upcoming_effective_scheduled_nets(reference, horizon_days=120)
    }
    canceled = db.execute(
        """SELECT * FROM schedule_exceptions
           WHERE action='canceled' AND scheduled_date>=? AND scheduled_date<=?
           ORDER BY scheduled_date""",
        (
            reference.date().isoformat(),
            (reference.date() + timedelta(days=120)).isoformat(),
        ),
    ).fetchall()
    for exception in canceled:
        try:
            original_date = date.fromisoformat(exception["scheduled_date"])
        except ValueError:
            continue
        scheduled = scheduled_net_for_date(exception["schedule_type"], original_date)
        if scheduled:
            effective = apply_schedule_exception(scheduled, db)
            items[(exception["schedule_type"], exception["scheduled_date"])] = effective

    rows = []
    for item in sorted(items.values(), key=schedule_start_datetime)[:limit]:
        row = dict(item)
        existing = db.execute(
            """SELECT n.id, n.status, COUNT(p.id) AS participant_count
               FROM nets n LEFT JOIN participants p ON p.net_id=n.id
               WHERE n.schedule_type=?
                 AND COALESCE(n.scheduled_date,n.net_date)=?
               GROUP BY n.id""",
            (row["schedule_type"], row["original_date"]),
        ).fetchone()
        row["existing_id"] = existing["id"] if existing else None
        row["existing_status"] = existing["status"] if existing else None
        row["participant_count"] = existing["participant_count"] if existing else 0
        rows.append(row)
    return rows


def ics_escape(value):
    return (
        str(value or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def ics_fold(line, width=73):
    if len(line) <= width:
        return [line]
    parts = [line[:width]]
    remaining = line[width:]
    while remaining:
        parts.append(" " + remaining[: width - 1])
        remaining = remaining[width - 1 :]
    return parts


def get_db():
    if "db" not in g:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        g.db = sqlite3.connect(DB_PATH, timeout=30)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA journal_mode = WAL")
        g.db.execute("PRAGMA busy_timeout = 30000")
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            full_name TEXT NOT NULL,
            callsign TEXT NOT NULL COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin', 'leader')),
            active INTEGER NOT NULL DEFAULT 1,
            auth_version INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(id),
            username TEXT NOT NULL,
            ip_address TEXT NOT NULL,
            success INTEGER NOT NULL DEFAULT 0,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS nets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            net_date TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'closed')),
            leader_id INTEGER NOT NULL REFERENCES users(id),
            schedule_type TEXT,
            repeater TEXT,
            control_callsign TEXT,
            notes TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS participants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            net_id INTEGER NOT NULL REFERENCES nets(id) ON DELETE CASCADE,
            full_name TEXT NOT NULL,
            callsign TEXT NOT NULL COLLATE NOCASE,
            checkin_at TEXT NOT NULL,
            created_by INTEGER NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL,
            updated_by INTEGER REFERENCES users(id),
            updated_at TEXT,
            UNIQUE(net_id, callsign)
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(id),
            action TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id INTEGER,
            details TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS net_deletions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_net_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            net_date TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            leader_callsign TEXT,
            participant_count INTEGER NOT NULL,
            reason TEXT NOT NULL,
            snapshot TEXT NOT NULL,
            deleted_by INTEGER NOT NULL REFERENCES users(id),
            deleted_at TEXT NOT NULL,
            restored_by INTEGER REFERENCES users(id),
            restored_at TEXT,
            restored_net_id INTEGER
        );

        CREATE TABLE IF NOT EXISTS callsign_directory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            callsign TEXT NOT NULL UNIQUE COLLATE NOCASE,
            full_name TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            use_count INTEGER NOT NULL DEFAULT 0,
            last_used_at TEXT,
            notes TEXT,
            created_by INTEGER NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL,
            updated_by INTEGER REFERENCES users(id),
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS schedule_exceptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            schedule_type TEXT NOT NULL CHECK(schedule_type IN ('monthly', 'saturday')),
            scheduled_date TEXT NOT NULL,
            action TEXT NOT NULL CHECK(action IN ('canceled', 'postponed')),
            new_date TEXT,
            new_time TEXT,
            reason TEXT NOT NULL,
            created_by INTEGER NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL,
            updated_by INTEGER REFERENCES users(id),
            updated_at TEXT,
            UNIQUE(schedule_type, scheduled_date),
            CHECK(action='canceled' OR (new_date IS NOT NULL AND new_time IS NOT NULL))
        );

        CREATE TABLE IF NOT EXISTS csv_imports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            data_json TEXT,
            net_count INTEGER NOT NULL,
            participant_count INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending', 'imported', 'canceled')),
            created_by INTEGER NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL,
            imported_by INTEGER REFERENCES users(id),
            imported_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_nets_date ON nets(net_date DESC);
        CREATE INDEX IF NOT EXISTS idx_participants_net ON participants(net_id, checkin_at);
        CREATE INDEX IF NOT EXISTS idx_net_deletions_date
            ON net_deletions(deleted_at DESC);
        CREATE INDEX IF NOT EXISTS idx_callsign_directory_active
            ON callsign_directory(active, callsign);
        CREATE INDEX IF NOT EXISTS idx_schedule_exceptions_date
            ON schedule_exceptions(scheduled_date);
        CREATE INDEX IF NOT EXISTS idx_login_attempts_created
            ON login_attempts(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_login_attempts_ip
            ON login_attempts(ip_address, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_csv_imports_created
            ON csv_imports(created_at DESC);
        """
    )

    net_columns = {row["name"] for row in db.execute("PRAGMA table_info(nets)")}
    for column, declaration in {
        "schedule_type": "TEXT",
        "repeater": "TEXT",
        "control_callsign": "TEXT",
        "scheduled_date": "TEXT",
        "notes": "TEXT",
    }.items():
        if column not in net_columns:
            try:
                db.execute(f"ALTER TABLE nets ADD COLUMN {column} {declaration}")
            except sqlite3.OperationalError as error:
                if "duplicate column name" not in str(error).lower():
                    raise
    db.execute(
        """UPDATE nets SET scheduled_date=net_date
           WHERE schedule_type IS NOT NULL AND scheduled_date IS NULL"""
    )
    user_columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
    for column, declaration in {
        "failed_login_count": "INTEGER NOT NULL DEFAULT 0",
        "last_failed_login_at": "TEXT",
        "locked_until": "TEXT",
        "last_login_at": "TEXT",
        "last_login_ip": "TEXT",
    }.items():
        if column not in user_columns:
            try:
                db.execute(f"ALTER TABLE users ADD COLUMN {column} {declaration}")
            except sqlite3.OperationalError as error:
                if "duplicate column name" not in str(error).lower():
                    raise
    directory_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(callsign_directory)")
    }
    if "notes" not in directory_columns:
        try:
            db.execute("ALTER TABLE callsign_directory ADD COLUMN notes TEXT")
        except sqlite3.OperationalError as error:
            if "duplicate column name" not in str(error).lower():
                raise
    deletion_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(net_deletions)")
    }
    for column, declaration in {
        "restored_by": "INTEGER REFERENCES users(id)",
        "restored_at": "TEXT",
        "restored_net_id": "INTEGER",
    }.items():
        if column not in deletion_columns:
            try:
                db.execute(
                    f"ALTER TABLE net_deletions ADD COLUMN {column} {declaration}"
                )
            except sqlite3.OperationalError as error:
                if "duplicate column name" not in str(error).lower():
                    raise
    db.commit()
    run_migrations(db, now_db())
    db.execute("DROP INDEX IF EXISTS idx_nets_schedule_date")
    db.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_nets_schedule_date
           ON nets(schedule_type, COALESCE(scheduled_date, net_date))
           WHERE schedule_type IS NOT NULL"""
    )
    db.commit()

    db.execute("BEGIN IMMEDIATE")
    try:
        if db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0:
            username = os.environ.get("ADMIN_USERNAME", "S57ZM").strip()
            password = os.environ.get("ADMIN_PASSWORD", "").strip()
            if not password:
                raise RuntimeError(
                    "ADMIN_PASSWORD mora biti nastavljen ob prvem zagonu."
                )
            if not valid_password(password):
                raise RuntimeError(
                    "ADMIN_PASSWORD mora imeti od 15 do 128 znakov."
                )
            admin_callsign = normalize_callsign(
                os.environ.get("ADMIN_CALLSIGN", "S57ZM")
            )
            if not valid_callsign(admin_callsign):
                raise RuntimeError("ADMIN_CALLSIGN ni veljaven klicni znak.")
            db.execute(
                """INSERT INTO users
                   (username, full_name, callsign, password_hash, role, active,
                    must_change_password, created_at)
                   VALUES (?, ?, ?, ?, 'admin', 1, 1, ?)""",
                (
                    username,
                    os.environ.get("ADMIN_NAME", "Marko Zidar").strip()
                    or "Administrator",
                    admin_callsign,
                    generate_password_hash(password),
                    now_db(),
                ),
            )
        db.commit()
    except Exception:
        db.rollback()
        raise

    db.execute(
        """INSERT OR IGNORE INTO callsign_directory
           (callsign, full_name, active, use_count, last_used_at,
            created_by, created_at)
           SELECT UPPER(TRIM(p.callsign)), p.full_name, 1,
                  (SELECT COUNT(*) FROM participants counted
                   WHERE counted.callsign=p.callsign COLLATE NOCASE),
                  p.checkin_at, p.created_by, p.created_at
           FROM participants p
           WHERE TRIM(p.callsign)<>''
             AND p.id=(SELECT newest.id FROM participants newest
                       WHERE newest.callsign=p.callsign COLLATE NOCASE
                       ORDER BY newest.checkin_at DESC, newest.id DESC LIMIT 1)"""
    )
    db.commit()


def audit(action, entity_type, entity_id=None, details=None):
    db = get_db()
    db.execute(
        """INSERT INTO audit_log(user_id, action, entity_type, entity_id, details, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (session.get("user_id"), action, entity_type, entity_id, details, now_db()),
    )
    db.commit()


def learn_callsign(db, callsign, full_name, user_id, used_at):
    existing = db.execute(
        "SELECT id FROM callsign_directory WHERE callsign=?", (callsign,)
    ).fetchone()
    if existing:
        db.execute(
            """UPDATE callsign_directory
               SET use_count=use_count+1, last_used_at=? WHERE id=?""",
            (used_at, existing["id"]),
        )
        return existing["id"], False

    cursor = db.execute(
        """INSERT INTO callsign_directory
           (callsign, full_name, active, use_count, last_used_at,
            created_by, created_at)
           VALUES (?, ?, 1, 1, ?, ?, ?)""",
        (callsign[:24], full_name[:120], used_at, user_id, now_db()),
    )
    return cursor.lastrowid, True


def refresh_callsign_usage(db, callsign):
    usage = db.execute(
        """SELECT COUNT(*) AS use_count, MAX(checkin_at) AS last_used_at
           FROM participants WHERE callsign=? COLLATE NOCASE""",
        (callsign,),
    ).fetchone()
    db.execute(
        """UPDATE callsign_directory SET use_count=?, last_used_at=?
           WHERE callsign=? COLLATE NOCASE""",
        (usage["use_count"], usage["last_used_at"], callsign),
    )


def similar_callsigns(callsign, limit=3):
    callsign = callsign.strip().upper()
    if not callsign:
        return []
    known = [
        row["callsign"]
        for row in get_db().execute(
            "SELECT callsign FROM callsign_directory WHERE active=1"
        ).fetchall()
    ]
    if callsign in {value.upper() for value in known}:
        return []
    return difflib.get_close_matches(callsign, known, n=limit, cutoff=0.72)


@app.before_request
def load_user_and_csrf():
    g.user = None
    if session.get("user_id"):
        user = get_db().execute(
            "SELECT * FROM users WHERE id = ? AND active = 1", (session["user_id"],)
        ).fetchone()
        try:
            authenticated_at = int(session.get("authenticated_at", 0))
            session_auth_version = int(session.get("auth_version", -1))
        except (TypeError, ValueError):
            authenticated_at = 0
            session_auth_version = -1
        session_expired = (
            not authenticated_at
            or time.time() - authenticated_at > SESSION_ABSOLUTE_SECONDS
        )
        if (
            user is None
            or session_expired
            or session_auth_version != user["auth_version"]
        ):
            session.clear()
        else:
            g.user = user

    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)

    if request.method == "POST":
        if request.endpoint == "offline_sync" and g.user is None:
            return None
        submitted = request.form.get("csrf_token", "") or request.headers.get(
            "X-CSRF-Token", ""
        )
        expected = session.get("csrf_token", "")
        if not submitted or not expected or not hmac.compare_digest(submitted, expected):
            abort(400, "Neveljaven varnostni žeton. Osveži stran in poskusi ponovno.")


@app.before_request
def require_initial_password_change():
    if not g.user or not g.user["must_change_password"]:
        return None
    if request.endpoint in {"change_password", "logout", "static"}:
        return None
    flash("Pred nadaljevanjem zamenjaj začasno geslo.", "warning")
    return redirect(url_for("change_password"))


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), geolocation=(), microphone=()"
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; "
        "frame-ancestors 'none'",
    )
    if request.is_secure and RELEASE_CHANNEL == "stable":
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    if g.get("user") is not None or request.endpoint in {"login", "change_password"}:
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("Pragma", "no-cache")
    return response


@app.context_processor
def template_context():
    return {
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "release_channel": RELEASE_CHANNEL,
        "csrf_token": session.get("csrf_token", ""),
    }


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login"))
        if g.user["role"] != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def valid_password(password):
    return PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH


def client_ip_address():
    raw_value = (request.remote_addr or "").strip()
    try:
        return ipaddress.ip_address(raw_value).compressed
    except ValueError:
        return "unknown"


def record_login_attempt(user, username, ip_address_value, success, reason, created_at):
    db = get_db()
    db.execute(
        """INSERT INTO login_attempts
           (user_id, username, ip_address, success, reason, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            user["id"] if user else None,
            username[:120],
            ip_address_value[:64],
            1 if success else 0,
            reason[:40],
            created_at,
        ),
    )
    db.execute(
        """DELETE FROM login_attempts WHERE id NOT IN
           (SELECT id FROM login_attempts ORDER BY id DESC LIMIT ?)""",
        (LOGIN_ATTEMPT_RETENTION,),
    )
    db.commit()


@app.route("/health")
def health():
    try:
        db = get_db()
        db.execute("SELECT 1").fetchone()
        current_schema = schema_version(db)
    except sqlite3.Error:
        return {
            "status": "error",
            "database": "unavailable",
            "version": APP_VERSION,
            "channel": RELEASE_CHANNEL,
        }, 503
    return {
        "status": "ok",
        "database": "ok",
        "schema_version": current_schema,
        "schema_latest": LATEST_SCHEMA_VERSION,
        "version": APP_VERSION,
        "channel": RELEASE_CHANNEL,
    }


@app.get("/app.webmanifest")
def pwa_manifest():
    response = send_file(
        APP_ROOT / "static" / "app.webmanifest",
        mimetype="application/manifest+json",
        conditional=True,
    )
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/service-worker.js")
def service_worker():
    response = send_file(
        APP_ROOT / "static" / "service-worker.js",
        mimetype="application/javascript",
        conditional=True,
    )
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@app.get("/urnik")
def public_schedule():
    return render_template(
        "public_schedule.html", scheduled_nets=public_schedule_rows()
    )


@app.get("/urnik.ics")
def public_schedule_ics():
    host = request.host.split(":", 1)[0] or "skedi.s57zm.eu"
    generated = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//S50TTT//Dnevnik skedov//SL",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:S50TTT skedi",
        "X-WR-TIMEZONE:Europe/Ljubljana",
    ]
    for item in public_schedule_rows(limit=20):
        start_local = schedule_start_datetime(item).replace(tzinfo=TIMEZONE)
        start_utc = start_local.astimezone(ZoneInfo("UTC"))
        end_utc = start_utc + timedelta(hours=1)
        description_parts = [item["rule"], "Upravna postaja: S50TTT"]
        if item["repeater"]:
            description_parts.append(f"Repetitor: {item['repeater']}")
        if item["exception_reason"]:
            description_parts.append(f"Razlog: {item['exception_reason']}")
        event = [
            "BEGIN:VEVENT",
            f"UID:{item['schedule_type']}-{item['original_date']}@{host}",
            f"DTSTAMP:{generated}",
            f"DTSTART:{start_utc.strftime('%Y%m%dT%H%M%SZ')}",
            f"DTEND:{end_utc.strftime('%Y%m%dT%H%M%SZ')}",
            f"SUMMARY:{ics_escape(item['label'])}",
            f"DESCRIPTION:{ics_escape(' | '.join(description_parts))}",
        ]
        if item["exception_action"] == "canceled":
            event.extend(["STATUS:CANCELLED", "SEQUENCE:1"])
        elif item["exception_action"] == "postponed":
            event.append("SEQUENCE:1")
        event.append("END:VEVENT")
        lines.extend(event)
    lines.append("END:VCALENDAR")
    folded = [part for line in lines for part in ics_fold(line)]
    return Response(
        "\r\n".join(folded) + "\r\n",
        mimetype="text/calendar; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=s50ttt-skedi.ics"},
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        db = get_db()
        current = now_local()
        current_text = current.strftime("%Y-%m-%d %H:%M:%S")
        ip_address_value = client_ip_address()
        cutoff = (current - timedelta(minutes=LOGIN_LOCK_MINUTES)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        recent_ip_failures = db.execute(
            """SELECT COUNT(*) AS n FROM login_attempts
               WHERE ip_address=? AND success=0 AND created_at>=?""",
            (ip_address_value, cutoff),
        ).fetchone()["n"]
        user = db.execute(
            "SELECT * FROM users WHERE username = ? AND active = 1", (username,)
        ).fetchone()

        if recent_ip_failures >= LOGIN_IP_MAX_FAILURES:
            check_password_hash(DUMMY_PASSWORD_HASH, password)
            record_login_attempt(
                user, username, ip_address_value, False, "ip_limited", current_text
            )
            flash(
                "Prijava ni uspela. Preveri podatke ali poskusi znova čez 15 minut.",
                "danger",
            )
        else:
            if user and user["last_failed_login_at"] and user["last_failed_login_at"] < cutoff:
                db.execute(
                    """UPDATE users SET failed_login_count=0,
                       last_failed_login_at=NULL, locked_until=NULL WHERE id=?""",
                    (user["id"],),
                )
                db.commit()
                user = db.execute(
                    "SELECT * FROM users WHERE id=?", (user["id"],)
                ).fetchone()

            if user and user["locked_until"]:
                locked_until = datetime.fromisoformat(user["locked_until"])
                if locked_until <= current:
                    db.execute(
                        """UPDATE users SET failed_login_count=0,
                           last_failed_login_at=NULL, locked_until=NULL WHERE id=?""",
                        (user["id"],),
                    )
                    db.commit()
                    user = db.execute(
                        "SELECT * FROM users WHERE id=?", (user["id"],)
                    ).fetchone()

            account_locked = bool(
                user
                and user["locked_until"]
                and datetime.fromisoformat(user["locked_until"]) > current
            )
            password_ok = check_password_hash(
                user["password_hash"] if user else DUMMY_PASSWORD_HASH, password
            )
            if user and not account_locked and password_ok:
                previous_login = user["last_login_at"]
                db.execute(
                    """UPDATE users SET failed_login_count=0,
                       last_failed_login_at=NULL, locked_until=NULL,
                       last_login_at=?, last_login_ip=? WHERE id=?""",
                    (current_text, ip_address_value, user["id"]),
                )
                db.commit()
                record_login_attempt(
                    user, username, ip_address_value, True, "success", current_text
                )
                session.clear()
                session["user_id"] = user["id"]
                session["auth_version"] = user["auth_version"]
                session["authenticated_at"] = int(time.time())
                session["csrf_token"] = secrets.token_urlsafe(32)
                session.permanent = True
                if previous_login:
                    flash(
                        "Prejšnja uspešna prijava: "
                        + datetime.fromisoformat(previous_login).strftime(
                            "%d. %m. %Y ob %H:%M"
                        ),
                        "success",
                    )
                if user["must_change_password"]:
                    return redirect(url_for("change_password"))
                next_target = request.args.get("next", "")
                if safe_local_redirect(next_target):
                    return redirect(next_target)
                return redirect(url_for("dashboard"))

            reason = "account_locked" if account_locked else "invalid_credentials"
            if user and not account_locked:
                lock_until_text = (
                    current + timedelta(minutes=LOGIN_LOCK_MINUTES)
                ).strftime("%Y-%m-%d %H:%M:%S")
                db.execute(
                    """UPDATE users
                       SET failed_login_count=failed_login_count+1,
                           last_failed_login_at=?,
                           locked_until=CASE
                               WHEN failed_login_count+1>=? THEN ? ELSE NULL END
                       WHERE id=?""",
                    (current_text, LOGIN_MAX_FAILURES, lock_until_text, user["id"]),
                )
                updated = db.execute(
                    "SELECT failed_login_count FROM users WHERE id=?", (user["id"],)
                ).fetchone()
                if updated["failed_login_count"] >= LOGIN_MAX_FAILURES:
                    reason = "account_locked"
                    db.execute(
                        """INSERT INTO audit_log
                           (user_id, action, entity_type, entity_id, details, created_at)
                           VALUES (NULL, 'login_locked', 'user', ?, ?, ?)""",
                        (
                            user["id"],
                            json.dumps(
                                {"username": user["username"], "ip": ip_address_value},
                                ensure_ascii=False,
                            ),
                            current_text,
                        ),
                    )
                db.commit()
            record_login_attempt(
                user, username, ip_address_value, False, reason, current_text
            )
            flash(
                "Prijava ni uspela. Preveri podatke ali poskusi znova čez 15 minut.",
                "danger",
            )
    countdown_net = next_effective_countdown_net()
    displayed_saturday = next_effective_saturday_net()
    return render_template(
        "login_v2.html",
        countdown_net=countdown_net,
        next_saturday=displayed_saturday,
        saturday_participant_count=scheduled_net_participant_count(
            SCHEDULE_SATURDAY, displayed_saturday["original_date"]
        ),
        recent_saturdays=recent_closed_saturday_summaries(),
    )


@app.post("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    db = get_db()
    scheduled_nets = dashboard_scheduled_nets()
    current_date = now_local().date()
    for scheduled in scheduled_nets:
        existing = db.execute(
            """SELECT id, status FROM nets
               WHERE schedule_type=? AND COALESCE(scheduled_date,net_date)=?""",
            (scheduled["schedule_type"], scheduled["original_date"]),
        ).fetchone()
        scheduled["existing_id"] = existing["id"] if existing else None
        scheduled["existing_status"] = existing["status"] if existing else None
        open_date = regular_net_open_date(scheduled["date"])
        scheduled["open_date"] = open_date.isoformat()
        scheduled["can_open"] = current_date >= open_date

    open_nets = db.execute(
        """SELECT n.*, u.full_name AS leader_name, u.callsign AS leader_callsign,
                  COUNT(p.id) AS participant_count
           FROM nets n JOIN users u ON u.id=n.leader_id
           LEFT JOIN participants p ON p.net_id=n.id
           WHERE n.status='open' GROUP BY n.id ORDER BY n.started_at DESC"""
    ).fetchall()
    recent = db.execute(
        """SELECT n.*, u.full_name AS leader_name, u.callsign AS leader_callsign,
                  COUNT(p.id) AS participant_count
           FROM nets n JOIN users u ON u.id=n.leader_id
           LEFT JOIN participants p ON p.net_id=n.id
           WHERE n.status='closed' GROUP BY n.id ORDER BY n.started_at DESC LIMIT 15"""
    ).fetchall()
    return render_template(
        "dashboard_v2.html",
        open_nets=open_nets,
        recent=recent,
        scheduled_nets=scheduled_nets,
    )


@app.route(
    "/schedule/<schedule_type>/<scheduled_date>/exception", methods=["GET", "POST"]
)
@admin_required
def schedule_exception(schedule_type, scheduled_date):
    if schedule_type not in SCHEDULE_TYPES:
        abort(404)
    try:
        original_date = date.fromisoformat(scheduled_date)
    except ValueError:
        abort(404)
    scheduled = scheduled_net_for_date(schedule_type, original_date)
    if scheduled is None:
        abort(404)

    db = get_db()
    existing_net = db.execute(
        """SELECT id FROM nets WHERE schedule_type=?
           AND COALESCE(scheduled_date,net_date)=?""",
        (schedule_type, scheduled_date),
    ).fetchone()
    if existing_net:
        flash("Termina ni mogoče spreminjati, ker dnevnik že obstaja.", "warning")
        return redirect(url_for("net_detail", net_id=existing_net["id"]))

    exception = get_schedule_exception(schedule_type, scheduled_date, db)
    if request.method == "POST":
        action = request.form.get("action", "").strip()
        reason = request.form.get("reason", "").strip()
        new_date = request.form.get("new_date", "").strip() or None
        new_time = request.form.get("new_time", "").strip() or None
        errors = []
        if action not in {"canceled", "postponed"}:
            errors.append("Izberi odpoved ali prestavitev.")
        if len(reason) < 10:
            errors.append("Razlog mora imeti najmanj 10 znakov.")
        if action == "postponed":
            try:
                datetime.strptime(f"{new_date} {new_time}", "%Y-%m-%d %H:%M")
            except (TypeError, ValueError):
                errors.append("Vnesi veljaven novi datum in uro.")
        else:
            new_date = None
            new_time = None

        if errors:
            for error in errors:
                flash(error, "danger")
        elif exception:
            db.execute(
                """UPDATE schedule_exceptions
                   SET action=?, new_date=?, new_time=?, reason=?,
                       updated_by=?, updated_at=? WHERE id=?""",
                (
                    action,
                    new_date,
                    new_time,
                    reason[:1000],
                    g.user["id"],
                    now_db(),
                    exception["id"],
                ),
            )
            db.commit()
            audit(
                "update",
                "schedule_exception",
                exception["id"],
                json.dumps(
                    {
                        "schedule_type": schedule_type,
                        "scheduled_date": scheduled_date,
                        "action": action,
                        "new_date": new_date,
                        "new_time": new_time,
                        "reason": reason,
                    },
                    ensure_ascii=False,
                ),
            )
            flash("Sprememba termina je shranjena.", "success")
            return redirect(url_for("dashboard"))
        else:
            cursor = db.execute(
                """INSERT INTO schedule_exceptions
                   (schedule_type, scheduled_date, action, new_date, new_time,
                    reason, created_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    schedule_type,
                    scheduled_date,
                    action,
                    new_date,
                    new_time,
                    reason[:1000],
                    g.user["id"],
                    now_db(),
                ),
            )
            db.commit()
            audit(
                "create",
                "schedule_exception",
                cursor.lastrowid,
                json.dumps(
                    {
                        "schedule_type": schedule_type,
                        "scheduled_date": scheduled_date,
                        "action": action,
                        "new_date": new_date,
                        "new_time": new_time,
                        "reason": reason,
                    },
                    ensure_ascii=False,
                ),
            )
            flash("Sprememba termina je shranjena.", "success")
            return redirect(url_for("dashboard"))

    return render_template(
        "schedule_exception.html", scheduled=scheduled, exception=exception
    )


@app.post("/schedule/<schedule_type>/<scheduled_date>/exception/delete")
@admin_required
def delete_schedule_exception(schedule_type, scheduled_date):
    if schedule_type not in SCHEDULE_TYPES:
        abort(404)
    db = get_db()
    exception = get_schedule_exception(schedule_type, scheduled_date, db)
    if exception is None:
        abort(404)
    db.execute("DELETE FROM schedule_exceptions WHERE id=?", (exception["id"],))
    db.commit()
    audit(
        "delete",
        "schedule_exception",
        exception["id"],
        json.dumps(
            {
                "schedule_type": schedule_type,
                "scheduled_date": scheduled_date,
                "action": exception["action"],
                "reason": exception["reason"],
            },
            ensure_ascii=False,
        ),
    )
    flash("Redni termin je ponovno nastavljen po običajnem urniku.", "success")
    return redirect(url_for("dashboard"))


@app.post("/nets/new")
@login_required
def new_net():
    current = now_local()
    net_date = request.form.get("net_date", current.strftime("%Y-%m-%d"))
    started_time = request.form.get("started_time", current.strftime("%H:%M"))
    try:
        parsed_start = datetime.strptime(f"{net_date} {started_time}", "%Y-%m-%d %H:%M")
    except ValueError:
        flash("Datum ali ura nista veljavna.", "danger")
        return redirect(url_for("dashboard"))

    schedule_type = request.form.get("schedule_type", "").strip() or None
    if schedule_type and schedule_type not in SCHEDULE_TYPES:
        abort(400, "Neveljavna vrsta rednega skeda.")

    scheduled_info = None
    opened_early = False
    repeater = None
    control_callsign = None
    if schedule_type:
        scheduled_date = request.form.get("scheduled_date", net_date)
        try:
            original_date = date.fromisoformat(scheduled_date)
        except ValueError:
            flash("Prvotni datum rednega skeda ni veljaven.", "danger")
            return redirect(url_for("dashboard"))
        scheduled_info = scheduled_net_for_date(schedule_type, original_date)
        if scheduled_info is None:
            flash("Datum ali ura ne ustrezata pravilom izbranega rednega skeda.", "danger")
            return redirect(url_for("dashboard"))
        effective_info = apply_schedule_exception(scheduled_info)
        if effective_info["exception_action"] == "canceled":
            flash("Ta redni sked je odpovedan in dnevnika ni mogoče odpreti.", "warning")
            return redirect(url_for("dashboard"))
        if net_date != effective_info["date"] or started_time != effective_info["time"]:
            flash("Datum ali ura ne ustrezata veljavnemu terminu rednega skeda.", "danger")
            return redirect(url_for("dashboard"))
        title = scheduled_info["title"]
        if effective_info["exception_action"] == "postponed":
            title += f" (prestavljen na {parsed_start.strftime('%d. %m. %Y')})"
        repeater = scheduled_info["repeater"]
        control_callsign = scheduled_info["control_callsign"]
        open_date = regular_net_open_date(parsed_start.date())
        if current.date() < open_date:
            if request.form.get("early_unlock") != "1":
                flash(
                    f"Ta dnevnik bo mogoče odpreti v petek, "
                    f"{open_date.strftime('%d. %m. %Y')}.",
                    "warning",
                )
                return redirect(url_for("dashboard"))
            opened_early = True
    else:
        title = request.form.get("title", "").strip()
        if not title:
            title = f"Sked {parsed_start.strftime('%d. %m. %Y')}"

    db = get_db()
    if schedule_type:
        existing = db.execute(
            """SELECT id FROM nets WHERE schedule_type=?
               AND COALESCE(scheduled_date,net_date)=?""",
            (schedule_type, scheduled_date),
        ).fetchone()
        if existing:
            flash("Dnevnik za ta redni sked že obstaja.", "warning")
            return redirect(url_for("net_detail", net_id=existing["id"]))

    try:
        cur = db.execute(
            """INSERT INTO nets
               (title, net_date, scheduled_date, started_at, status, leader_id, schedule_type,
                repeater, control_callsign, created_at)
               VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)""",
            (
                title[:120],
                net_date,
                scheduled_date if schedule_type else None,
                f"{net_date} {started_time}:00",
                g.user["id"],
                schedule_type,
                repeater,
                control_callsign,
                now_db(),
            ),
        )
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        if schedule_type:
            existing = db.execute(
                """SELECT id FROM nets WHERE schedule_type=?
                   AND COALESCE(scheduled_date,net_date)=?""",
                (schedule_type, scheduled_date),
            ).fetchone()
            if existing:
                flash("Dnevnik za ta redni sked že obstaja.", "warning")
                return redirect(url_for("net_detail", net_id=existing["id"]))
        raise
    audit_details = title
    if opened_early:
        audit_details += " · predčasno odprtje s petimi pritiski"
    audit("create", "net", cur.lastrowid, audit_details)
    flash("Novi sked je odprt.", "success")
    return redirect(url_for("net_detail", net_id=cur.lastrowid))


def fetch_net(net_id):
    row = get_db().execute(
        """SELECT n.*, u.full_name AS leader_name, u.callsign AS leader_callsign
           FROM nets n JOIN users u ON u.id=n.leader_id WHERE n.id=?""",
        (net_id,),
    ).fetchone()
    if row is None:
        abort(404)
    return row


def can_manage_participants(net):
    if g.user["role"] == "admin":
        return True
    return net["status"] == "open" and net["leader_id"] == g.user["id"]


def offline_net_snapshot(net_id, db=None):
    db = db or get_db()
    net = db.execute(
        """SELECT n.*, u.full_name AS leader_name, u.callsign AS leader_callsign
           FROM nets n JOIN users u ON u.id=n.leader_id WHERE n.id=?""",
        (net_id,),
    ).fetchone()
    if net is None:
        return None
    participants = db.execute(
        """SELECT p.*, u.full_name AS entered_by_name
           FROM participants p JOIN users u ON u.id=p.created_by
           WHERE p.net_id=? ORDER BY p.checkin_at, p.id""",
        (net_id,),
    ).fetchall()
    directory = db.execute(
        """SELECT callsign, full_name FROM callsign_directory
           WHERE active=1 ORDER BY callsign"""
    ).fetchall()
    can_sync = bool(
        g.user
        and net["status"] == "open"
        and (g.user["role"] == "admin" or net["leader_id"] == g.user["id"])
    )
    return {
        "schema_version": 1,
        "saved_at": now_local().isoformat(timespec="seconds"),
        "net": {
            "id": net["id"],
            "title": net["title"],
            "net_date": net["net_date"],
            "started_at": net["started_at"],
            "status": net["status"],
            "leader_name": net["leader_name"],
            "leader_callsign": net["leader_callsign"],
            "repeater": net["repeater"],
            "control_callsign": net["control_callsign"],
            "notes": net["notes"] or "",
            "can_sync": can_sync,
        },
        "participants": [
            {
                "id": participant["id"],
                "full_name": participant["full_name"],
                "callsign": participant["callsign"],
                "checkin_time": participant["checkin_at"][11:16],
                "entered_by_name": participant["entered_by_name"],
            }
            for participant in participants
        ],
        "directory": [
            {"callsign": entry["callsign"], "full_name": entry["full_name"]}
            for entry in directory
        ],
    }


@app.route("/nets/<int:net_id>")
@login_required
def net_detail(net_id):
    net = fetch_net(net_id)
    db = get_db()
    participants = db.execute(
        """SELECT p.*, u.full_name AS entered_by_name
           FROM participants p JOIN users u ON u.id=p.created_by
           WHERE p.net_id=? ORDER BY p.checkin_at, p.id""",
        (net_id,),
    ).fetchall()
    directory_entries = db.execute(
        """SELECT callsign, full_name FROM callsign_directory
           WHERE active=1 ORDER BY callsign"""
    ).fetchall()
    can_delete_net = (
        net["status"] == "open"
        and not participants
        and (g.user["role"] == "admin" or net["leader_id"] == g.user["id"])
    )
    can_edit_notes = g.user["role"] == "admin" or (
        net["status"] == "open" and net["leader_id"] == g.user["id"]
    )
    return render_template(
        "net_v2.html",
        net=net,
        participants=participants,
        now=now_local(),
        can_delete_net=can_delete_net,
        can_edit_notes=can_edit_notes,
        can_manage_participants=can_manage_participants(net),
        directory_entries=directory_entries,
        recent_participants=list(reversed(participants[-5:])),
        offline_snapshot=(
            offline_net_snapshot(net_id)
            if net["status"] == "open" and can_manage_participants(net)
            else None
        ),
    )


OFFLINE_OPERATION_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,80}$")
OFFLINE_ACTIONS = {"add_participant", "delete_participant", "update_notes"}


def offline_operation_result(operation_id, status, message, **values):
    result = {
        "operation_id": operation_id,
        "status": status,
        "message": message,
    }
    result.update(values)
    return result


def insert_offline_audit(db, action, entity_type, entity_id, details):
    db.execute(
        """INSERT INTO audit_log
           (user_id, action, entity_type, entity_id, details, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (g.user["id"], action, entity_type, entity_id, details, now_db()),
    )


def apply_offline_operation(db, net, operation_id, action, data):
    if net["status"] != "open":
        return offline_operation_result(
            operation_id,
            "conflict",
            "Sked je bil med delom brez povezave že zaključen.",
        )

    if action == "add_participant":
        full_name = str(data.get("full_name", "")).strip()
        callsign = normalize_callsign(str(data.get("callsign", "")))
        checkin_time = str(data.get("checkin_time", "")).strip()
        if not full_name or not valid_callsign(callsign):
            return offline_operation_result(
                operation_id, "invalid", "Ime ali klicni znak ni veljaven."
            )
        try:
            datetime.strptime(checkin_time, "%H:%M")
        except ValueError:
            return offline_operation_result(
                operation_id, "invalid", "Ura prijave ni veljavna."
            )
        existing = db.execute(
            "SELECT id FROM participants WHERE net_id=? AND callsign=?",
            (net["id"], callsign),
        ).fetchone()
        if existing:
            return offline_operation_result(
                operation_id,
                "conflict",
                f"Klicni znak {callsign} je že vpisan v tem skedu.",
            )
        checkin_at = f"{net['net_date']} {checkin_time}:00"
        cursor = db.execute(
            """INSERT INTO participants
               (net_id, full_name, callsign, checkin_at, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                net["id"],
                full_name[:120],
                callsign[:24],
                checkin_at,
                g.user["id"],
                now_db(),
            ),
        )
        directory_id, directory_created = learn_callsign(
            db, callsign, full_name, g.user["id"], checkin_at
        )
        insert_offline_audit(
            db,
            "offline_create",
            "participant",
            cursor.lastrowid,
            f"{callsign} – {full_name[:120]} · operacija {operation_id}",
        )
        if directory_created:
            insert_offline_audit(
                db,
                "learn",
                "callsign",
                directory_id,
                f"{callsign} – {full_name[:120]} · offline",
            )
        return offline_operation_result(
            operation_id,
            "applied",
            f"Dodan {callsign}.",
            participant_id=cursor.lastrowid,
        )

    if action == "delete_participant":
        try:
            participant_id = int(data.get("participant_id"))
        except (TypeError, ValueError):
            return offline_operation_result(
                operation_id, "invalid", "Udeleženec za izbris ni veljaven."
            )
        participant = db.execute(
            "SELECT * FROM participants WHERE id=? AND net_id=?",
            (participant_id, net["id"]),
        ).fetchone()
        if participant is None:
            return offline_operation_result(
                operation_id,
                "applied",
                "Vnos je bil že odstranjen.",
            )
        db.execute("DELETE FROM participants WHERE id=?", (participant_id,))
        refresh_callsign_usage(db, participant["callsign"])
        insert_offline_audit(
            db,
            "offline_delete",
            "participant",
            participant_id,
            f"{participant['callsign']} – {participant['full_name']} · operacija {operation_id}",
        )
        return offline_operation_result(
            operation_id,
            "applied",
            f"Odstranjen {participant['callsign']}.",
        )

    notes = str(data.get("notes", "")).strip()[:5000]
    base_notes = str(data.get("base_notes", ""))[:5000]
    current_notes_row = db.execute(
        "SELECT notes FROM nets WHERE id=?", (net["id"],)
    ).fetchone()
    current_notes = current_notes_row["notes"] or ""
    if current_notes not in {base_notes, notes}:
        return offline_operation_result(
            operation_id,
            "conflict",
            "Zapisnik je bil na drugi napravi že spremenjen; strežniška različica je ohranjena.",
        )
    db.execute("UPDATE nets SET notes=? WHERE id=?", (notes or None, net["id"]))
    insert_offline_audit(
        db,
        "offline_update_notes",
        "net",
        net["id"],
        f"{net['title']} · operacija {operation_id}",
    )
    return offline_operation_result(
        operation_id, "applied", "Zapisnik je sinhroniziran."
    )


@app.post("/api/offline/sync")
def offline_sync():
    if g.user is None:
        return jsonify(error="authentication_required"), 401
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(error="invalid_payload"), 400
    try:
        net_id = int(payload.get("net_id"))
    except (TypeError, ValueError):
        return jsonify(error="invalid_net"), 400
    operations = payload.get("operations")
    if not isinstance(operations, list) or not 1 <= len(operations) <= 100:
        return jsonify(error="invalid_operations"), 400

    db = get_db()
    net = db.execute("SELECT * FROM nets WHERE id=?", (net_id,)).fetchone()
    if net is None:
        return jsonify(error="net_not_found"), 404
    if g.user["role"] != "admin" and net["leader_id"] != g.user["id"]:
        return jsonify(error="forbidden"), 403

    results = []
    try:
        db.execute("BEGIN IMMEDIATE")
        for operation in operations:
            if not isinstance(operation, dict):
                results.append(
                    offline_operation_result("", "invalid", "Neveljavna operacija.")
                )
                continue
            operation_id = str(operation.get("operation_id", ""))
            action = str(operation.get("action", ""))
            data = operation.get("data")
            if not OFFLINE_OPERATION_PATTERN.fullmatch(operation_id):
                results.append(
                    offline_operation_result(
                        operation_id, "invalid", "ID operacije ni veljaven."
                    )
                )
                continue
            previous = db.execute(
                """SELECT user_id, net_id, result_json FROM offline_operations
                   WHERE operation_id=?""",
                (operation_id,),
            ).fetchone()
            if previous:
                if previous["user_id"] == g.user["id"] and previous["net_id"] == net_id:
                    replayed = json.loads(previous["result_json"])
                    replayed["replayed"] = True
                    results.append(replayed)
                else:
                    results.append(
                        offline_operation_result(
                            operation_id, "invalid", "ID operacije je že uporabljen."
                        )
                    )
                continue
            if action not in OFFLINE_ACTIONS or not isinstance(data, dict):
                result = offline_operation_result(
                    operation_id, "invalid", "Vrsta operacije ni veljavna."
                )
            else:
                result = apply_offline_operation(
                    db, net, operation_id, action, data
                )
            db.execute(
                """INSERT INTO offline_operations
                   (operation_id, user_id, net_id, action, result_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    operation_id,
                    g.user["id"],
                    net_id,
                    action[:40],
                    json.dumps(result, ensure_ascii=False),
                    now_db(),
                ),
            )
            results.append(result)
        db.execute(
            """DELETE FROM offline_operations WHERE id NOT IN
               (SELECT id FROM offline_operations ORDER BY id DESC LIMIT 5000)"""
        )
        db.commit()
    except sqlite3.Error:
        db.rollback()
        return jsonify(error="database_unavailable"), 503

    return jsonify(
        results=results,
        snapshot=offline_net_snapshot(net_id, db),
        version=APP_VERSION,
    )


@app.post("/nets/<int:net_id>/notes")
@login_required
def update_net_notes(net_id):
    net = fetch_net(net_id)
    if g.user["role"] != "admin" and (
        net["status"] != "open" or net["leader_id"] != g.user["id"]
    ):
        abort(403)
    notes = request.form.get("notes", "").strip()[:5000]
    db = get_db()
    db.execute("UPDATE nets SET notes=? WHERE id=?", (notes or None, net_id))
    db.commit()
    audit("update_notes", "net", net_id, net["title"])
    flash("Zapisnik skeda je shranjen.", "success")
    return redirect(url_for("net_detail", net_id=net_id))


@app.route("/nets/<int:net_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_net(net_id):
    net = fetch_net(net_id)
    if net["status"] != "closed":
        flash("Ta obrazec je namenjen popravkom zaključenih skedov.", "warning")
        return redirect(url_for("net_detail", net_id=net_id))

    db = get_db()
    users = db.execute(
        "SELECT * FROM users ORDER BY active DESC, full_name"
    ).fetchall()
    participants = db.execute(
        """SELECT p.*, u.full_name AS entered_by_name
           FROM participants p JOIN users u ON u.id=p.created_by
           WHERE p.net_id=? ORDER BY p.checkin_at, p.id""",
        (net_id,),
    ).fetchall()
    directory_entries = db.execute(
        """SELECT callsign, full_name FROM callsign_directory
           WHERE active=1 ORDER BY callsign"""
    ).fetchall()
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        net_date = request.form.get("net_date", "").strip()
        started_time = request.form.get("started_time", "").strip()
        ended_time = request.form.get("ended_time", "").strip()
        notes = request.form.get("notes", "").strip()[:5000]
        try:
            leader_id = int(request.form.get("leader_id", ""))
            started_at = datetime.strptime(
                f"{net_date} {started_time}", "%Y-%m-%d %H:%M"
            )
            ended_at = datetime.strptime(
                f"{net_date} {ended_time}", "%Y-%m-%d %H:%M"
            )
        except (TypeError, ValueError):
            flash("Preveri datum, začetno in končno uro ter operaterja.", "danger")
            return redirect(request.url)

        leader = db.execute("SELECT id FROM users WHERE id=?", (leader_id,)).fetchone()
        if not title or leader is None:
            flash("Naslov in operater sta obvezna.", "danger")
            return redirect(request.url)
        if ended_at < started_at:
            ended_at += timedelta(days=1)

        affected_callsigns = []
        if net_date != net["net_date"]:
            affected_callsigns = [
                row["callsign"]
                for row in db.execute(
                    "SELECT DISTINCT callsign FROM participants WHERE net_id=?",
                    (net_id,),
                ).fetchall()
            ]

        before = {
            "title": net["title"],
            "net_date": net["net_date"],
            "started_at": net["started_at"],
            "ended_at": net["ended_at"],
            "leader_id": net["leader_id"],
            "notes": net["notes"],
        }
        after = {
            "title": title[:180],
            "net_date": net_date,
            "started_at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
            "ended_at": ended_at.strftime("%Y-%m-%d %H:%M:%S"),
            "leader_id": leader_id,
            "notes": notes or None,
        }
        try:
            db.execute(
                """UPDATE nets
                   SET title=?, net_date=?, started_at=?, ended_at=?, leader_id=?, notes=?
                   WHERE id=?""",
                (
                    after["title"],
                    after["net_date"],
                    after["started_at"],
                    after["ended_at"],
                    after["leader_id"],
                    after["notes"],
                    net_id,
                ),
            )
            if net_date != net["net_date"]:
                for participant in db.execute(
                    "SELECT id, checkin_at FROM participants WHERE net_id=?", (net_id,)
                ).fetchall():
                    checkin_time = participant["checkin_at"][11:19]
                    db.execute(
                        """UPDATE participants
                           SET checkin_at=?, updated_by=?, updated_at=? WHERE id=?""",
                        (
                            f"{net_date} {checkin_time}",
                            g.user["id"],
                            now_db(),
                            participant["id"],
                        ),
                    )
                for callsign in affected_callsigns:
                    refresh_callsign_usage(db, callsign)
            audit(
                "update",
                "net",
                net_id,
                json.dumps({"before": before, "after": after}, ensure_ascii=False),
            )
        except sqlite3.IntegrityError:
            db.rollback()
            flash("Za ta redni termin dnevnik že obstaja.", "warning")
            return redirect(request.url)

        flash("Podatki zaključenega skeda so popravljeni.", "success")
        return redirect(url_for("net_detail", net_id=net_id))

    return render_template(
        "net_edit_v2.html",
        net=net,
        users=users,
        participants=participants,
        directory_entries=directory_entries,
        now=now_local(),
    )


def participant_return_url(net_id):
    if request.form.get("return_to") == "net_edit" and g.user["role"] == "admin":
        return url_for("edit_net", net_id=net_id)
    return url_for("net_detail", net_id=net_id)


@app.post("/nets/<int:net_id>/participants")
@login_required
def add_participant(net_id):
    net = fetch_net(net_id)
    if not can_manage_participants(net):
        abort(403)
    full_name = request.form.get("full_name", "").strip()
    callsign = normalize_callsign(request.form.get("callsign", ""))
    checkin_time = request.form.get("checkin_time", now_local().strftime("%H:%M"))
    if not full_name or not callsign:
        flash("Vpiši ime in klicni znak.", "danger")
        return redirect(participant_return_url(net_id))
    if not valid_callsign(callsign):
        flash(
            "Klicni znak lahko vsebuje samo črke, številke, / in - (2–24 znakov).",
            "danger",
        )
        return redirect(participant_return_url(net_id))
    try:
        datetime.strptime(checkin_time, "%H:%M")
    except ValueError:
        flash("Ura prijave ni veljavna.", "danger")
        return redirect(participant_return_url(net_id))
    db = get_db()
    possible_typos = similar_callsigns(callsign)
    checkin_at = f"{net['net_date']} {checkin_time}:00"
    try:
        cur = db.execute(
            """INSERT INTO participants
               (net_id, full_name, callsign, checkin_at, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                net_id,
                full_name[:120],
                callsign[:24],
                checkin_at,
                g.user["id"],
                now_db(),
            ),
        )
        directory_id, directory_created = learn_callsign(
            db, callsign, full_name, g.user["id"], checkin_at
        )
        db.commit()
    except sqlite3.IntegrityError:
        flash(f"Klicni znak {callsign} je v tem skedu že vpisan.", "warning")
        return redirect(participant_return_url(net_id))
    audit("create", "participant", cur.lastrowid, f"{callsign} – {full_name}")
    if directory_created:
        audit("learn", "callsign", directory_id, f"{callsign} – {full_name}")
    flash(f"Dodan: {callsign} – {full_name}", "success")
    if possible_typos:
        flash(
            "Preveri klicni znak: podoben je " + ", ".join(possible_typos) + ".",
            "warning",
        )
    return redirect(participant_return_url(net_id))


def fetch_participant(participant_id):
    row = get_db().execute("SELECT * FROM participants WHERE id=?", (participant_id,)).fetchone()
    if row is None:
        abort(404)
    return row


@app.route("/participants/<int:participant_id>/edit", methods=["GET", "POST"])
@login_required
def edit_participant(participant_id):
    participant = fetch_participant(participant_id)
    net = fetch_net(participant["net_id"])
    return_to = request.form.get("return_to") or request.args.get("return_to", "")
    if not can_manage_participants(net):
        abort(403)
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        callsign = normalize_callsign(request.form.get("callsign", ""))
        checkin_time = request.form.get("checkin_time", "")
        if not full_name or not callsign:
            flash("Vpiši ime in klicni znak.", "danger")
            return redirect(request.url)
        if not valid_callsign(callsign):
            flash(
                "Klicni znak lahko vsebuje samo črke, številke, / in - (2–24 znakov).",
                "danger",
            )
            return redirect(request.url)
        try:
            datetime.strptime(checkin_time, "%H:%M")
            db = get_db()
            checkin_at = f"{net['net_date']} {checkin_time}:00"
            db.execute(
                """UPDATE participants SET full_name=?, callsign=?, checkin_at=?,
                   updated_by=?, updated_at=? WHERE id=?""",
                (full_name[:120], callsign[:24], checkin_at, g.user["id"], now_db(), participant_id),
            )
            directory_id, directory_created = learn_callsign(
                db, callsign, full_name, g.user["id"], checkin_at
            )
            refresh_callsign_usage(db, participant["callsign"])
            refresh_callsign_usage(db, callsign)
            db.commit()
        except ValueError:
            flash("Ura prijave ni veljavna.", "danger")
            return redirect(request.url)
        except sqlite3.IntegrityError:
            flash(f"Klicni znak {callsign} je v tem skedu že vpisan.", "warning")
            return redirect(request.url)
        audit("update", "participant", participant_id, f"{callsign} – {full_name}")
        if directory_created:
            audit("learn", "callsign", directory_id, f"{callsign} – {full_name}")
        flash("Vnos je popravljen.", "success")
        if return_to == "net_edit" and g.user["role"] == "admin":
            return redirect(url_for("edit_net", net_id=net["id"]))
        return redirect(url_for("net_detail", net_id=net["id"]))
    return render_template(
        "participant_edit.html",
        participant=participant,
        net=net,
        return_to=return_to,
    )


@app.post("/participants/<int:participant_id>/delete")
@login_required
def delete_participant(participant_id):
    participant = fetch_participant(participant_id)
    net = fetch_net(participant["net_id"])
    if not can_manage_participants(net):
        abort(403)
    db = get_db()
    db.execute("DELETE FROM participants WHERE id=?", (participant_id,))
    refresh_callsign_usage(db, participant["callsign"])
    db.commit()
    audit("delete", "participant", participant_id, f"{participant['callsign']} – {participant['full_name']}")
    flash("Vnos je izbrisan.", "success")
    return redirect(participant_return_url(net["id"]))


@app.post("/nets/<int:net_id>/participants/undo-last")
@login_required
def undo_last_participant(net_id):
    net = fetch_net(net_id)
    if not can_manage_participants(net) or net["status"] != "open":
        abort(403)
    db = get_db()
    participant = db.execute(
        """SELECT * FROM participants WHERE net_id=?
           ORDER BY id DESC LIMIT 1""",
        (net_id,),
    ).fetchone()
    if participant is None:
        flash("V dnevniku še ni vnosa za razveljavitev.", "warning")
        return redirect(url_for("net_detail", net_id=net_id))
    db.execute("DELETE FROM participants WHERE id=?", (participant["id"],))
    refresh_callsign_usage(db, participant["callsign"])
    db.commit()
    audit(
        "undo",
        "participant",
        participant["id"],
        f"{participant['callsign']} – {participant['full_name']}",
    )
    flash(
        f"Razveljavljen zadnji vnos: {participant['callsign']} – "
        f"{participant['full_name']}",
        "success",
    )
    return redirect(url_for("net_detail", net_id=net_id))


@app.post("/nets/<int:net_id>/delete")
@login_required
def delete_net(net_id):
    net = fetch_net(net_id)
    if g.user["role"] != "admin" and net["leader_id"] != g.user["id"]:
        abort(403)
    if net["status"] != "open":
        flash("Zaključenega skeda ni mogoče izbrisati.", "danger")
        return redirect(url_for("net_detail", net_id=net_id))

    db = get_db()
    participant_count = db.execute(
        "SELECT COUNT(*) AS n FROM participants WHERE net_id=?", (net_id,)
    ).fetchone()["n"]
    if participant_count:
        flash("Skeda z vpisanimi udeleženci ni mogoče izbrisati.", "warning")
        return redirect(url_for("net_detail", net_id=net_id))

    db.execute("DELETE FROM nets WHERE id=?", (net_id,))
    audit("delete", "net", net_id, f"{net['title']} ({net['net_date']})")
    flash("Prazen odprt sked je izbrisan.", "success")
    return redirect(url_for("dashboard"))


@app.route("/nets/<int:net_id>/delete-closed", methods=["GET", "POST"])
@admin_required
def delete_closed_net(net_id):
    net = fetch_net(net_id)
    if net["status"] != "closed":
        flash("Na ta način je mogoče izbrisati samo zaključen sked.", "warning")
        return redirect(url_for("net_detail", net_id=net_id))

    db = get_db()
    participants = db.execute(
        """SELECT full_name, callsign, checkin_at
           FROM participants WHERE net_id=? ORDER BY checkin_at, id""",
        (net_id,),
    ).fetchall()
    participant_count = len(participants)
    if request.method == "POST":
        reason = request.form.get("reason", "").strip()
        if len(reason) < 10:
            flash("Vpiši razlog brisanja z najmanj 10 znaki.", "danger")
            return render_template(
                "net_delete.html",
                net=net,
                participant_count=participant_count,
                reason=reason,
            )

        snapshot = {
            "net": {
                key: net[key]
                for key in (
                    "id",
                    "title",
                    "net_date",
                    "started_at",
                    "ended_at",
                    "status",
                    "leader_id",
                    "leader_name",
                    "leader_callsign",
                    "schedule_type",
                    "scheduled_date",
                    "repeater",
                    "control_callsign",
                    "notes",
                    "created_at",
                )
            },
            "participants": [dict(participant) for participant in participants],
        }
        deleted_at = now_db()
        db.execute(
            """INSERT INTO net_deletions
               (original_net_id, title, net_date, started_at, ended_at,
                leader_callsign, participant_count, reason, snapshot,
                deleted_by, deleted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                net_id,
                net["title"],
                net["net_date"],
                net["started_at"],
                net["ended_at"],
                net["leader_callsign"],
                participant_count,
                reason[:1000],
                json.dumps(snapshot, ensure_ascii=False),
                g.user["id"],
                deleted_at,
            ),
        )
        db.execute("DELETE FROM nets WHERE id=?", (net_id,))
        for callsign in {participant["callsign"] for participant in participants}:
            refresh_callsign_usage(db, callsign)
        audit(
            "delete",
            "net",
            net_id,
            json.dumps(
                {
                    "title": net["title"],
                    "net_date": net["net_date"],
                    "participant_count": participant_count,
                    "reason": reason[:1000],
                },
                ensure_ascii=False,
            ),
        )
        flash("Zaključeni sked je premaknjen v koš skupaj z razlogom in kopijo podatkov.", "success")
        return redirect(url_for("archive"))

    return render_template(
        "net_delete.html", net=net, participant_count=participant_count, reason=""
    )


@app.post("/nets/<int:net_id>/close")
@login_required
def close_net(net_id):
    net = fetch_net(net_id)
    if not can_manage_participants(net):
        abort(403)
    if net["status"] == "open":
        get_db().execute(
            "UPDATE nets SET status='closed', ended_at=? WHERE id=?", (now_db(), net_id)
        )
        get_db().commit()
        audit("close", "net", net_id, net["title"])
        flash("Sked je zaključen in shranjen v arhiv.", "success")
    return redirect(url_for("net_detail", net_id=net_id))


@app.post("/nets/<int:net_id>/reopen")
@admin_required
def reopen_net(net_id):
    net = fetch_net(net_id)
    get_db().execute("UPDATE nets SET status='open', ended_at=NULL WHERE id=?", (net_id,))
    get_db().commit()
    audit("reopen", "net", net_id, net["title"])
    flash("Sked je ponovno odprt.", "warning")
    return redirect(url_for("net_detail", net_id=net_id))


def report_filters():
    values = {
        "from_date": request.args.get("from_date", "").strip(),
        "to_date": request.args.get("to_date", "").strip(),
        "schedule_type": request.args.get("schedule_type", "all").strip(),
        "status": request.args.get("status", "all").strip(),
    }
    for key in ("from_date", "to_date"):
        if values[key]:
            try:
                date.fromisoformat(values[key])
            except ValueError:
                values[key] = ""
    if values["schedule_type"] not in {"all", "monthly", "saturday", "other"}:
        values["schedule_type"] = "all"
    if values["status"] not in {"all", "open", "closed"}:
        values["status"] = "all"
    return values


def report_where(filters, alias="n"):
    clauses = []
    parameters = []
    if filters["from_date"]:
        clauses.append(f"{alias}.net_date>=?")
        parameters.append(filters["from_date"])
    if filters["to_date"]:
        clauses.append(f"{alias}.net_date<=?")
        parameters.append(filters["to_date"])
    if filters["schedule_type"] == "other":
        clauses.append(f"{alias}.schedule_type IS NULL")
    elif filters["schedule_type"] != "all":
        clauses.append(f"{alias}.schedule_type=?")
        parameters.append(filters["schedule_type"])
    if filters["status"] != "all":
        clauses.append(f"{alias}.status=?")
        parameters.append(filters["status"])
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", parameters


def report_net_rows(filters):
    where, parameters = report_where(filters)
    return get_db().execute(
        f"""SELECT n.*, u.full_name AS leader_name, u.callsign AS leader_callsign,
                   COUNT(p.id) AS participant_count
            FROM nets n JOIN users u ON u.id=n.leader_id
            LEFT JOIN participants p ON p.net_id=n.id
            {where}
            GROUP BY n.id ORDER BY n.net_date, n.started_at""",
        parameters,
    ).fetchall()


@app.route("/statistics")
@login_required
def statistics():
    filters = report_filters()
    rows = report_net_rows(filters)
    total_participants = sum(row["participant_count"] for row in rows)
    net_count = len(rows)
    summary = {
        "net_count": net_count,
        "participant_count": total_participants,
        "average": round(total_participants / net_count, 1) if net_count else 0,
    }

    where, parameters = report_where(filters)
    unique_row = get_db().execute(
        f"""SELECT COUNT(DISTINCT UPPER(p.callsign)) AS n
            FROM nets n JOIN participants p ON p.net_id=n.id {where}""",
        parameters,
    ).fetchone()
    summary["unique_callsigns"] = unique_row["n"]

    monthly = {}
    for row in rows:
        month = row["net_date"][:7]
        bucket = monthly.setdefault(month, {"month": month, "nets": 0, "participants": 0})
        bucket["nets"] += 1
        bucket["participants"] += row["participant_count"]
    monthly_rows = list(monthly.values())
    max_month_participants = max(
        (item["participants"] for item in monthly_rows), default=1
    )

    participant_condition = " AND " if where else " WHERE "
    top_participants = get_db().execute(
        f"""SELECT UPPER(p.callsign) AS callsign, MAX(p.full_name) AS full_name,
                   COUNT(*) AS attendance_count
            FROM nets n JOIN participants p ON p.net_id=n.id
            {where}{participant_condition}p.callsign<>''
            GROUP BY UPPER(p.callsign)
            ORDER BY attendance_count DESC, callsign LIMIT 10""",
        parameters,
    ).fetchall()
    max_attendance = max(
        (row["attendance_count"] for row in top_participants), default=1
    )

    leaders = get_db().execute(
        f"""SELECT u.full_name, u.callsign, COUNT(DISTINCT n.id) AS net_count
            FROM nets n JOIN users u ON u.id=n.leader_id {where}
            GROUP BY u.id ORDER BY net_count DESC, u.callsign""",
        parameters,
    ).fetchall()
    query_string = request.query_string.decode("utf-8")
    return render_template(
        "statistics.html",
        filters=filters,
        summary=summary,
        monthly_rows=monthly_rows,
        max_month_participants=max_month_participants,
        top_participants=top_participants,
        max_attendance=max_attendance,
        leaders=leaders,
        export_query=query_string,
    )


@app.route("/audit")
@admin_required
def audit_log_view():
    action = request.args.get("action", "").strip()
    entity_type = request.args.get("entity_type", "").strip()
    from_date = request.args.get("from_date", "").strip()
    to_date = request.args.get("to_date", "").strip()
    try:
        user_id = int(request.args.get("user_id", "0") or 0)
    except ValueError:
        user_id = 0
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1

    clauses = []
    parameters = []
    if action:
        clauses.append("a.action=?")
        parameters.append(action)
    if entity_type:
        clauses.append("a.entity_type=?")
        parameters.append(entity_type)
    if user_id:
        clauses.append("a.user_id=?")
        parameters.append(user_id)
    for value, operator in ((from_date, ">="), (to_date, "<=")):
        try:
            valid_date = date.fromisoformat(value).isoformat() if value else ""
        except ValueError:
            valid_date = ""
        if valid_date:
            clauses.append(f"DATE(a.created_at){operator}?")
            parameters.append(valid_date)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    db = get_db()
    total = db.execute(
        f"SELECT COUNT(*) AS n FROM audit_log a{where}", parameters
    ).fetchone()["n"]
    per_page = 100
    rows = db.execute(
        f"""SELECT a.*, u.full_name AS user_name, u.callsign AS user_callsign
            FROM audit_log a LEFT JOIN users u ON u.id=a.user_id
            {where} ORDER BY a.id DESC LIMIT ? OFFSET ?""",
        [*parameters, per_page, (page - 1) * per_page],
    ).fetchall()
    users_rows = db.execute(
        "SELECT id, full_name, callsign FROM users ORDER BY full_name"
    ).fetchall()
    actions = db.execute(
        "SELECT DISTINCT action FROM audit_log ORDER BY action"
    ).fetchall()
    entities = db.execute(
        "SELECT DISTINCT entity_type FROM audit_log ORDER BY entity_type"
    ).fetchall()
    return render_template(
        "audit.html",
        rows=rows,
        users=users_rows,
        actions=actions,
        entities=entities,
        selected_action=action,
        selected_entity=entity_type,
        selected_user=user_id,
        from_date=from_date,
        to_date=to_date,
        page=page,
        total=total,
        has_next=page * per_page < total,
    )


@app.route("/backups")
@admin_required
def backups_view():
    return render_template(
        "backups.html", backups=list_backups(), backup_state=backup_status()
    )


@app.post("/backups/create")
@admin_required
def create_backup_view():
    try:
        path = create_backup("manual")
    except (OSError, sqlite3.Error, RuntimeError) as error:
        flash(f"Varnostne kopije ni bilo mogoče izdelati: {error}", "danger")
    else:
        audit("create", "backup", None, path.name)
        try:
            mirrored = mirror_backup(path)
        except (OSError, sqlite3.Error, RuntimeError) as error:
            flash(
                "Lokalna kopija je izdelana, druga kopija pa ni uspela: "
                f"{error}",
                "warning",
            )
        else:
            message = "Nova varnostna kopija je izdelana in preverjena."
            if mirrored:
                message += " Preverjena je tudi druga kopija."
            flash(message, "success")
    return redirect(url_for("backups_view"))


@app.get("/system")
@admin_required
def system_status_view():
    db = get_db()
    try:
        integrity = db.execute("PRAGMA quick_check").fetchone()[0]
    except sqlite3.Error:
        integrity = "error"
    try:
        database_size = Path(DB_PATH).stat().st_size
    except OSError:
        database_size = 0
    counts = {
        "nets": db.execute("SELECT COUNT(*) AS n FROM nets").fetchone()["n"],
        "participants": db.execute(
            "SELECT COUNT(*) AS n FROM participants"
        ).fetchone()["n"],
        "users": db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"],
        "pending_imports": db.execute(
            "SELECT COUNT(*) AS n FROM csv_imports WHERE status='pending'"
        ).fetchone()["n"],
    }
    return render_template(
        "system_status.html",
        backup_state=backup_status(),
        database_integrity=integrity,
        database_size=database_size,
        schema_current=schema_version(db),
        schema_latest=LATEST_SCHEMA_VERSION,
        secure_cookie=app.config["SESSION_COOKIE_SECURE"],
        trust_proxy=TRUST_PROXY,
        trusted_proxy_networks=[str(network) for network in TRUSTED_PROXY_NETWORKS],
        trusted_hosts=TRUSTED_HOSTS,
        session_cookie_name=app.config["SESSION_COOKIE_NAME"],
        system_metrics=collect_system_metrics(),
        counts=counts,
    )


@app.get("/system/metrics")
@admin_required
def system_metrics_view():
    return collect_system_metrics()


@app.get("/backups/<path:name>/download")
@admin_required
def download_backup(name):
    try:
        path = backup_path(name)
    except ValueError:
        abort(404)
    if not path.is_file() or not verify_database(path):
        abort(404)
    return send_file(
        path,
        as_attachment=True,
        download_name=path.name,
        mimetype="application/vnd.sqlite3",
    )


def normalize_csv_header(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = value.encode("ascii", "ignore").decode("ascii").lower().strip()
    normalized = "".join(character if character.isalnum() else "_" for character in value)
    while "__" in normalized:
        normalized = normalized.replace("__", "_")
    aliases = {
        "zacetna_ura": "zacetek",
        "koncna_ura": "konec",
        "klicni_znak": "klicni_znak",
        "klicniznak": "klicni_znak",
        "ura_prijave": "prijava",
        "opomba": "opombe",
    }
    normalized = normalized.strip("_")
    return aliases.get(normalized, normalized)


def parse_import_date(value):
    compact = str(value or "").strip()
    for pattern in ("%Y-%m-%d", "%d.%m.%Y", "%d. %m. %Y"):
        try:
            return datetime.strptime(compact, pattern).date()
        except ValueError:
            continue
    raise ValueError


def parse_import_time(value):
    compact = str(value or "").strip()
    for pattern in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(compact, pattern).time().replace(second=0)
        except ValueError:
            continue
    raise ValueError


def parse_csv_import(upload):
    errors = []

    def add_error(line_number, message):
        if len(errors) < 100:
            errors.append({"line": line_number, "message": message})

    raw = upload.read(1024 * 1024 + 1)
    if not raw:
        return None, [{"line": 0, "message": "Izbrana datoteka je prazna."}]
    if len(raw) > 1024 * 1024:
        return None, [{"line": 0, "message": "Datoteka je večja od 1 MB."}]
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, [{"line": 0, "message": "Datoteka mora biti shranjena kot UTF-8 CSV."}]
    if "\x00" in text:
        return None, [{"line": 0, "message": "Datoteka ni veljaven besedilni CSV."}]

    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=";,\t")
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    except csv.Error:
        reader = csv.DictReader(io.StringIO(text), delimiter=";")
    if not reader.fieldnames:
        return None, [{"line": 1, "message": "CSV nima glave stolpcev."}]

    header_pairs = [
        (original, normalize_csv_header(original)) for original in reader.fieldnames
    ]
    normalized_header_list = [normalized for _original, normalized in header_pairs if normalized]
    normalized_headers = set(normalized_header_list)
    if len(normalized_header_list) != len(normalized_headers):
        return None, [
            {"line": 1, "message": "CSV vsebuje podvojena imena stolpcev."}
        ]
    required_headers = {"datum", "zacetek", "operater", "klicni_znak", "ime"}
    missing = sorted(required_headers - normalized_headers)
    if missing:
        return None, [
            {
                "line": 1,
                "message": "Manjkajo obvezni stolpci: " + ", ".join(missing) + ".",
            }
        ]

    db = get_db()
    operator_map = {}
    for user in db.execute(
        "SELECT id, username, full_name, callsign, active FROM users ORDER BY active DESC, id"
    ).fetchall():
        for key in (user["username"], user["callsign"]):
            operator_map.setdefault(str(key).strip().casefold(), user)

    type_map = {
        "": None,
        "izredni": None,
        "other": None,
        "sobotni": SCHEDULE_SATURDAY,
        "saturday": SCHEDULE_SATURDAY,
        "mesecni": SCHEDULE_MONTHLY,
        "monthly": SCHEDULE_MONTHLY,
    }
    try:
        source_rows = list(reader)
    except csv.Error:
        return None, [{"line": 0, "message": "CSV vsebuje nepravilne narekovaje ali vrstice."}]

    groups = {}
    input_row_count = 0
    for line_number, source_row in enumerate(source_rows, start=2):
        row = {
            normalized: str(source_row.get(original) or "").strip()
            for original, normalized in header_pairs
            if normalized
        }
        if not any(row.values()):
            continue
        input_row_count += 1
        if input_row_count > 3000:
            add_error(line_number, "Uvoz je omejen na največ 3000 podatkovnih vrstic.")
            break

        try:
            net_date_value = parse_import_date(row.get("datum"))
        except ValueError:
            add_error(line_number, "Datum mora biti v obliki LLLL-MM-DD ali DD.MM.LLLL.")
            continue
        try:
            start_time_value = parse_import_time(row.get("zacetek"))
        except ValueError:
            add_error(line_number, "Začetek mora biti v obliki UU:MM.")
            continue

        end_time_value = None
        if row.get("konec"):
            try:
                end_time_value = parse_import_time(row["konec"])
            except ValueError:
                add_error(line_number, "Konec mora biti prazen ali v obliki UU:MM.")
                continue

        type_key = normalize_csv_header(row.get("vrsta", ""))
        if type_key not in type_map:
            add_error(line_number, "Vrsta mora biti sobotni, mesečni ali izredni.")
            continue
        schedule_type = type_map[type_key]
        scheduled = None
        if schedule_type:
            scheduled = scheduled_net_for_date(schedule_type, net_date_value)
            if scheduled is None:
                label = "sobota" if schedule_type == SCHEDULE_SATURDAY else "prvi četrtek v mesecu"
                add_error(line_number, f"Datum ne ustreza rednemu terminu ({label}).")
                continue

        operator_key = row.get("operater", "").strip().casefold()
        operator = operator_map.get(operator_key)
        if operator is None:
            add_error(
                line_number,
                f"Operater {row.get('operater') or '–'} ni uporabnik portala.",
            )
            continue

        title = row.get("naslov", "").strip()[:180]
        if not title:
            title = (
                scheduled["title"]
                if scheduled
                else f"Sked {net_date_value.strftime('%d. %m. %Y')}"
            )
        start_at = datetime.combine(net_date_value, start_time_value)
        end_at = None
        if end_time_value:
            end_at = datetime.combine(net_date_value, end_time_value)
            if end_at < start_at:
                end_at += timedelta(days=1)

        if schedule_type:
            group_key = f"regular:{schedule_type}:{net_date_value.isoformat()}"
        else:
            group_key = "manual:" + "|".join(
                (
                    net_date_value.isoformat(),
                    start_time_value.strftime("%H:%M"),
                    title.casefold(),
                    str(operator["id"]),
                )
            )
        notes = row.get("opombe", "").strip()[:5000] or None
        group = groups.get(group_key)
        if group is None:
            group = {
                "title": title,
                "net_date": net_date_value.isoformat(),
                "started_at": start_at.strftime("%Y-%m-%d %H:%M:%S"),
                "ended_at": end_at.strftime("%Y-%m-%d %H:%M:%S") if end_at else None,
                "leader_id": operator["id"],
                "leader_label": f"{operator['full_name']} ({operator['callsign']})",
                "schedule_type": schedule_type,
                "scheduled_date": net_date_value.isoformat() if schedule_type else None,
                "repeater": scheduled["repeater"] if scheduled else None,
                "control_callsign": scheduled["control_callsign"] if scheduled else None,
                "notes": notes,
                "participants": [],
                "callsigns": [],
                "first_line": line_number,
            }
            groups[group_key] = group
        else:
            current_values = (
                title,
                start_at.strftime("%Y-%m-%d %H:%M:%S"),
                end_at.strftime("%Y-%m-%d %H:%M:%S") if end_at else None,
                operator["id"],
                notes,
            )
            stored_values = (
                group["title"],
                group["started_at"],
                group["ended_at"],
                group["leader_id"],
                group["notes"],
            )
            if current_values != stored_values:
                add_error(
                    line_number,
                    "Vrstice istega skeda imajo različne podatke o skedu.",
                )
                continue

        callsign = normalize_callsign(row.get("klicni_znak", ""))
        full_name = row.get("ime", "").strip()[:120]
        if bool(callsign) != bool(full_name):
            add_error(line_number, "Klicni znak in ime morata biti oba izpolnjena ali oba prazna.")
            continue
        if callsign:
            if not valid_callsign(callsign):
                add_error(
                    line_number,
                    "Klicni znak lahko vsebuje samo črke, številke, / in - (2–24 znakov).",
                )
                continue
            if callsign.casefold() in {value.casefold() for value in group["callsigns"]}:
                add_error(line_number, f"Klicni znak {callsign} je v istem skedu podvojen.")
                continue
            checkin_time_value = start_time_value
            if row.get("prijava"):
                try:
                    checkin_time_value = parse_import_time(row["prijava"])
                except ValueError:
                    add_error(line_number, "Ura prijave mora biti v obliki UU:MM.")
                    continue
            checkin_at = datetime.combine(net_date_value, checkin_time_value)
            if end_at and end_at.date() > net_date_value and checkin_at < start_at:
                checkin_at += timedelta(days=1)
            group["participants"].append(
                {
                    "callsign": callsign,
                    "full_name": full_name,
                    "checkin_at": checkin_at.strftime("%Y-%m-%d %H:%M:%S"),
                    "line": line_number,
                }
            )
            group["callsigns"].append(callsign)

    nets = list(groups.values())
    if not nets and not errors:
        add_error(0, "CSV ne vsebuje nobenega skeda.")
    if not errors:
        for net in nets:
            if net["schedule_type"]:
                existing = db.execute(
                    """SELECT id FROM nets WHERE schedule_type=?
                       AND COALESCE(scheduled_date,net_date)=?""",
                    (net["schedule_type"], net["scheduled_date"]),
                ).fetchone()
            else:
                existing = db.execute(
                    """SELECT id FROM nets
                       WHERE title=? AND net_date=? AND started_at=?""",
                    (net["title"], net["net_date"], net["started_at"]),
                ).fetchone()
            if existing:
                add_error(
                    net["first_line"],
                    f"Sked že obstaja v portalu (ID {existing['id']}).",
                )

    for net in nets:
        net.pop("callsigns", None)
    result = {
        "nets": nets,
        "net_count": len(nets),
        "participant_count": sum(len(net["participants"]) for net in nets),
        "input_row_count": input_row_count,
    }
    return result, errors


def fetch_csv_import(import_id, pending_only=False):
    clauses = ["id=?", "created_by=?"]
    parameters = [import_id, g.user["id"]]
    if pending_only:
        clauses.append("status='pending'")
    row = get_db().execute(
        f"SELECT * FROM csv_imports WHERE {' AND '.join(clauses)}", parameters
    ).fetchone()
    if row is None:
        abort(404)
    return row


@app.route("/imports/csv", methods=["GET", "POST"])
@admin_required
def csv_import_view():
    errors = []
    db = get_db()
    pending_cutoff = (now_local() - timedelta(hours=24)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    db.execute(
        "DELETE FROM csv_imports WHERE status='pending' AND created_at<?",
        (pending_cutoff,),
    )
    db.commit()
    if request.method == "POST":
        upload = request.files.get("csv_file")
        if upload is None or not upload.filename:
            errors.append({"line": 0, "message": "Izberi CSV-datoteko."})
        elif not upload.filename.lower().endswith(".csv"):
            errors.append({"line": 0, "message": "Datoteka mora imeti končnico .csv."})
        else:
            parsed, errors = parse_csv_import(upload)
            if not errors:
                db.execute(
                    "DELETE FROM csv_imports WHERE status='pending' AND created_by=?",
                    (g.user["id"],),
                )
                cursor = db.execute(
                    """INSERT INTO csv_imports
                       (filename, data_json, net_count, participant_count,
                        status, created_by, created_at)
                       VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
                    (
                        upload.filename[:180],
                        json.dumps(parsed, ensure_ascii=False),
                        parsed["net_count"],
                        parsed["participant_count"],
                        g.user["id"],
                        now_db(),
                    ),
                )
                db.commit()
                return redirect(url_for("csv_import_preview", import_id=cursor.lastrowid))
    return render_template("csv_import.html", errors=errors)


@app.get("/imports/csv/template")
@admin_required
def csv_import_template():
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=";")
    writer.writerow(
        [
            "datum", "zacetek", "konec", "naslov", "vrsta", "operater",
            "klicni_znak", "ime", "prijava", "opombe",
        ]
    )
    writer.writerow(
        [
            "2026-07-25", "21:00", "21:45", "", "sobotni",
            g.user["callsign"], "S51ABC", "Janez Novak", "21:03",
            "Primer zgodovinskega skeda",
        ]
    )
    writer.writerow(
        [
            "2026-07-25", "21:00", "21:45", "", "sobotni",
            g.user["callsign"], "S52XYZ", "Maja Kovač", "21:08",
            "Primer zgodovinskega skeda",
        ]
    )
    return Response(
        "\ufeff" + output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=predloga-uvoz-skedov.csv"},
    )


@app.get("/imports/csv/<int:import_id>")
@admin_required
def csv_import_preview(import_id):
    batch = fetch_csv_import(import_id, pending_only=True)
    try:
        parsed = json.loads(batch["data_json"])
    except (json.JSONDecodeError, TypeError):
        abort(404)
    return render_template("csv_import_preview.html", batch=batch, parsed=parsed)


@app.post("/imports/csv/<int:import_id>/cancel")
@admin_required
def cancel_csv_import(import_id):
    batch = fetch_csv_import(import_id, pending_only=True)
    db = get_db()
    db.execute(
        "UPDATE csv_imports SET status='canceled', data_json=NULL WHERE id=?",
        (batch["id"],),
    )
    db.commit()
    flash("Predogled uvoza je preklican; podatki niso bili spremenjeni.", "success")
    return redirect(url_for("csv_import_view"))


@app.post("/imports/csv/<int:import_id>/confirm")
@admin_required
def confirm_csv_import(import_id):
    batch = fetch_csv_import(import_id, pending_only=True)
    try:
        parsed = json.loads(batch["data_json"])
        nets = parsed["nets"]
        if not isinstance(nets, list) or not nets:
            raise ValueError
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        flash("Shranjeni predogled ni veljaven; pripravi uvoz znova.", "danger")
        return redirect(url_for("csv_import_view"))

    try:
        backup = create_backup("pre-import")
    except (OSError, sqlite3.Error, RuntimeError) as error:
        flash(f"Uvoz je ustavljen, ker varnostna kopija ni uspela: {error}", "danger")
        return redirect(url_for("csv_import_preview", import_id=import_id))
    audit("create", "backup", None, backup.name)
    try:
        mirror_backup(backup)
    except (OSError, sqlite3.Error, RuntimeError):
        pass

    db = get_db()
    created_net_ids = []
    affected_callsigns = set()
    try:
        db.execute("BEGIN IMMEDIATE")
        for net in nets:
            leader = db.execute(
                "SELECT id FROM users WHERE id=?", (net["leader_id"],)
            ).fetchone()
            if leader is None:
                raise ValueError("Operater ne obstaja več.")
            if net["schedule_type"]:
                conflict = db.execute(
                    """SELECT id FROM nets WHERE schedule_type=?
                       AND COALESCE(scheduled_date,net_date)=?""",
                    (net["schedule_type"], net["scheduled_date"]),
                ).fetchone()
            else:
                conflict = db.execute(
                    """SELECT id FROM nets
                       WHERE title=? AND net_date=? AND started_at=?""",
                    (net["title"], net["net_date"], net["started_at"]),
                ).fetchone()
            if conflict:
                raise ValueError(f"Sked {net['title']} že obstaja.")

            cursor = db.execute(
                """INSERT INTO nets
                   (title, net_date, scheduled_date, started_at, ended_at, status,
                    leader_id, schedule_type, repeater, control_callsign,
                    notes, created_at)
                   VALUES (?, ?, ?, ?, ?, 'closed', ?, ?, ?, ?, ?, ?)""",
                (
                    net["title"],
                    net["net_date"],
                    net["scheduled_date"],
                    net["started_at"],
                    net["ended_at"],
                    net["leader_id"],
                    net["schedule_type"],
                    net["repeater"],
                    net["control_callsign"],
                    net["notes"],
                    now_db(),
                ),
            )
            net_id = cursor.lastrowid
            created_net_ids.append(net_id)
            for participant in net["participants"]:
                db.execute(
                    """INSERT INTO participants
                       (net_id, full_name, callsign, checkin_at,
                        created_by, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        net_id,
                        participant["full_name"],
                        participant["callsign"],
                        participant["checkin_at"],
                        g.user["id"],
                        now_db(),
                    ),
                )
                learn_callsign(
                    db,
                    participant["callsign"],
                    participant["full_name"],
                    g.user["id"],
                    participant["checkin_at"],
                )
                affected_callsigns.add(participant["callsign"])
        for callsign in affected_callsigns:
            refresh_callsign_usage(db, callsign)
        imported_at = now_db()
        db.execute(
            """UPDATE csv_imports SET status='imported', data_json=NULL,
               imported_by=?, imported_at=? WHERE id=? AND status='pending'""",
            (g.user["id"], imported_at, import_id),
        )
        db.commit()
    except (KeyError, TypeError, ValueError, sqlite3.Error) as error:
        db.rollback()
        flash(f"Uvoz ni uspel in ni spremenil baze: {error}", "danger")
        return redirect(url_for("csv_import_preview", import_id=import_id))

    audit(
        "import",
        "csv_import",
        import_id,
        json.dumps(
            {
                "filename": batch["filename"],
                "net_count": len(created_net_ids),
                "participant_count": parsed["participant_count"],
                "backup": backup.name,
            },
            ensure_ascii=False,
        ),
    )
    flash(
        f"Uvoženih je {len(created_net_ids)} skedov in "
        f"{parsed['participant_count']} prijavljenih.",
        "success",
    )
    return redirect(url_for("archive"))


def report_participant_rows(filters):
    where, parameters = report_where(filters)
    return get_db().execute(
        f"""SELECT n.net_date, n.title, n.schedule_type, n.status,
                   n.started_at, n.ended_at, n.notes, u.full_name AS leader_name,
                   u.callsign AS leader_callsign, p.callsign,
                   p.full_name, p.checkin_at
            FROM nets n JOIN users u ON u.id=n.leader_id
            LEFT JOIN participants p ON p.net_id=n.id
            {where}
            ORDER BY n.net_date, n.started_at, p.checkin_at, p.id""",
        parameters,
    ).fetchall()


def csv_safe(value):
    text = "" if value is None else str(value)
    if text.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


@app.route("/reports/export.csv")
@admin_required
def export_csv():
    filters = report_filters()
    rows = report_participant_rows(filters)
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=";")
    writer.writerow(
        [
            "Datum", "Naslov", "Vrsta", "Status", "Začetek", "Konec",
            "Operater", "Klicni znak operaterja", "Klicni znak prijavljenega",
            "Ime prijavljenega", "Ura prijave", "Opombe skeda",
        ]
    )
    type_labels = {"monthly": "Mesečni", "saturday": "Sobotni", None: "Izredni"}
    for row in rows:
        writer.writerow(
            [csv_safe(value) for value in [
                row["net_date"], row["title"], type_labels.get(row["schedule_type"], "Izredni"),
                "Zaključen" if row["status"] == "closed" else "Odprt",
                row["started_at"], row["ended_at"] or "", row["leader_name"],
                row["leader_callsign"], row["callsign"] or "",
                row["full_name"] or "", row["checkin_at"] or "", row["notes"] or "",
            ]]
        )
    content = "\ufeff" + output.getvalue()
    return Response(
        content,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=skedi-izvoz.csv"},
    )


@app.route("/reports/print")
@admin_required
def print_report():
    filters = report_filters()
    rows = report_net_rows(filters)
    return render_template(
        "print_report.html", filters=filters, rows=rows, generated_at=now_db()
    )


@app.route("/archive")
@login_required
def archive():
    filters = report_filters()
    query = request.args.get("q", "").strip()[:100]
    try:
        page = max(1, int(request.args.get("page", "1")))
    except (TypeError, ValueError):
        page = 1

    clauses = []
    parameters = []
    if filters["from_date"]:
        clauses.append("n.net_date>=?")
        parameters.append(filters["from_date"])
    if filters["to_date"]:
        clauses.append("n.net_date<=?")
        parameters.append(filters["to_date"])
    if filters["schedule_type"] == "other":
        clauses.append("n.schedule_type IS NULL")
    elif filters["schedule_type"] != "all":
        clauses.append("n.schedule_type=?")
        parameters.append(filters["schedule_type"])
    if filters["status"] != "all":
        clauses.append("n.status=?")
        parameters.append(filters["status"])
    if query:
        search = f"%{query}%"
        clauses.append(
            """(n.title LIKE ? OR u.full_name LIKE ? OR u.callsign LIKE ?
                 OR EXISTS (
                     SELECT 1 FROM participants searched
                     WHERE searched.net_id=n.id
                       AND (searched.callsign LIKE ? OR searched.full_name LIKE ?)
                 ))"""
        )
        parameters.extend([search, search, search, search, search])

    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    db = get_db()
    total = db.execute(
        f"""SELECT COUNT(*) AS n FROM nets n
            JOIN users u ON u.id=n.leader_id{where}""",
        parameters,
    ).fetchone()["n"]
    per_page = 25
    page_count = max(1, (total + per_page - 1) // per_page)
    page = min(page, page_count)
    rows = db.execute(
        f"""SELECT n.*, u.full_name AS leader_name,
                   u.callsign AS leader_callsign,
                   (SELECT COUNT(*) FROM participants counted
                    WHERE counted.net_id=n.id) AS participant_count
            FROM nets n JOIN users u ON u.id=n.leader_id
            {where} ORDER BY n.started_at DESC, n.id DESC LIMIT ? OFFSET ?""",
        [*parameters, per_page, (page - 1) * per_page],
    ).fetchall()
    return render_template(
        "archive_v3.html",
        nets=rows,
        filters=filters,
        query=query,
        page=page,
        page_count=page_count,
        total=total,
        has_previous=page > 1,
        has_next=page < page_count,
    )


def fetch_net_deletion(deletion_id):
    row = get_db().execute(
        """SELECT d.*, deleted_user.full_name AS deleted_by_name,
                  deleted_user.callsign AS deleted_by_callsign,
                  restored_user.full_name AS restored_by_name,
                  restored_user.callsign AS restored_by_callsign
           FROM net_deletions d
           JOIN users deleted_user ON deleted_user.id=d.deleted_by
           LEFT JOIN users restored_user ON restored_user.id=d.restored_by
           WHERE d.id=?""",
        (deletion_id,),
    ).fetchone()
    if row is None:
        abort(404)
    return row


def parse_net_deletion_snapshot(deletion):
    try:
        snapshot = json.loads(deletion["snapshot"])
        if not isinstance(snapshot, dict) or not isinstance(
            snapshot.get("net"), dict
        ) or not isinstance(
            snapshot.get("participants"), list
        ):
            raise ValueError
        return snapshot
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


@app.get("/deleted-nets")
@admin_required
def deleted_nets():
    rows = get_db().execute(
        """SELECT d.*, deleted_user.full_name AS deleted_by_name,
                  deleted_user.callsign AS deleted_by_callsign,
                  restored_user.full_name AS restored_by_name,
                  restored_user.callsign AS restored_by_callsign
           FROM net_deletions d
           JOIN users deleted_user ON deleted_user.id=d.deleted_by
           LEFT JOIN users restored_user ON restored_user.id=d.restored_by
           ORDER BY d.deleted_at DESC, d.id DESC"""
    ).fetchall()
    return render_template("deleted_nets.html", deletions=rows)


@app.get("/deleted-nets/<int:deletion_id>")
@admin_required
def deleted_net_detail(deletion_id):
    deletion = fetch_net_deletion(deletion_id)
    snapshot = parse_net_deletion_snapshot(deletion)
    restored_exists = False
    if deletion["restored_net_id"]:
        restored_exists = (
            get_db().execute(
                "SELECT id FROM nets WHERE id=?", (deletion["restored_net_id"],)
            ).fetchone()
            is not None
        )
    return render_template(
        "deleted_net_detail.html",
        deletion=deletion,
        snapshot=snapshot,
        restored_exists=restored_exists,
    )


@app.post("/deleted-nets/<int:deletion_id>/restore")
@admin_required
def restore_deleted_net(deletion_id):
    deletion = fetch_net_deletion(deletion_id)
    if deletion["restored_at"]:
        flash("Ta izbrisani sked je že bil obnovljen.", "warning")
        return redirect(url_for("deleted_net_detail", deletion_id=deletion_id))

    snapshot = parse_net_deletion_snapshot(deletion)
    if snapshot is None:
        flash("Shranjena kopija skeda je poškodovana in je ni mogoče obnoviti.", "danger")
        return redirect(url_for("deleted_net_detail", deletion_id=deletion_id))

    net_data = snapshot["net"]
    participants = snapshot["participants"]
    db = get_db()
    try:
        title = str(net_data["title"]).strip()[:180]
        net_date = date.fromisoformat(str(net_data["net_date"])).isoformat()
        started_at = str(net_data["started_at"])
        ended_at = net_data.get("ended_at")
        datetime.strptime(started_at[:19], "%Y-%m-%d %H:%M:%S")
        if ended_at:
            datetime.strptime(str(ended_at)[:19], "%Y-%m-%d %H:%M:%S")
        if not title:
            raise ValueError

        leader_id = net_data.get("leader_id")
        leader = db.execute("SELECT id FROM users WHERE id=?", (leader_id,)).fetchone()
        if leader is None:
            leader_id = g.user["id"]

        schedule_type = net_data.get("schedule_type")
        if schedule_type not in {SCHEDULE_MONTHLY, SCHEDULE_SATURDAY}:
            schedule_type = None
        scheduled_date = None
        if schedule_type:
            scheduled_date = net_data.get("scheduled_date") or net_date
            existing = db.execute(
                """SELECT id FROM nets WHERE schedule_type=?
                   AND COALESCE(scheduled_date,net_date)=?""",
                (schedule_type, scheduled_date),
            ).fetchone()
            if existing:
                flash(
                    "Obnova ni mogoča, ker dnevnik za isti redni termin že obstaja.",
                    "warning",
                )
                return redirect(
                    url_for("deleted_net_detail", deletion_id=deletion_id)
                )

        cursor = db.execute(
            """INSERT INTO nets
               (title, net_date, scheduled_date, started_at, ended_at, status,
                leader_id, schedule_type, repeater, control_callsign, notes, created_at)
               VALUES (?, ?, ?, ?, ?, 'closed', ?, ?, ?, ?, ?, ?)""",
            (
                title,
                net_date,
                scheduled_date,
                started_at,
                ended_at,
                leader_id,
                schedule_type,
                net_data.get("repeater"),
                net_data.get("control_callsign"),
                net_data.get("notes"),
                net_data.get("created_at") or now_db(),
            ),
        )
        restored_net_id = cursor.lastrowid
        affected_callsigns = set()
        for participant in participants:
            full_name = str(participant["full_name"]).strip()[:120]
            callsign = (
                str(participant["callsign"]).strip().upper().replace(" ", "")[:24]
            )
            checkin_at = str(participant["checkin_at"])
            datetime.strptime(checkin_at[:19], "%Y-%m-%d %H:%M:%S")
            if not full_name or not callsign:
                raise ValueError
            db.execute(
                """INSERT INTO participants
                   (net_id, full_name, callsign, checkin_at, created_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    restored_net_id,
                    full_name,
                    callsign,
                    checkin_at,
                    g.user["id"],
                    now_db(),
                ),
            )
            learn_callsign(db, callsign, full_name, g.user["id"], checkin_at)
            affected_callsigns.add(callsign)

        for callsign in affected_callsigns:
            refresh_callsign_usage(db, callsign)
        restored_at = now_db()
        db.execute(
            """UPDATE net_deletions
               SET restored_by=?, restored_at=?, restored_net_id=? WHERE id=?""",
            (g.user["id"], restored_at, restored_net_id, deletion_id),
        )
        db.commit()
    except (KeyError, TypeError, ValueError, sqlite3.IntegrityError):
        db.rollback()
        flash("Obnova ni uspela, ker shranjeni podatki niso veljavni.", "danger")
        return redirect(url_for("deleted_net_detail", deletion_id=deletion_id))

    audit(
        "restore",
        "net",
        restored_net_id,
        json.dumps(
            {
                "deletion_id": deletion_id,
                "original_net_id": deletion["original_net_id"],
                "participant_count": len(participants),
            },
            ensure_ascii=False,
        ),
    )
    flash("Sked in vsi prijavljeni so obnovljeni v arhiv.", "success")
    return redirect(url_for("net_detail", net_id=restored_net_id))


@app.route("/callsigns")
@login_required
def callsigns():
    query = request.args.get("q", "").strip()
    parameters = []
    where = ""
    if query:
        where = "WHERE callsign LIKE ? OR full_name LIKE ?"
        search = f"%{query}%"
        parameters = [search, search]
    rows = get_db().execute(
        f"""SELECT * FROM callsign_directory {where}
            ORDER BY active DESC, callsign""",
        parameters,
    ).fetchall()
    return render_template("callsigns_v2.html", entries=rows, query=query)


def fetch_callsign_entry(entry_id):
    row = get_db().execute(
        "SELECT * FROM callsign_directory WHERE id=?", (entry_id,)
    ).fetchone()
    if row is None:
        abort(404)
    return row


@app.route("/callsigns/<int:entry_id>", methods=["GET", "POST"])
@login_required
def callsign_profile(entry_id):
    entry = fetch_callsign_entry(entry_id)
    db = get_db()
    if request.method == "POST":
        if g.user["role"] != "admin":
            abort(403)
        notes = request.form.get("notes", "").strip()[:2000]
        db.execute(
            """UPDATE callsign_directory SET notes=?, updated_by=?, updated_at=?
               WHERE id=?""",
            (notes or None, g.user["id"], now_db(), entry_id),
        )
        db.commit()
        audit("update_notes", "callsign", entry_id, entry["callsign"])
        flash("Interna opomba je shranjena.", "success")
        return redirect(url_for("callsign_profile", entry_id=entry_id))

    history = db.execute(
        """SELECT p.id AS participant_id, p.checkin_at, p.full_name AS entered_name,
                  n.id AS net_id, n.title, n.net_date, n.started_at, n.status,
                  n.schedule_type, u.full_name AS leader_name,
                  u.callsign AS leader_callsign
           FROM participants p JOIN nets n ON n.id=p.net_id
           JOIN users u ON u.id=n.leader_id
           WHERE p.callsign=? COLLATE NOCASE
           ORDER BY n.net_date DESC, p.checkin_at DESC""",
        (entry["callsign"],),
    ).fetchall()
    yearly = db.execute(
        """SELECT SUBSTR(n.net_date,1,4) AS year, COUNT(*) AS attendance_count
           FROM participants p JOIN nets n ON n.id=p.net_id
           WHERE p.callsign=? COLLATE NOCASE
           GROUP BY SUBSTR(n.net_date,1,4) ORDER BY year DESC""",
        (entry["callsign"],),
    ).fetchall()
    summary = {
        "attendance_count": len(history),
        "first_checkin": history[-1]["checkin_at"] if history else None,
        "last_checkin": history[0]["checkin_at"] if history else None,
        "years_count": len(yearly),
    }
    return render_template(
        "callsign_profile.html",
        entry=entry,
        history=history,
        yearly=yearly,
        summary=summary,
    )


@app.route("/callsigns/<int:entry_id>/merge", methods=["GET", "POST"])
@admin_required
def merge_callsign(entry_id):
    source = fetch_callsign_entry(entry_id)
    db = get_db()
    targets = db.execute(
        """SELECT id, callsign, full_name FROM callsign_directory
           WHERE id<>? ORDER BY active DESC, callsign""",
        (entry_id,),
    ).fetchall()
    if request.method == "POST":
        try:
            target_id = int(request.form.get("target_id", ""))
        except (TypeError, ValueError):
            target_id = 0
        target = db.execute(
            "SELECT * FROM callsign_directory WHERE id=? AND id<>?",
            (target_id, entry_id),
        ).fetchone()
        if target is None:
            flash("Izberi veljaven ciljni klicni znak.", "danger")
        else:
            source_callsign = source["callsign"]
            target_callsign = target["callsign"]
            source_count = db.execute(
                """SELECT COUNT(*) AS n FROM participants
                   WHERE callsign=? COLLATE NOCASE""",
                (source_callsign,),
            ).fetchone()["n"]
            duplicate_ids = db.execute(
                """SELECT source.id FROM participants source
                   WHERE source.callsign=? COLLATE NOCASE
                     AND EXISTS (
                         SELECT 1 FROM participants target
                         WHERE target.net_id=source.net_id
                           AND target.callsign=? COLLATE NOCASE
                     )""",
                (source_callsign, target_callsign),
            ).fetchall()
            duplicate_count = len(duplicate_ids)
            for participant in duplicate_ids:
                db.execute("DELETE FROM participants WHERE id=?", (participant["id"],))
            db.execute(
                """UPDATE participants SET callsign=?, updated_by=?, updated_at=?
                   WHERE callsign=? COLLATE NOCASE""",
                (target_callsign, g.user["id"], now_db(), source_callsign),
            )
            notes = target["notes"] or ""
            if source["notes"]:
                separator = "\n\n" if notes else ""
                notes += f"{separator}[Združeno iz {source_callsign}] {source['notes']}"
            db.execute(
                """UPDATE callsign_directory SET notes=?, updated_by=?, updated_at=?
                   WHERE id=?""",
                (notes[:2000] or None, g.user["id"], now_db(), target_id),
            )
            db.execute("DELETE FROM callsign_directory WHERE id=?", (entry_id,))
            refresh_callsign_usage(db, target_callsign)
            db.commit()
            audit(
                "merge",
                "callsign",
                target_id,
                json.dumps(
                    {
                        "source": source_callsign,
                        "target": target_callsign,
                        "moved_participations": source_count - duplicate_count,
                        "removed_duplicates": duplicate_count,
                    },
                    ensure_ascii=False,
                ),
            )
            flash(
                f"Klicni znak {source_callsign} je združen v {target_callsign}.",
                "success",
            )
            return redirect(url_for("callsign_profile", entry_id=target_id))
    return render_template("callsign_merge.html", source=source, targets=targets)


@app.route("/callsigns/new", methods=["GET", "POST"])
@admin_required
def new_callsign():
    if request.method == "POST":
        callsign = normalize_callsign(request.form.get("callsign", ""))
        full_name = request.form.get("full_name", "").strip()
        if not callsign or not full_name:
            flash("Vpiši klicni znak in ime.", "danger")
        elif not valid_callsign(callsign):
            flash(
                "Klicni znak lahko vsebuje samo črke, številke, / in - (2–24 znakov).",
                "danger",
            )
        else:
            try:
                cursor = get_db().execute(
                    """INSERT INTO callsign_directory
                       (callsign, full_name, active, use_count, created_by, created_at)
                       VALUES (?, ?, 1, 0, ?, ?)""",
                    (callsign[:24], full_name[:120], g.user["id"], now_db()),
                )
                get_db().commit()
                audit("create", "callsign", cursor.lastrowid, f"{callsign} – {full_name}")
                flash("Klicni znak je dodan v imenik.", "success")
                return redirect(url_for("callsigns"))
            except sqlite3.IntegrityError:
                get_db().rollback()
                flash("Ta klicni znak je že v imeniku.", "warning")
    return render_template("callsign_form.html", entry=None)


@app.route("/callsigns/<int:entry_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_callsign(entry_id):
    entry = fetch_callsign_entry(entry_id)
    if request.method == "POST":
        callsign = normalize_callsign(request.form.get("callsign", ""))
        full_name = request.form.get("full_name", "").strip()
        active = 1 if request.form.get("active") == "1" else 0
        if not callsign or not full_name:
            flash("Vpiši klicni znak in ime.", "danger")
        elif not valid_callsign(callsign):
            flash(
                "Klicni znak lahko vsebuje samo črke, številke, / in - (2–24 znakov).",
                "danger",
            )
        else:
            try:
                db = get_db()
                db.execute(
                    """UPDATE callsign_directory
                       SET callsign=?, full_name=?, active=?, updated_by=?, updated_at=?
                       WHERE id=?""",
                    (
                        callsign,
                        full_name[:120],
                        active,
                        g.user["id"],
                        now_db(),
                        entry_id,
                    ),
                )
                if callsign != entry["callsign"]:
                    db.execute(
                        """UPDATE participants SET callsign=?, updated_by=?, updated_at=?
                           WHERE callsign=? COLLATE NOCASE""",
                        (callsign, g.user["id"], now_db(), entry["callsign"]),
                    )
                    refresh_callsign_usage(db, callsign)
                db.commit()
                audit("update", "callsign", entry_id, f"{callsign} – {full_name}")
                flash("Vnos v imeniku je posodobljen.", "success")
                return redirect(url_for("callsign_profile", entry_id=entry_id))
            except sqlite3.IntegrityError:
                get_db().rollback()
                flash(
                    "Tega klicnega znaka ni mogoče preimenovati. Če že obstaja, "
                    "uporabi funkcijo Združi.",
                    "warning",
                )
    return render_template("callsign_form.html", entry=entry)


@app.route("/users")
@admin_required
def users():
    rows = get_db().execute("SELECT * FROM users ORDER BY active DESC, full_name").fetchall()
    return render_template("users_v2.html", users=rows, now_value=now_db())


@app.route("/security")
@admin_required
def security_view():
    result = request.args.get("result", "all").strip()
    query = request.args.get("q", "").strip()
    if result not in {"all", "success", "failed", "limited"}:
        result = "all"
    clauses = []
    parameters = []
    if result == "success":
        clauses.append("a.success=1")
    elif result == "failed":
        clauses.append("a.success=0")
    elif result == "limited":
        clauses.append("a.reason IN ('account_locked','ip_limited')")
    if query:
        clauses.append("(a.username LIKE ? OR a.ip_address LIKE ?)")
        search = f"%{query}%"
        parameters.extend([search, search])
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    db = get_db()
    attempts = db.execute(
        f"""SELECT a.*, u.full_name AS user_name, u.callsign AS user_callsign
            FROM login_attempts a LEFT JOIN users u ON u.id=a.user_id
            {where} ORDER BY a.id DESC LIMIT 200""",
        parameters,
    ).fetchall()
    accounts = db.execute(
        """SELECT id, username, full_name, callsign, active, failed_login_count,
                  locked_until, last_login_at, last_login_ip
           FROM users ORDER BY active DESC, full_name"""
    ).fetchall()
    return render_template(
        "security.html",
        attempts=attempts,
        accounts=accounts,
        selected_result=result,
        query=query,
        now_value=now_db(),
        max_failures=LOGIN_MAX_FAILURES,
        lock_minutes=LOGIN_LOCK_MINUTES,
        ip_max_failures=LOGIN_IP_MAX_FAILURES,
    )


@app.post("/users/<int:user_id>/unlock")
@admin_required
def unlock_user(user_id):
    user = get_db().execute(
        "SELECT id, username FROM users WHERE id=?", (user_id,)
    ).fetchone()
    if user is None:
        abort(404)
    get_db().execute(
        """UPDATE users SET failed_login_count=0, last_failed_login_at=NULL,
           locked_until=NULL WHERE id=?""",
        (user_id,),
    )
    get_db().commit()
    audit("unlock", "user", user_id, user["username"])
    flash("Uporabniški račun je odklenjen.", "success")
    return redirect(url_for("security_view"))


@app.route("/users/new", methods=["GET", "POST"])
@admin_required
def new_user():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        full_name = request.form.get("full_name", "").strip()
        callsign = normalize_callsign(request.form.get("callsign", ""))
        password = request.form.get("password", "")
        role = request.form.get("role", "leader")
        if not username or not full_name or not callsign or role not in {"admin", "leader"}:
            flash("Izpolni vsa zahtevana polja.", "danger")
        elif not valid_callsign(callsign):
            flash(
                "Klicni znak lahko vsebuje samo črke, številke, / in - (2–24 znakov).",
                "danger",
            )
        elif not valid_password(password):
            flash("Geslo mora imeti od 15 do 128 znakov.", "danger")
        else:
            try:
                cur = get_db().execute(
                    """INSERT INTO users
                       (username, full_name, callsign, password_hash, role, active,
                        must_change_password, created_at)
                       VALUES (?, ?, ?, ?, ?, 1, 1, ?)""",
                    (username, full_name[:120], callsign[:24], generate_password_hash(password), role, now_db()),
                )
                get_db().commit()
                audit("create", "user", cur.lastrowid, username)
                flash("Uporabnik je ustvarjen.", "success")
                return redirect(url_for("users"))
            except sqlite3.IntegrityError:
                flash("To uporabniško ime že obstaja.", "danger")
    return render_template("user_form.html", edit_user=None)


@app.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_user(user_id):
    edit_user_row = get_db().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if edit_user_row is None:
        abort(404)
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        callsign = normalize_callsign(request.form.get("callsign", ""))
        role = request.form.get("role", "leader")
        active = 1 if request.form.get("active") == "1" else 0
        password = request.form.get("password", "")
        removes_active_admin = (
            edit_user_row["role"] == "admin"
            and edit_user_row["active"]
            and (role != "admin" or not active)
        )
        active_admin_count = get_db().execute(
            "SELECT COUNT(*) AS n FROM users WHERE role='admin' AND active=1"
        ).fetchone()["n"]
        if user_id == g.user["id"] and not active:
            flash("Svojega računa ne moreš onemogočiti.", "danger")
        elif removes_active_admin and active_admin_count <= 1:
            flash(
                "V portalu mora ostati vsaj en aktiven administrator.", "danger"
            )
        elif not full_name or not callsign or role not in {"admin", "leader"}:
            flash("Izpolni vsa zahtevana polja.", "danger")
        elif not valid_callsign(callsign):
            flash(
                "Klicni znak lahko vsebuje samo črke, številke, / in - (2–24 znakov).",
                "danger",
            )
        elif password and not valid_password(password):
            flash("Novo geslo mora imeti od 15 do 128 znakov.", "danger")
        else:
            if password:
                get_db().execute(
                    """UPDATE users SET full_name=?, callsign=?, role=?, active=?,
                       password_hash=?, must_change_password=1,
                       auth_version=auth_version+1
                       WHERE id=?""",
                    (full_name[:120], callsign[:24], role, active, generate_password_hash(password), user_id),
                )
            else:
                get_db().execute(
                    "UPDATE users SET full_name=?, callsign=?, role=?, active=? WHERE id=?",
                    (full_name[:120], callsign[:24], role, active, user_id),
                )
            get_db().commit()
            if password and user_id == g.user["id"]:
                refreshed = get_db().execute(
                    "SELECT auth_version FROM users WHERE id=?", (user_id,)
                ).fetchone()
                session["auth_version"] = refreshed["auth_version"]
                session["authenticated_at"] = int(time.time())
            audit("update", "user", user_id, edit_user_row["username"])
            flash("Uporabnik je posodobljen.", "success")
            return redirect(url_for("users"))
    return render_template("user_form.html", edit_user=edit_user_row)


@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if not check_password_hash(g.user["password_hash"], current):
            flash("Trenutno geslo ni pravilno.", "danger")
        elif not valid_password(new):
            flash("Novo geslo mora imeti od 15 do 128 znakov.", "danger")
        elif new != confirm:
            flash("Novi gesli se ne ujemata.", "danger")
        else:
            get_db().execute(
                """UPDATE users SET password_hash=?, must_change_password=0,
                   auth_version=auth_version+1
                   WHERE id=?""",
                (generate_password_hash(new), g.user["id"]),
            )
            get_db().commit()
            refreshed = get_db().execute(
                "SELECT auth_version FROM users WHERE id=?", (g.user["id"],)
            ).fetchone()
            session["auth_version"] = refreshed["auth_version"]
            session["authenticated_at"] = int(time.time())
            session.permanent = True
            audit("password_change", "user", g.user["id"], g.user["username"])
            flash("Geslo je spremenjeno.", "success")
            return redirect(url_for("dashboard"))
    return render_template("change_password.html")


@app.template_filter("date_si")
def date_si(value):
    if not value:
        return ""
    return datetime.strptime(value[:10], "%Y-%m-%d").strftime("%d. %m. %Y")


@app.template_filter("time_si")
def time_si(value):
    return value[11:16] if value and len(value) >= 16 else ""


@app.template_filter("datetime_si")
def datetime_si(value):
    if not value:
        return ""
    return datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S").strftime("%d. %m. %Y ob %H:%M")


@app.template_filter("audit_action_si")
def audit_action_si(value):
    return {
        "create": "Ustvarjeno",
        "update": "Spremenjeno",
        "delete": "Izbrisano",
        "close": "Zaključeno",
        "reopen": "Ponovno odprto",
        "learn": "Dodano v imenik",
        "password_change": "Sprememba gesla",
        "login_locked": "Blokirana prijava",
        "unlock": "Odklep računa",
        "update_notes": "Sprememba opombe",
        "merge": "Združitev",
        "restore": "Obnovljeno",
        "import": "Uvoženo",
        "undo": "Razveljavljeno",
    }.get(value, value)


@app.template_filter("audit_entity_si")
def audit_entity_si(value):
    return {
        "net": "Sked",
        "participant": "Prijava",
        "callsign": "Klicni znak",
        "user": "Uporabnik",
        "schedule_exception": "Sprememba urnika",
        "backup": "Varnostna kopija",
        "csv_import": "CSV-uvoz",
    }.get(value, value)


@app.template_filter("filesize_si")
def filesize_si(value):
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


@app.template_filter("duration_si")
def duration_si(value):
    seconds = max(0, int(value or 0))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    parts = []
    if days:
        parts.append(f"{days} d")
    if hours or days:
        parts.append(f"{hours} h")
    parts.append(f"{minutes} min")
    return " ".join(parts)


@app.template_filter("login_reason_si")
def login_reason_si(value):
    return {
        "success": "Uspešna prijava",
        "invalid_credentials": "Napačni podatki",
        "account_locked": "Zaklenjen račun",
        "ip_limited": "Omejen naslov IP",
    }.get(value, value)


TEMPLATES = {
"base.html": r'''<!doctype html>
<html lang="sl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{% block title %}{{ app_name }}{% endblock %}</title>
<style>
:root{--blue:#145da0;--blue2:#0d477d;--light:#eef5fb;--line:#d7e0e8;--danger:#b42318;--success:#117a43;--text:#17202a}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:var(--text);background:#f5f7f9}
header{background:linear-gradient(135deg,var(--blue2),var(--blue));color:white;box-shadow:0 2px 8px #0003}.nav{max-width:1100px;margin:auto;padding:14px 18px;display:flex;align-items:center;gap:18px;flex-wrap:wrap}.brand{font-weight:800;font-size:1.12rem;margin-right:auto}.nav a,.link-button{color:white;text-decoration:none;font-weight:650;background:none;border:0;padding:0;cursor:pointer;font:inherit}.user{font-size:.9rem;opacity:.9}
main{max-width:1100px;margin:24px auto;padding:0 16px}.card{background:white;border:1px solid var(--line);border-radius:14px;padding:20px;margin-bottom:18px;box-shadow:0 2px 10px #1020300c}.card h1,.card h2{margin-top:0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}.actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.schedule-box{border:1px solid var(--line);border-radius:12px;padding:16px;background:var(--light)}.schedule-box h2{margin:10px 0 8px}.canceled{background:#fee4e2;color:#8f1d14}.postponed{background:#fff1c7;color:#704b00}.exception-note{padding:11px;border-radius:9px;background:#fff8e5}.exception-login{padding:10px;border-radius:9px;background:#ffffff18}
.countdown-card{background:linear-gradient(135deg,var(--blue2),var(--blue));color:white}.countdown-card .muted{color:#dbeeff}.countdown-value{font-size:clamp(1.8rem,6vw,3.2rem);font-weight:850;letter-spacing:.03em;margin:8px 0}
.footer{max-width:1100px;margin:0 auto;padding:2px 16px 22px;text-align:center;color:#65717c;font-size:.85rem}.alpha-banner{background:#f5a800;color:#2b2100;padding:10px 16px;text-align:center;font-weight:850;letter-spacing:.03em}
label{display:block;font-weight:700;margin:0 0 6px}.field{margin-bottom:14px}input,select,textarea{width:100%;padding:11px 12px;border:1px solid #aebdca;border-radius:9px;background:white;font:inherit}input:focus,select:focus,textarea:focus{outline:3px solid #bddcff;border-color:var(--blue)}textarea{min-height:120px;resize:vertical}
.btn{display:inline-block;border:0;border-radius:9px;padding:10px 15px;font-weight:750;cursor:pointer;text-decoration:none;font:inherit}.btn-primary{background:var(--blue);color:white}.btn-primary:hover{background:var(--blue2)}.btn-secondary{background:#e6edf3;color:#1d2b36}.btn-danger{background:#fee4e2;color:var(--danger)}.btn-success{background:#daf5e6;color:#075f34}.btn-locked,.btn-locked:hover{background:#d7dde3;color:#66727c}.btn-small{padding:7px 10px;font-size:.9rem}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:11px 9px;border-bottom:1px solid var(--line)}th{background:var(--light);font-size:.88rem}tr:last-child td{border-bottom:0}.table-wrap{overflow-x:auto}.badge{display:inline-block;padding:4px 9px;border-radius:999px;font-size:.8rem;font-weight:750}.open{background:#d8f3e5;color:#075f34}.closed{background:#e7ebef;color:#45525d}.admin{background:#e5ddff;color:#4f2c90}.leader{background:#ddebfa;color:#164f7c}
.flash{padding:12px 14px;border-radius:9px;margin-bottom:14px;background:#e7edf2}.flash.success{background:#dff5e9;color:#075f34}.flash.danger{background:#fee4e2;color:#8f1d14}.flash.warning{background:#fff1c7;color:#704b00}.muted{color:#65717c}.big-number{font-size:2rem;font-weight:850}.login{max-width:430px;margin:6vh auto 18px}.login-schedule{max-width:620px;margin:0 auto 24px;text-align:center}.login-stats{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:18px;padding-top:16px;border-top:1px solid #ffffff55}.login-stat{padding:12px;border-radius:10px;background:#ffffff18}.login-stat span,.login-stat small{display:block}.login-stat strong{display:block;font-size:1.7rem;margin:4px 0}.login-stat small{color:#dbeeff}.login-history-title{margin:20px 0 -6px;font-weight:750}.login-history-count{font-weight:750;margin-top:5px}.edit-section{margin-top:28px;padding-top:22px;border-top:1px solid var(--line)}.danger-zone{margin-top:26px;padding-top:22px;border-top:2px solid #f2b8b5}.danger-zone h2{color:var(--danger)}.inline{display:inline}.right{margin-left:auto}.empty{text-align:center;padding:30px;color:#65717c}.nowrap{white-space:nowrap}
@media(max-width:700px){main{margin-top:14px}.card{padding:15px}.nav{gap:12px}.user{width:100%;order:3}th,td{padding:9px 6px}.hide-mobile{display:none}.btn{width:100%;text-align:center}.actions form{width:100%}.actions .btn-small{width:auto}.brand{width:100%}}
@media print{header,.no-print,.flash,.footer{display:none!important}body{background:white}main{max-width:none;margin:0;padding:0}.card{border:0;box-shadow:none;padding:0}table{font-size:11pt}a{color:black;text-decoration:none}}
</style></head><body>
{% if release_channel=='alpha' %}<div class="alpha-banner">⚠ ALPHA TESTNA RAZLIČICA · podatki niso produkcijski</div>{% endif %}
{% if g.user %}<header><nav class="nav"><span class="brand">📻 S50TTT Skedi</span><a href="{{ url_for('dashboard') }}">Domov</a><a href="{{ url_for('archive') }}">Arhiv</a><a href="{{ url_for('callsigns') }}">Imenik</a>{% if g.user['role']=='admin' %}<a href="{{ url_for('users') }}">Uporabniki</a>{% endif %}<span class="user">{{ g.user['full_name'] }} · {{ g.user['callsign'] }}</span><a href="{{ url_for('change_password') }}">Geslo</a><form class="inline" method="post" action="{{ url_for('logout') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><button class="link-button">Odjava</button></form></nav></header>{% endif %}
<main>{% with msgs=get_flashed_messages(with_categories=true) %}{% for category,msg in msgs %}<div class="flash {{ category }}">{{ msg }}</div>{% endfor %}{% endwith %}{% block content %}{% endblock %}</main>
<footer class="footer">{{ app_name }} · različica {{ app_version }}</footer>
</body></html>''',
"login.html": r'''{% extends "base.html" %}{% block title %}Prijava · {{ app_name }}{% endblock %}{% block content %}<div class="card login"><h1>📻 S50TTT</h1><h2>Dnevnik skedov</h2><p class="muted">Prijava za vodje skeda</p><form method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><div class="field"><label>Uporabniško ime</label><input name="username" autocomplete="username" required autofocus></div><div class="field"><label>Geslo</label><input type="password" name="password" autocomplete="current-password" required></div><button class="btn btn-primary" type="submit">Prijava</button></form></div>
<div class="card countdown-card login-schedule" data-countdown="{{ countdown_net['starts_at_iso'] }}"><p class="muted">Do naslednjega rednega skeda</p><div class="countdown-value" data-countdown-value>Izračunavam …</div><p><b>{{ countdown_net['label'] }}</b><br>{{ countdown_net['date']|date_si }} ob {{ countdown_net['time'] }}{% if countdown_net['repeater'] %} · {{ countdown_net['repeater'] }}{% endif %}</p><div class="login-stats"><div class="login-stat"><span>Redni sobotni sked</span><strong>št. {{ next_saturday['sequence_number'] }}</strong><small>{{ next_saturday['date']|date_si }} ob {{ next_saturday['time'] }}<br>{{ next_saturday['repeater'] }}</small></div><div class="login-stat" data-participant-count="{{ saturday_participant_count }}"><span>Prijavljenih</span><strong>{{ saturday_participant_count }}</strong><small>v dnevniku tega skeda</small></div></div>{% if recent_saturdays %}<p class="login-history-title">Zadnja zaključena sobotna skeda</p><div class="login-stats">{% for saturday in recent_saturdays %}<div class="login-stat" data-history-count="{{ saturday['participant_count'] }}"><span>Sobotni sked</span><strong>št. {{ saturday['sequence_number'] }}</strong><span class="login-history-count">{{ saturday['participant_count'] }} prijavljenih</span><small>{{ saturday['net_date']|date_si }}</small></div>{% endfor %}</div>{% endif %}</div>
<script>(function(){const card=document.querySelector('[data-countdown]');if(!card)return;const output=card.querySelector('[data-countdown-value]');const target=Date.parse(card.dataset.countdown);function pad(value){return String(value).padStart(2,'0')}function update(){const remaining=target-Date.now();if(remaining<=0){output.textContent='Sked se je začel';return}const total=Math.floor(remaining/1000);const days=Math.floor(total/86400);const hours=Math.floor((total%86400)/3600);const minutes=Math.floor((total%3600)/60);const seconds=total%60;output.textContent=(days?days+' dni · ':'')+pad(hours)+':'+pad(minutes)+':'+pad(seconds)}update();setInterval(update,1000)})();</script>{% endblock %}''',
"dashboard.html": r'''{% extends "base.html" %}{% block content %}
<div class="card"><h1>Naslednji redni skedi</h1><p class="muted">Portal samodejno upošteva mesečni in sezonski sobotni urnik Radiokluba Sevnica.</p><div class="grid">{% for s in scheduled_nets %}<div class="schedule-box"><span class="badge leader">{{ 'Mesečni' if s['schedule_type']=='monthly' else 'Sobotni' }}</span>{% if s['existing_status'] %} <span class="badge {{ s['existing_status'] }}">{{ 'Odprt' if s['existing_status']=='open' else 'Zaključen' }}</span>{% endif %}<h2>{{ s['label'] }}</h2><p><b>{{ s['date']|date_si }}</b> ob {{ s['time'] }}<br>Upravna postaja: <b>{{ s['control_callsign'] }}</b>{% if s['repeater'] %}<br>Repetitor: {{ s['repeater'] }}{% endif %}</p><p class="muted">{{ s['rule'] }}</p>{% if s['existing_id'] %}<a class="btn btn-primary" href="{{ url_for('net_detail',net_id=s['existing_id']) }}">Odpri obstoječi dnevnik</a>{% else %}<form method="post" action="{{ url_for('new_net') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><input type="hidden" name="schedule_type" value="{{ s['schedule_type'] }}"><input type="hidden" name="net_date" value="{{ s['date'] }}"><input type="hidden" name="started_time" value="{{ s['time'] }}">{% if s['can_open'] %}<button class="btn btn-primary">Odpri ta dnevnik</button>{% else %}<input type="hidden" name="early_unlock" value="0" data-early-unlock><button type="button" class="btn btn-locked" data-early-open aria-disabled="true">Odpri ta dnevnik</button>{% endif %}</form>{% endif %}</div>{% endfor %}</div></div>
<script>(function(){document.querySelectorAll('[data-early-open]').forEach(function(button){let presses=0;let resetTimer;const original=button.textContent;button.addEventListener('click',function(){presses+=1;clearTimeout(resetTimer);if(presses>=5){button.form.querySelector('[data-early-unlock]').value='1';alert('Ti si pravi Heker 😄');button.textContent='Odpiram …';button.form.requestSubmit();return}button.textContent='Še '+(5-presses)+'× pritisni';resetTimer=setTimeout(function(){presses=0;button.textContent=original},4000)})})})();</script>
<div class="card"><h2>Drug ali izredni sked</h2><p class="muted">Po potrebi odpri dnevnik z ročno izbranim datumom in uro.</p><form method="post" action="{{ url_for('new_net') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><div class="grid"><div class="field"><label>Naslov (neobvezno)</label><input name="title" placeholder="Samodejno: Sked DD. MM. LLLL"></div><div class="field"><label>Datum</label><input type="date" name="net_date" value="{{ now_local_value if now_local_value else '' }}" required></div><div class="field"><label>Začetna ura</label><input type="time" name="started_time" value="{{ current_time if current_time else '' }}" required></div></div><button class="btn btn-secondary">＋ Odpri izredni sked</button></form></div>
{% if open_nets %}<h2>Odprti skedi</h2><div class="grid">{% for n in open_nets %}<div class="card"><span class="badge open">Odprt</span><h2>{{ n['title'] }}</h2><p><b>{{ n['net_date']|date_si }}</b> ob {{ n['started_at']|time_si }}<br>Vodja: {{ n['leader_name'] }} ({{ n['leader_callsign'] }})</p><p><span class="big-number">{{ n['participant_count'] }}</span> prijavljenih</p><a class="btn btn-primary" href="{{ url_for('net_detail',net_id=n['id']) }}">Odpri dnevnik</a></div>{% endfor %}</div>{% endif %}
<div class="card"><div class="actions"><h2>Zadnji zaključeni skedi</h2><a class="btn btn-secondary right" href="{{ url_for('archive') }}">Celoten arhiv</a></div>{% if recent %}<div class="table-wrap"><table><thead><tr><th>Sked</th><th>Vodja</th><th>Prijavljeni</th><th></th></tr></thead><tbody>{% for n in recent %}<tr><td><b>{{ n['title'] }}</b><br><span class="muted">{{ n['net_date']|date_si }} ob {{ n['started_at']|time_si }}</span></td><td>{{ n['leader_callsign'] }}</td><td>{{ n['participant_count'] }}</td><td><a class="btn btn-secondary btn-small" href="{{ url_for('net_detail',net_id=n['id']) }}">Pregled</a></td></tr>{% endfor %}</tbody></table></div>{% else %}<p class="empty">V arhivu še ni skedov.</p>{% endif %}</div>
{% endblock %}''',
"net.html": r'''{% extends "base.html" %}{% block title %}{{ net['title'] }} · {{ app_name }}{% endblock %}{% block content %}{% set can_edit_participants=net['status']=='open' or g.user['role']=='admin' %}
<div class="card"><div class="actions"><div><span class="badge {{ net['status'] }}">{{ 'Odprt' if net['status']=='open' else 'Zaključen' }}</span>{% if net['schedule_type'] %} <span class="badge leader">Redni sked</span>{% endif %}<h1>{{ net['title'] }}</h1><p>{{ net['net_date']|date_si }} · začetek {{ net['started_at']|time_si }}{% if net['ended_at'] %} · konec {{ net['ended_at']|time_si }}{% endif %}{% if net['control_callsign'] %}<br>Upravna postaja: <b>{{ net['control_callsign'] }}</b>{% endif %}{% if net['repeater'] %}<br>Repetitor: <b>{{ net['repeater'] }}</b>{% endif %}<br>Operater: <b>{{ net['leader_name'] }} ({{ net['leader_callsign'] }})</b></p></div><div class="actions right no-print"><button class="btn btn-secondary" onclick="window.print()">🖨 Natisni / PDF</button>{% if net['status']=='open' %}{% if can_delete_net %}<form method="post" action="{{ url_for('delete_net',net_id=net['id']) }}" onsubmit="return confirm('Izbrišem ta prazen odprt sked? Tega dejanja ni mogoče razveljaviti.')"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><button class="btn btn-danger">Izbriši prazen sked</button></form>{% endif %}<form method="post" action="{{ url_for('close_net',net_id=net['id']) }}" onsubmit="return confirm('Zaključim ta sked?')"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><button class="btn btn-success">✓ Zaključi sked</button></form>{% elif g.user['role']=='admin' %}<a class="btn btn-primary" href="{{ url_for('edit_net',net_id=net['id']) }}">Uredi podatke</a><form method="post" action="{{ url_for('reopen_net',net_id=net['id']) }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><button class="btn btn-secondary">Ponovno odpri</button></form><a class="btn btn-danger" href="{{ url_for('delete_closed_net',net_id=net['id']) }}">Izbriši sked</a>{% endif %}</div></div></div>
{% if net['status']=='open' %}<div class="card no-print"><h2>Dodaj prijavljenega</h2><form method="post" action="{{ url_for('add_participant',net_id=net['id']) }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><div class="grid"><div class="field"><label>Klicni znak</label><input id="participant-callsign" name="callsign" list="callsign-options" required autofocus autocomplete="off" style="text-transform:uppercase"><datalist id="callsign-options">{% for entry in directory_entries %}<option value="{{ entry['callsign'] }}" data-full-name="{{ entry['full_name'] }}">{{ entry['full_name'] }}</option>{% endfor %}</datalist><small class="muted">Začni tipkati; znani klicni znak bo izpolnil ime.</small></div><div class="field"><label>Ime in priimek</label><input id="participant-full-name" name="full_name" required autocomplete="off"></div><div class="field"><label>Ura prijave</label><input type="time" name="checkin_time" value="{{ now.strftime('%H:%M') }}" required></div></div><button class="btn btn-primary">＋ Dodaj v dnevnik</button></form></div><script>(function(){const callsign=document.getElementById('participant-callsign');const fullName=document.getElementById('participant-full-name');const options=document.querySelectorAll('#callsign-options option');if(!callsign||!fullName)return;const directory={};options.forEach(function(option){directory[option.value.toUpperCase()]=option.dataset.fullName});let lastAutofill='';function suggest(){const value=callsign.value.trim().toUpperCase().replace(/\s+/g,'');callsign.value=value;const knownName=directory[value];if(knownName&&(!fullName.value||fullName.value===lastAutofill)){fullName.value=knownName;lastAutofill=knownName}}callsign.addEventListener('input',suggest);callsign.addEventListener('change',suggest)})();</script>{% endif %}
<div class="card"><h2>Prijavljeni: {{ participants|length }}</h2>{% if participants %}<div class="table-wrap"><table><thead><tr><th>Št.</th><th>Ura</th><th>Klicni znak</th><th>Ime in priimek</th><th class="no-print">Vnesel</th>{% if can_edit_participants %}<th class="no-print"></th>{% endif %}</tr></thead><tbody>{% for p in participants %}<tr><td>{{ loop.index }}</td><td class="nowrap">{{ p['checkin_at']|time_si }}</td><td><b>{{ p['callsign'] }}</b></td><td>{{ p['full_name'] }}</td><td class="no-print">{{ p['entered_by_name'] }}</td>{% if can_edit_participants %}<td class="no-print"><div class="actions"><a class="btn btn-secondary btn-small" href="{{ url_for('edit_participant',participant_id=p['id']) }}">Uredi</a><form method="post" action="{{ url_for('delete_participant',participant_id=p['id']) }}" onsubmit="return confirm('Izbrišem {{ p['callsign'] }}?')"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><button class="btn btn-danger btn-small">Izbriši</button></form></div></td>{% endif %}</tr>{% endfor %}</tbody></table></div>{% else %}<p class="empty">V tem skedu še ni prijavljenih.</p>{% endif %}</div>
{% endblock %}''',
"participant_edit.html": r'''{% extends "base.html" %}{% block content %}<div class="card"><h1>Uredi prijavo</h1><p>{{ net['title'] }}</p><form method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><input type="hidden" name="return_to" value="{{ return_to }}"><div class="field"><label>Ime in priimek</label><input name="full_name" value="{{ participant['full_name'] }}" required></div><div class="field"><label>Klicni znak</label><input name="callsign" value="{{ participant['callsign'] }}" required style="text-transform:uppercase"></div><div class="field"><label>Ura prijave</label><input type="time" name="checkin_time" value="{{ participant['checkin_at']|time_si }}" required></div><div class="actions"><button class="btn btn-primary">Shrani</button><a class="btn btn-secondary" href="{{ url_for('edit_net',net_id=net['id']) if return_to=='net_edit' else url_for('net_detail',net_id=net['id']) }}">Prekliči</a></div></form></div>{% endblock %}''',
"net_edit.html": r'''{% extends "base.html" %}{% block content %}<div class="card" style="max-width:900px"><h1>Uredi zaključeni sked</h1><p class="muted">Vsak popravek se zabeleži v revizijsko sled podatkovne baze.</p><form method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><div class="field"><label>Naslov skeda</label><input name="title" value="{{ net['title'] }}" required></div><div class="grid"><div class="field"><label>Datum</label><input type="date" name="net_date" value="{{ net['net_date'] }}" required></div><div class="field"><label>Začetna ura</label><input type="time" name="started_time" value="{{ net['started_at']|time_si }}" required></div><div class="field"><label>Končna ura</label><input type="time" name="ended_time" value="{{ net['ended_at']|time_si }}" required></div></div><div class="field"><label>Operater</label><select name="leader_id" required>{% for user in users %}<option value="{{ user['id'] }}" {% if user['id']==net['leader_id'] %}selected{% endif %}>{{ user['full_name'] }} ({{ user['callsign'] }}){% if not user['active'] %} – neaktiven{% endif %}</option>{% endfor %}</select></div><div class="actions"><button class="btn btn-primary">Shrani popravke</button><a class="btn btn-secondary" href="{{ url_for('net_detail',net_id=net['id']) }}">Prekliči</a></div></form>
<div class="edit-section"><h2>Dodaj prijavljenega</h2><form method="post" action="{{ url_for('add_participant',net_id=net['id']) }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><input type="hidden" name="return_to" value="net_edit"><div class="grid"><div class="field"><label>Klicni znak</label><input id="edit-participant-callsign" name="callsign" list="edit-callsign-options" required autocomplete="off" style="text-transform:uppercase"><datalist id="edit-callsign-options">{% for entry in directory_entries %}<option value="{{ entry['callsign'] }}" data-full-name="{{ entry['full_name'] }}">{{ entry['full_name'] }}</option>{% endfor %}</datalist></div><div class="field"><label>Ime in priimek</label><input id="edit-participant-full-name" name="full_name" required autocomplete="off"></div><div class="field"><label>Ura prijave</label><input type="time" name="checkin_time" value="{{ net['started_at']|time_si }}" required></div></div><button class="btn btn-primary">＋ Dodaj prijavljenega</button></form></div>
<div class="edit-section"><h2>Prijavljeni: {{ participants|length }}</h2>{% if participants %}<div class="table-wrap"><table><thead><tr><th>Ura</th><th>Klicni znak</th><th>Ime in priimek</th><th></th></tr></thead><tbody>{% for p in participants %}<tr><td>{{ p['checkin_at']|time_si }}</td><td><b>{{ p['callsign'] }}</b></td><td>{{ p['full_name'] }}</td><td><div class="actions"><a class="btn btn-secondary btn-small" href="{{ url_for('edit_participant',participant_id=p['id'],return_to='net_edit') }}">Uredi</a><form method="post" action="{{ url_for('delete_participant',participant_id=p['id']) }}" onsubmit="return confirm('Izbrišem {{ p['callsign'] }} iz zaključenega skeda?')"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><input type="hidden" name="return_to" value="net_edit"><button class="btn btn-danger btn-small">Izbriši</button></form></div></td></tr>{% endfor %}</tbody></table></div>{% else %}<p class="empty">V skedu ni prijavljenih.</p>{% endif %}</div>
<script>(function(){const callsign=document.getElementById('edit-participant-callsign');const fullName=document.getElementById('edit-participant-full-name');const options=document.querySelectorAll('#edit-callsign-options option');if(!callsign||!fullName)return;const directory={};options.forEach(function(option){directory[option.value.toUpperCase()]=option.dataset.fullName});let lastAutofill='';function suggest(){const value=callsign.value.trim().toUpperCase().replace(/\s+/g,'');callsign.value=value;const knownName=directory[value];if(knownName&&(!fullName.value||fullName.value===lastAutofill)){fullName.value=knownName;lastAutofill=knownName}}callsign.addEventListener('input',suggest);callsign.addEventListener('change',suggest)})();</script>
<div class="danger-zone"><h2>Izbris skeda</h2><p class="muted">Izbris je dovoljen samo z navedenim razlogom. Kopija skeda in vseh prijav ostane shranjena v revizijski tabeli.</p><form method="post" action="{{ url_for('delete_closed_net',net_id=net['id']) }}" onsubmit="return confirm('Res trajno izbrišem ta zaključeni sked?')"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><div class="field"><label>Razlog brisanja</label><textarea name="reason" minlength="10" maxlength="1000" required placeholder="Na primer: podvojen dnevnik, odprt za napačen datum …"></textarea><small class="muted">Najmanj 10 znakov.</small></div><button class="btn btn-danger">Trajno izbriši sked</button></form></div></div>{% endblock %}''',
"net_delete.html": r'''{% extends "base.html" %}{% block content %}<div class="card" style="max-width:680px"><h1>Izbriši zaključeni sked</h1><p><b>{{ net['title'] }}</b><br>{{ net['net_date']|date_si }} · {{ participant_count }} prijavljenih</p><div class="flash warning">Sked bo odstranjen iz arhiva. Razlog, podatki skeda in kopija vseh prijav bodo trajno shranjeni v revizijski tabeli.</div><form method="post" onsubmit="return confirm('Res izbrišem ta zaključeni sked?')"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><div class="field"><label>Razlog brisanja</label><textarea name="reason" minlength="10" maxlength="1000" required placeholder="Na primer: podvojen dnevnik, odprt za napačen datum …">{{ reason }}</textarea><small class="muted">Najmanj 10 znakov. Razlog se zabeleži skupaj z administratorjem in časom brisanja.</small></div><div class="actions"><button class="btn btn-danger">Trajno izbriši sked</button><a class="btn btn-secondary" href="{{ url_for('net_detail',net_id=net['id']) }}">Prekliči</a></div></form></div>{% endblock %}''',
"archive.html": r'''{% extends "base.html" %}{% block content %}<div class="card"><h1>Arhiv skedov</h1>{% if nets %}<div class="table-wrap"><table><thead><tr><th>Datum</th><th>Sked</th><th>Status</th><th>Operater</th><th>Prijavljeni</th><th></th></tr></thead><tbody>{% for n in nets %}<tr><td class="nowrap">{{ n['net_date']|date_si }}</td><td><b>{{ n['title'] }}</b><br><span class="muted">{{ n['started_at']|time_si }}{% if n['ended_at'] %}–{{ n['ended_at']|time_si }}{% endif %}{% if n['repeater'] %} · {{ n['repeater'] }}{% endif %}</span></td><td><span class="badge {{ n['status'] }}">{{ 'Odprt' if n['status']=='open' else 'Zaključen' }}</span></td><td>{{ n['leader_name'] }} ({{ n['leader_callsign'] }})</td><td>{{ n['participant_count'] }}</td><td><div class="actions"><a class="btn btn-secondary btn-small" href="{{ url_for('net_detail',net_id=n['id']) }}">Pregled</a>{% if g.user['role']=='admin' and n['status']=='closed' %}<a class="btn btn-primary btn-small" href="{{ url_for('edit_net',net_id=n['id']) }}">Uredi</a>{% endif %}</div></td></tr>{% endfor %}</tbody></table></div>{% else %}<p class="empty">Arhiv je prazen.</p>{% endif %}</div>{% endblock %}''',
"callsigns.html": r'''{% extends "base.html" %}{% block content %}<div class="card"><div class="actions"><div><h1>Imenik klicnih znakov</h1><p class="muted">Imenik se samodejno dopolnjuje z novimi prijavljenimi.</p></div>{% if g.user['role']=='admin' %}<a class="btn btn-primary right" href="{{ url_for('new_callsign') }}">＋ Novi vnos</a>{% endif %}</div><form method="get" class="actions no-print" style="margin-bottom:18px"><input name="q" value="{{ query }}" placeholder="Išči po klicnem znaku ali imenu" style="max-width:360px"><button class="btn btn-secondary">Išči</button>{% if query %}<a class="btn btn-secondary" href="{{ url_for('callsigns') }}">Počisti</a>{% endif %}</form>{% if entries %}<div class="table-wrap"><table><thead><tr><th>Klicni znak</th><th>Ime in priimek</th><th>Uporab</th><th>Zadnja prijava</th><th>Status</th>{% if g.user['role']=='admin' %}<th></th>{% endif %}</tr></thead><tbody>{% for entry in entries %}<tr><td><b>{{ entry['callsign'] }}</b></td><td>{{ entry['full_name'] }}</td><td>{{ entry['use_count'] }}</td><td>{{ entry['last_used_at']|datetime_si if entry['last_used_at'] else '–' }}</td><td><span class="badge {{ 'open' if entry['active'] else 'closed' }}">{{ 'Aktiven' if entry['active'] else 'Skrit' }}</span></td>{% if g.user['role']=='admin' %}<td><a class="btn btn-secondary btn-small" href="{{ url_for('edit_callsign',entry_id=entry['id']) }}">Uredi</a></td>{% endif %}</tr>{% endfor %}</tbody></table></div>{% else %}<p class="empty">V imeniku ni zadetkov.</p>{% endif %}</div>{% endblock %}''',
"callsign_form.html": r'''{% extends "base.html" %}{% block content %}<div class="card" style="max-width:620px"><h1>{{ 'Uredi vnos v imeniku' if entry else 'Novi vnos v imeniku' }}</h1><form method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><div class="field"><label>Klicni znak</label><input name="callsign" value="{{ entry['callsign'] if entry else '' }}" required autofocus style="text-transform:uppercase"></div><div class="field"><label>Ime in priimek</label><input name="full_name" value="{{ entry['full_name'] if entry else '' }}" required></div>{% if entry %}<div class="field"><label><input style="width:auto" type="checkbox" name="active" value="1" {% if entry['active'] %}checked{% endif %}> Aktiven vnos in prikaz med predlogi</label></div><p class="muted">Število uporab: {{ entry['use_count'] }}{% if entry['last_used_at'] %} · zadnja prijava {{ entry['last_used_at']|datetime_si }}{% endif %}</p>{% endif %}<div class="actions"><button class="btn btn-primary">Shrani</button><a class="btn btn-secondary" href="{{ url_for('callsigns') }}">Prekliči</a></div></form></div>{% endblock %}''',
"users.html": r'''{% extends "base.html" %}{% block content %}<div class="card"><div class="actions"><h1>Uporabniki</h1><a class="btn btn-primary right" href="{{ url_for('new_user') }}">＋ Novi uporabnik</a></div><div class="table-wrap"><table><thead><tr><th>Uporabnik</th><th>Ime</th><th>Klicni znak</th><th>Vloga</th><th>Status</th><th></th></tr></thead><tbody>{% for u in users %}<tr><td><b>{{ u['username'] }}</b></td><td>{{ u['full_name'] }}</td><td>{{ u['callsign'] }}</td><td><span class="badge {{ u['role'] }}">{{ 'Administrator' if u['role']=='admin' else 'Vodja skeda' }}</span></td><td>{{ 'Aktiven' if u['active'] else 'Onemogočen' }}</td><td><a class="btn btn-secondary btn-small" href="{{ url_for('edit_user',user_id=u['id']) }}">Uredi</a></td></tr>{% endfor %}</tbody></table></div></div>{% endblock %}''',
"user_form.html": r'''{% extends "base.html" %}{% block content %}<div class="card"><h1>{{ 'Uredi uporabnika' if edit_user else 'Novi uporabnik' }}</h1><form method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token }}">{% if not edit_user %}<div class="field"><label>Uporabniško ime</label><input name="username" required autocomplete="off"></div>{% else %}<p>Uporabniško ime: <b>{{ edit_user['username'] }}</b></p>{% endif %}<div class="grid"><div class="field"><label>Ime in priimek</label><input name="full_name" value="{{ edit_user['full_name'] if edit_user else '' }}" required></div><div class="field"><label>Klicni znak</label><input name="callsign" value="{{ edit_user['callsign'] if edit_user else '' }}" required style="text-transform:uppercase"></div><div class="field"><label>Vloga</label><select name="role"><option value="leader" {% if edit_user and edit_user['role']=='leader' %}selected{% endif %}>Vodja skeda</option><option value="admin" {% if edit_user and edit_user['role']=='admin' %}selected{% endif %}>Administrator</option></select></div></div><div class="field"><label>{{ 'Novo geslo (pusti prazno, če ga ne spreminjaš)' if edit_user else 'Začasno geslo (najmanj 10 znakov)' }}</label><input type="password" name="password" {% if not edit_user %}required{% endif %} autocomplete="new-password"></div>{% if edit_user %}<div class="field"><label><input style="width:auto" type="checkbox" name="active" value="1" {% if edit_user['active'] %}checked{% endif %}> Aktiven uporabnik</label></div>{% endif %}<div class="actions"><button class="btn btn-primary">Shrani</button><a class="btn btn-secondary" href="{{ url_for('users') }}">Prekliči</a></div></form></div>{% endblock %}''',
"change_password.html": r'''{% extends "base.html" %}{% block content %}<div class="card" style="max-width:520px"><h1>Spremeni geslo</h1><form method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><div class="field"><label>Trenutno geslo</label><input type="password" name="current_password" required></div><div class="field"><label>Novo geslo (najmanj 10 znakov)</label><input type="password" name="new_password" required></div><div class="field"><label>Ponovi novo geslo</label><input type="password" name="confirm_password" required></div><button class="btn btn-primary">Spremeni geslo</button></form></div>{% endblock %}'''
}

TEMPLATES.update({
"login_v2.html": r'''{% extends "base.html" %}{% block title %}Prijava · {{ app_name }}{% endblock %}{% block content %}
<div class="card login"><h1>📻 S50TTT</h1><h2>Dnevnik skedov</h2><p class="muted">Prijava za vodje skeda</p><form method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><div class="field"><label>Uporabniško ime</label><input name="username" autocomplete="username" required autofocus></div><div class="field"><label>Geslo</label><input type="password" name="password" autocomplete="current-password" required></div><button class="btn btn-primary" type="submit">Prijava</button></form></div>
<div class="card countdown-card login-schedule" data-countdown="{{ countdown_net['starts_at_iso'] }}"><p class="muted">Do naslednjega rednega skeda</p><div class="countdown-value" data-countdown-value>Izračunavam …</div><p><b>{{ countdown_net['label'] }}</b><br>{{ countdown_net['date']|date_si }} ob {{ countdown_net['time'] }}{% if countdown_net['repeater'] %} · {{ countdown_net['repeater'] }}{% endif %}</p>{% if countdown_net['exception_action']=='postponed' %}<p class="exception-login">Prestavljen s {{ countdown_net['original_date']|date_si }}. Razlog: {{ countdown_net['exception_reason'] }}</p>{% endif %}<div class="login-stats"><div class="login-stat"><span>Redni sobotni sked</span><strong>št. {{ next_saturday['sequence_number'] }}</strong><small>{{ next_saturday['date']|date_si }} ob {{ next_saturday['time'] }}<br>{{ next_saturday['repeater'] }}</small>{% if next_saturday['exception_action']=='postponed' %}<small>Prestavljen s {{ next_saturday['original_date']|date_si }}</small>{% endif %}</div><div class="login-stat" data-participant-count="{{ saturday_participant_count }}"><span>Prijavljenih</span><strong>{{ saturday_participant_count }}</strong><small>v dnevniku tega skeda</small></div></div>{% if recent_saturdays %}<p class="login-history-title">Zadnja zaključena sobotna skeda</p><div class="login-stats">{% for saturday in recent_saturdays %}<div class="login-stat" data-history-count="{{ saturday['participant_count'] }}"><span>Sobotni sked</span><strong>št. {{ saturday['sequence_number'] }}</strong><span class="login-history-count">{{ saturday['participant_count'] }} prijavljenih</span><small>{{ saturday['net_date']|date_si }}</small></div>{% endfor %}</div>{% endif %}</div>
<script>(function(){const card=document.querySelector('[data-countdown]');if(!card)return;const output=card.querySelector('[data-countdown-value]');const target=Date.parse(card.dataset.countdown);function pad(value){return String(value).padStart(2,'0')}function update(){const remaining=target-Date.now();if(remaining<=0){output.textContent='Sked se je začel';return}const total=Math.floor(remaining/1000);const days=Math.floor(total/86400);const hours=Math.floor((total%86400)/3600);const minutes=Math.floor((total%3600)/60);const seconds=total%60;output.textContent=(days?days+' dni · ':'')+pad(hours)+':'+pad(minutes)+':'+pad(seconds)}update();setInterval(update,1000)})();</script>{% endblock %}''',
"dashboard_v2.html": r'''{% extends "base.html" %}{% block content %}
<div class="card"><h1>Naslednji redni skedi</h1><p class="muted">Portal samodejno upošteva mesečni in sezonski sobotni urnik Radiokluba Sevnica.</p><div class="grid">{% for s in scheduled_nets %}<div class="schedule-box"><span class="badge leader">{{ 'Mesečni' if s['schedule_type']=='monthly' else 'Sobotni' }}</span>{% if s['exception_action']=='canceled' %} <span class="badge canceled">Odpovedan</span>{% elif s['exception_action']=='postponed' %} <span class="badge postponed">Prestavljen</span>{% endif %}{% if s['existing_status'] %} <span class="badge {{ s['existing_status'] }}">{{ 'Odprt' if s['existing_status']=='open' else 'Zaključen' }}</span>{% endif %}<h2>{{ s['label'] }}</h2><p><b>{{ s['date']|date_si }}</b> ob {{ s['time'] }}<br>Upravna postaja: <b>{{ s['control_callsign'] }}</b>{% if s['repeater'] %}<br>Repetitor: {{ s['repeater'] }}{% endif %}</p>{% if s['exception_action']=='postponed' %}<p class="exception-note"><b>Prvotni termin:</b> {{ s['original_date']|date_si }} ob {{ s['original_time'] }}<br><b>Razlog:</b> {{ s['exception_reason'] }}</p>{% elif s['exception_action']=='canceled' %}<p class="exception-note"><b>Razlog odpovedi:</b> {{ s['exception_reason'] }}</p>{% else %}<p class="muted">{{ s['rule'] }}</p>{% endif %}<div class="actions">{% if s['existing_id'] %}<a class="btn btn-primary" href="{{ url_for('net_detail',net_id=s['existing_id']) }}">Odpri obstoječi dnevnik</a>{% elif s['exception_action']!='canceled' %}<form method="post" action="{{ url_for('new_net') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><input type="hidden" name="schedule_type" value="{{ s['schedule_type'] }}"><input type="hidden" name="scheduled_date" value="{{ s['original_date'] }}"><input type="hidden" name="net_date" value="{{ s['date'] }}"><input type="hidden" name="started_time" value="{{ s['time'] }}">{% if s['can_open'] %}<button class="btn btn-primary">Odpri ta dnevnik</button>{% else %}<input type="hidden" name="early_unlock" value="0" data-early-unlock><button type="button" class="btn btn-locked" data-early-open aria-disabled="true">Odpri ta dnevnik</button>{% endif %}</form>{% endif %}{% if g.user['role']=='admin' and not s['existing_id'] %}<a class="btn btn-secondary" href="{{ url_for('schedule_exception',schedule_type=s['schedule_type'],scheduled_date=s['original_date']) }}">{{ 'Uredi spremembo' if s['exception_action'] else 'Odpovej ali prestavi' }}</a>{% endif %}</div></div>{% endfor %}</div></div>
<script>(function(){document.querySelectorAll('[data-early-open]').forEach(function(button){let presses=0;let resetTimer;const original=button.textContent;button.addEventListener('click',function(){presses+=1;clearTimeout(resetTimer);if(presses>=5){button.form.querySelector('[data-early-unlock]').value='1';alert('Ti si pravi Heker 😄');button.textContent='Odpiram …';button.form.requestSubmit();return}button.textContent='Še '+(5-presses)+'× pritisni';resetTimer=setTimeout(function(){presses=0;button.textContent=original},4000)})})})();</script>
<div class="card"><h2>Drug ali izredni sked</h2><p class="muted">Po potrebi odpri dnevnik z ročno izbranim datumom in uro.</p><form method="post" action="{{ url_for('new_net') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><div class="grid"><div class="field"><label>Naslov (neobvezno)</label><input name="title" placeholder="Samodejno: Sked DD. MM. LLLL"></div><div class="field"><label>Datum</label><input type="date" name="net_date" value="{{ now_local_value if now_local_value else '' }}" required></div><div class="field"><label>Začetna ura</label><input type="time" name="started_time" value="{{ current_time if current_time else '' }}" required></div></div><button class="btn btn-secondary">＋ Odpri izredni sked</button></form></div>
{% if open_nets %}<h2>Odprti skedi</h2><div class="grid">{% for n in open_nets %}<div class="card"><span class="badge open">Odprt</span><h2>{{ n['title'] }}</h2><p><b>{{ n['net_date']|date_si }}</b> ob {{ n['started_at']|time_si }}<br>Vodja: {{ n['leader_name'] }} ({{ n['leader_callsign'] }})</p><p><span class="big-number">{{ n['participant_count'] }}</span> prijavljenih</p><a class="btn btn-primary" href="{{ url_for('net_detail',net_id=n['id']) }}">Odpri dnevnik</a></div>{% endfor %}</div>{% endif %}
<div class="card"><div class="actions"><h2>Zadnji zaključeni skedi</h2><a class="btn btn-secondary right" href="{{ url_for('archive') }}">Celoten arhiv</a></div>{% if recent %}<div class="table-wrap"><table><thead><tr><th>Sked</th><th>Vodja</th><th>Prijavljeni</th><th></th></tr></thead><tbody>{% for n in recent %}<tr><td><b>{{ n['title'] }}</b><br><span class="muted">{{ n['net_date']|date_si }} ob {{ n['started_at']|time_si }}</span></td><td>{{ n['leader_callsign'] }}</td><td>{{ n['participant_count'] }}</td><td><a class="btn btn-secondary btn-small" href="{{ url_for('net_detail',net_id=n['id']) }}">Pregled</a></td></tr>{% endfor %}</tbody></table></div>{% else %}<p class="empty">V arhivu še ni skedov.</p>{% endif %}</div>{% endblock %}''',
"schedule_exception.html": r'''{% extends "base.html" %}{% block content %}<div class="card" style="max-width:680px"><h1>Odpovej ali prestavi redni sked</h1><p><b>{{ scheduled['label'] }}</b><br>Redni termin: {{ scheduled['date']|date_si }} ob {{ scheduled['time'] }}</p><p class="muted">Spremembo lahko naredi samo administrator. Razlog in vsaka sprememba se shranita v revizijsko sled.</p><form method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><div class="field"><label>Sprememba</label><select name="action" id="exception-action" required><option value="postponed" {% if exception and exception['action']=='postponed' %}selected{% endif %}>Prestavi na drug termin</option><option value="canceled" {% if exception and exception['action']=='canceled' %}selected{% endif %}>Odpovej sked</option></select></div><div id="postponed-fields" class="grid"><div class="field"><label>Novi datum</label><input type="date" name="new_date" value="{{ exception['new_date'] if exception and exception['new_date'] else scheduled['date'] }}"></div><div class="field"><label>Nova ura</label><input type="time" name="new_time" value="{{ exception['new_time'] if exception and exception['new_time'] else scheduled['time'] }}"></div></div><div class="field"><label>Razlog</label><textarea name="reason" minlength="10" maxlength="1000" required placeholder="Na primer: dogodek kluba, praznik, tehnične težave …">{{ exception['reason'] if exception else '' }}</textarea><small class="muted">Najmanj 10 znakov.</small></div><div class="actions"><button class="btn btn-primary">Shrani spremembo</button><a class="btn btn-secondary" href="{{ url_for('dashboard') }}">Prekliči</a></div></form>{% if exception %}<div class="danger-zone"><form method="post" action="{{ url_for('delete_schedule_exception',schedule_type=scheduled['schedule_type'],scheduled_date=scheduled['date']) }}" onsubmit="return confirm('Odstranim spremembo in povrnem običajni termin?')"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><button class="btn btn-danger">Razveljavi spremembo</button></form></div>{% endif %}</div><script>(function(){const action=document.getElementById('exception-action');const fields=document.getElementById('postponed-fields');function update(){fields.hidden=action.value!=='postponed';fields.querySelectorAll('input').forEach(function(input){input.required=action.value==='postponed'})}action.addEventListener('change',update);update()})();</script>{% endblock %}'''
})

TEMPLATES.update({
"statistics.html": r'''{% extends "base.html" %}{% block title %}Statistika · {{ app_name }}{% endblock %}{% block content %}
<div class="card"><div class="actions"><div><h1>Statistika skedov</h1><p class="muted">Pregled udeležbe za izbrano obdobje in vrsto skeda.</p></div>{% if g.user['role']=='admin' %}<div class="actions right no-print"><a class="btn btn-secondary" href="{{ url_for('export_csv') }}{% if export_query %}?{{ export_query }}{% endif %}">Izvozi CSV</a><a class="btn btn-secondary" href="{{ url_for('print_report') }}{% if export_query %}?{{ export_query }}{% endif %}" target="_blank">PDF / tiskanje</a></div>{% endif %}</div><form method="get" class="report-filters no-print"><div class="field"><label>Od datuma</label><input type="date" name="from_date" value="{{ filters['from_date'] }}"></div><div class="field"><label>Do datuma</label><input type="date" name="to_date" value="{{ filters['to_date'] }}"></div><div class="field"><label>Vrsta skeda</label><select name="schedule_type"><option value="all">Vse vrste</option><option value="saturday" {% if filters['schedule_type']=='saturday' %}selected{% endif %}>Sobotni</option><option value="monthly" {% if filters['schedule_type']=='monthly' %}selected{% endif %}>Mesečni</option><option value="other" {% if filters['schedule_type']=='other' %}selected{% endif %}>Izredni</option></select></div><div class="field"><label>Status</label><select name="status"><option value="all">Vsi statusi</option><option value="closed" {% if filters['status']=='closed' %}selected{% endif %}>Zaključeni</option><option value="open" {% if filters['status']=='open' %}selected{% endif %}>Odprti</option></select></div><button class="btn btn-primary">Prikaži</button><a class="btn btn-secondary" href="{{ url_for('statistics') }}">Počisti</a></form></div>
<div class="stats-grid"><div class="card stat"><span>Skedov</span><strong>{{ summary['net_count'] }}</strong></div><div class="card stat"><span>Vseh prijav</span><strong>{{ summary['participant_count'] }}</strong></div><div class="card stat"><span>Različnih klicnih znakov</span><strong>{{ summary['unique_callsigns'] }}</strong></div><div class="card stat"><span>Povprečno na sked</span><strong>{{ summary['average'] }}</strong></div></div>
<div class="grid"><div class="card"><h2>Udeležba po mesecih</h2>{% if monthly_rows %}<div class="bar-chart" role="img" aria-label="Število prijav po mesecih">{% for item in monthly_rows %}<div class="bar-row"><span>{{ item['month'] }}</span><div class="bar-track"><i style="width:{{ (item['participants'] * 100 / max_month_participants)|round }}%"></i></div><b>{{ item['participants'] }}</b><small>{{ item['nets'] }} skedov</small></div>{% endfor %}</div>{% else %}<p class="empty">Za izbrane filtre ni podatkov.</p>{% endif %}</div><div class="card"><h2>Najbolj redni sodelujoči</h2>{% if top_participants %}<div class="bar-chart" role="img" aria-label="Najbolj redni sodelujoči po številu prijav">{% for item in top_participants %}<div class="bar-row"><span><b>{{ item['callsign'] }}</b><br><small>{{ item['full_name'] }}</small></span><div class="bar-track"><i style="width:{{ (item['attendance_count'] * 100 / max_attendance)|round }}%"></i></div><b>{{ item['attendance_count'] }}</b></div>{% endfor %}</div>{% else %}<p class="empty">Za izbrane filtre ni prijavljenih.</p>{% endif %}</div></div>
<div class="card"><h2>Skedi po operaterjih</h2>{% if leaders %}<div class="table-wrap"><table><thead><tr><th>Operater</th><th>Klicni znak</th><th>Število skedov</th></tr></thead><tbody>{% for leader in leaders %}<tr><td>{{ leader['full_name'] }}</td><td><b>{{ leader['callsign'] }}</b></td><td>{{ leader['net_count'] }}</td></tr>{% endfor %}</tbody></table></div>{% else %}<p class="empty">Za izbrane filtre ni podatkov.</p>{% endif %}</div>{% endblock %}''',
"audit.html": r'''{% extends "base.html" %}{% block title %}Revizijska sled · {{ app_name }}{% endblock %}{% block content %}<div class="card"><h1>Revizijska sled</h1><p class="muted">Zabeležene spremembe uporabnikov, skedov, prijav, imenika in rednega urnika. Prikazanih je največ 100 zapisov na stran.</p><form method="get" class="report-filters"><div class="field"><label>Dejanje</label><select name="action"><option value="">Vsa dejanja</option>{% for item in actions %}<option value="{{ item['action'] }}" {% if selected_action==item['action'] %}selected{% endif %}>{{ item['action']|audit_action_si }}</option>{% endfor %}</select></div><div class="field"><label>Vrsta podatka</label><select name="entity_type"><option value="">Vse vrste</option>{% for item in entities %}<option value="{{ item['entity_type'] }}" {% if selected_entity==item['entity_type'] %}selected{% endif %}>{{ item['entity_type']|audit_entity_si }}</option>{% endfor %}</select></div><div class="field"><label>Uporabnik</label><select name="user_id"><option value="">Vsi uporabniki</option>{% for user in users %}<option value="{{ user['id'] }}" {% if selected_user==user['id'] %}selected{% endif %}>{{ user['full_name'] }} ({{ user['callsign'] }})</option>{% endfor %}</select></div><div class="field"><label>Od datuma</label><input type="date" name="from_date" value="{{ from_date }}"></div><div class="field"><label>Do datuma</label><input type="date" name="to_date" value="{{ to_date }}"></div><button class="btn btn-primary">Filtriraj</button><a class="btn btn-secondary" href="{{ url_for('audit_log_view') }}">Počisti</a></form></div><div class="card"><div class="actions"><h2>Zapisi: {{ total }}</h2><span class="muted right">Stran {{ page }}</span></div>{% if rows %}<div class="table-wrap"><table><thead><tr><th>Čas</th><th>Uporabnik</th><th>Dejanje</th><th>Podatek</th><th>Podrobnosti</th></tr></thead><tbody>{% for row in rows %}<tr><td class="nowrap">{{ row['created_at']|datetime_si }}</td><td>{{ row['user_name'] or 'Sistem' }}{% if row['user_callsign'] %}<br><b>{{ row['user_callsign'] }}</b>{% endif %}</td><td><span class="badge leader">{{ row['action']|audit_action_si }}</span></td><td>{{ row['entity_type']|audit_entity_si }}{% if row['entity_id'] %} #{{ row['entity_id'] }}{% endif %}</td><td>{% if row['details'] %}<details><summary>Prikaži</summary><code class="audit-details">{{ row['details'] }}</code></details>{% else %}–{% endif %}</td></tr>{% endfor %}</tbody></table></div><div class="actions no-print" style="margin-top:16px">{% if page>1 %}<a class="btn btn-secondary" href="{{ url_for('audit_log_view',page=page-1,action=selected_action,entity_type=selected_entity,user_id=selected_user or '',from_date=from_date,to_date=to_date) }}">← Prejšnja</a>{% endif %}{% if has_next %}<a class="btn btn-secondary right" href="{{ url_for('audit_log_view',page=page+1,action=selected_action,entity_type=selected_entity,user_id=selected_user or '',from_date=from_date,to_date=to_date) }}">Naslednja →</a>{% endif %}</div>{% else %}<p class="empty">Ni revizijskih zapisov za izbrane filtre.</p>{% endif %}</div>{% endblock %}''',
"print_report.html": r'''{% extends "base.html" %}{% block title %}Poročilo skedov · {{ app_name }}{% endblock %}{% block content %}<div class="card print-report"><div class="actions no-print"><button class="btn btn-primary" onclick="window.print()">Shrani kot PDF / natisni</button><button class="btn btn-secondary" onclick="window.close()">Zapri</button></div><h1>Poročilo skedov Radiokluba Sevnica S50TTT</h1><p class="muted">Izdelano: {{ generated_at|datetime_si }}{% if filters['from_date'] %} · od {{ filters['from_date']|date_si }}{% endif %}{% if filters['to_date'] %} · do {{ filters['to_date']|date_si }}{% endif %}</p>{% if rows %}<div class="table-wrap"><table><thead><tr><th>Datum</th><th>Sked</th><th>Status</th><th>Operater</th><th>Prijavljeni</th></tr></thead><tbody>{% for row in rows %}<tr><td class="nowrap">{{ row['net_date']|date_si }}</td><td><b>{{ row['title'] }}</b><br>{{ row['started_at']|time_si }}{% if row['ended_at'] %}–{{ row['ended_at']|time_si }}{% endif %}</td><td>{{ 'Zaključen' if row['status']=='closed' else 'Odprt' }}</td><td>{{ row['leader_name'] }} ({{ row['leader_callsign'] }})</td><td>{{ row['participant_count'] }}</td></tr>{% endfor %}</tbody><tfoot><tr><th colspan="4">Skupaj</th><th>{{ rows|sum(attribute='participant_count') }}</th></tr></tfoot></table></div>{% else %}<p class="empty">Za izbrane filtre ni podatkov.</p>{% endif %}</div>{% endblock %}'''
})

TEMPLATES["backups.html"] = r'''{% extends "base.html" %}{% block title %}Varnostne kopije · {{ app_name }}{% endblock %}{% block content %}<div class="card"><div class="actions"><div><h1>Varnostne kopije</h1><p class="muted">Portal ločeno ohranja preverjene dnevne, ročne in varnostne kopije.</p></div><form class="right" method="post" action="{{ url_for('create_backup_view') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><button class="btn btn-primary">Izdelaj kopijo zdaj</button></form></div></div><div class="card"><h2>Shranjene kopije: {{ backups|length }}</h2>{% if backups %}<div class="table-wrap"><table><thead><tr><th>Datoteka</th><th>Čas izdelave</th><th>Velikost</th><th></th></tr></thead><tbody>{% for backup in backups %}<tr><td><b>{{ backup['name'] }}</b>{% if backup['name'].startswith('auto-') %}<br><span class="muted">Samodejna</span>{% elif backup['name'].startswith('pre-restore-') %}<br><span class="muted">Pred obnovo</span>{% elif backup['name'].startswith('pre-import-') %}<br><span class="muted">Pred CSV-uvozom</span>{% else %}<br><span class="muted">Ročna</span>{% endif %}</td><td class="nowrap">{{ backup['modified_at']|datetime_si }}</td><td>{{ backup['size']|filesize_si }}</td><td><a class="btn btn-secondary btn-small" href="{{ url_for('download_backup',name=backup['name']) }}">Prenesi</a></td></tr>{% endfor %}</tbody></table></div>{% else %}<p class="empty">Varnostna kopija še ni bila izdelana.</p>{% endif %}</div><div class="card"><h2>Obnovitev baze</h2><div class="flash warning">Obnovitev namenoma ni mogoča med delovanjem portala. Najprej prenesi izbrano kopijo tudi na drugo napravo, nato portal in storitev za kopije ustavi ter uporabi dokumentirani ukaz na strežniku.</div><p class="muted">Pred obnovitvijo orodje preveri kopijo in samodejno izdela še varnostno kopijo trenutne baze.</p></div>{% endblock %}'''

TEMPLATES["csv_import.html"] = r'''{% extends "base.html" %}{% block title %}CSV-uvoz skedov · {{ app_name }}{% endblock %}{% block content %}<div class="card"><div class="actions"><div><h1>Uvoz starejših skedov iz CSV</h1><p class="muted">Najprej se prikaže predogled. Do potrditve se podatkovna baza ne spremeni.</p></div><a class="btn btn-secondary right" href="{{ url_for('csv_import_template') }}">Prenesi CSV-predlogo</a></div>{% if errors %}<div class="flash danger"><b>Uvoz vsebuje napake in ni bil shranjen.</b></div><div class="table-wrap"><table><thead><tr><th>Vrstica</th><th>Napaka</th></tr></thead><tbody>{% for error in errors %}<tr><td>{{ error['line'] or '–' }}</td><td>{{ error['message'] }}</td></tr>{% endfor %}</tbody></table></div>{% endif %}<form method="post" enctype="multipart/form-data"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><div class="field"><label>CSV-datoteka</label><input type="file" name="csv_file" accept=".csv,text/csv" required><small class="muted">Največ 1 MB in 3000 vrstic; kodiranje UTF-8.</small></div><button class="btn btn-primary">Preveri in prikaži predogled</button></form></div><div class="card"><h2>Oblika datoteke</h2><p>Obvezni stolpci so <code>datum</code>, <code>zacetek</code>, <code>operater</code>, <code>klicni_znak</code> in <code>ime</code>. Dodatni stolpci so <code>konec</code>, <code>naslov</code>, <code>vrsta</code>, <code>prijava</code> in <code>opombe</code>.</p><ul><li>Vsak prijavljeni je v svoji vrstici.</li><li>Vrstice istega skeda morajo imeti enak datum, čas, naslov, operaterja in opombo.</li><li>Za sked brez prijavljenih pusti klicni znak in ime prazna.</li><li>Operater mora že obstajati med uporabniki portala; uporabi njegovo uporabniško ime ali klicni znak.</li><li>Vrsta je <code>sobotni</code>, <code>mesecni</code> ali <code>izredni</code>.</li></ul></div>{% endblock %}'''

TEMPLATES["csv_import_preview.html"] = r'''{% extends "base.html" %}{% block title %}Predogled CSV-uvoza · {{ app_name }}{% endblock %}{% block content %}<div class="card"><span class="badge postponed">Predogled – baza še ni spremenjena</span><h1>Predogled CSV-uvoza</h1><p><b>{{ batch['filename'] }}</b><br>{{ parsed['net_count'] }} skedov · {{ parsed['participant_count'] }} prijavljenih · {{ parsed['input_row_count'] }} vrstic CSV</p><div class="flash warning">Ob potrditvi se najprej samodejno izdela preverjena varnostna kopija. Nato se celoten uvoz izvede naenkrat.</div><div class="actions"><form method="post" action="{{ url_for('confirm_csv_import',import_id=batch['id']) }}" onsubmit="return confirm('Uvozim vse prikazane skede in prijavljene?')"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><button class="btn btn-success">Potrdi celoten uvoz</button></form><form method="post" action="{{ url_for('cancel_csv_import',import_id=batch['id']) }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><button class="btn btn-secondary">Prekliči</button></form></div></div>{% for net in parsed['nets'] %}<div class="card"><div class="actions"><div><span class="badge leader">{{ 'Sobotni' if net['schedule_type']=='saturday' else ('Mesečni' if net['schedule_type']=='monthly' else 'Izredni') }}</span><h2>{{ net['title'] }}</h2><p>{{ net['net_date']|date_si }} · {{ net['started_at']|time_si }}{% if net['ended_at'] %}–{{ net['ended_at']|time_si }}{% endif %}<br>Operater: <b>{{ net['leader_label'] }}</b></p></div><span class="big-number right">{{ net['participants']|length }}</span></div>{% if net['notes'] %}<div class="net-notes muted">{{ net['notes'] }}</div>{% endif %}{% if net['participants'] %}<div class="table-wrap"><table><thead><tr><th>Vrstica</th><th>Prijava</th><th>Klicni znak</th><th>Ime</th></tr></thead><tbody>{% for participant in net['participants'] %}<tr><td>{{ participant['line'] }}</td><td>{{ participant['checkin_at']|time_si }}</td><td><b>{{ participant['callsign'] }}</b></td><td>{{ participant['full_name'] }}</td></tr>{% endfor %}</tbody></table></div>{% else %}<p class="empty">Brez prijavljenih.</p>{% endif %}</div>{% endfor %}{% endblock %}'''

TEMPLATES["users_v2.html"] = r'''{% extends "base.html" %}{% block content %}<div class="card"><div class="actions"><h1>Uporabniki</h1><a class="btn btn-primary right" href="{{ url_for('new_user') }}">＋ Novi uporabnik</a></div><div class="table-wrap"><table><thead><tr><th>Uporabnik</th><th>Ime in klicni znak</th><th>Vloga</th><th>Zadnja prijava</th><th>Varnost</th><th></th></tr></thead><tbody>{% for user in users %}<tr><td><b>{{ user['username'] }}</b><br>{{ 'Aktiven' if user['active'] else 'Onemogočen' }}</td><td>{{ user['full_name'] }}<br><b>{{ user['callsign'] }}</b></td><td><span class="badge {{ user['role'] }}">{{ 'Administrator' if user['role']=='admin' else 'Vodja skeda' }}</span></td><td>{% if user['last_login_at'] %}{{ user['last_login_at']|datetime_si }}{% if user['last_login_ip'] %}<br><span class="muted">IP: {{ user['last_login_ip'] }}</span>{% endif %}{% else %}–{% endif %}</td><td>{% if user['locked_until'] and user['locked_until']>now_value %}<span class="badge canceled">Zaklenjen</span><br><small>do {{ user['locked_until']|datetime_si }}</small>{% elif user['failed_login_count'] %}<span class="badge postponed">{{ user['failed_login_count'] }} napačnih poskusov</span>{% else %}<span class="badge open">V redu</span>{% endif %}</td><td><a class="btn btn-secondary btn-small" href="{{ url_for('edit_user',user_id=user['id']) }}">Uredi</a></td></tr>{% endfor %}</tbody></table></div></div>{% endblock %}'''

TEMPLATES["security.html"] = r'''{% extends "base.html" %}{% block title %}Varnost prijav · {{ app_name }}{% endblock %}{% block content %}<div class="card"><h1>Varnost prijav</h1><p class="muted">Po {{ max_failures }} napačnih geslih se račun zaklene za {{ lock_minutes }} minut. Po {{ ip_max_failures }} neuspešnih poskusih z istega naslova v {{ lock_minutes }} minutah se začasno omeji tudi ta naslov.</p><div class="table-wrap"><table><thead><tr><th>Uporabnik</th><th>Zadnja uspešna prijava</th><th>Napačni poskusi</th><th>Status</th><th></th></tr></thead><tbody>{% for account in accounts %}<tr><td>{{ account['full_name'] }}<br><b>{{ account['callsign'] }}</b> · {{ account['username'] }}</td><td>{% if account['last_login_at'] %}{{ account['last_login_at']|datetime_si }}<br><span class="muted">{{ account['last_login_ip'] or '' }}</span>{% else %}–{% endif %}</td><td>{{ account['failed_login_count'] }}</td><td>{% if account['locked_until'] and account['locked_until']>now_value %}<span class="badge canceled">Zaklenjen do {{ account['locked_until']|datetime_si }}</span>{% elif account['active'] %}<span class="badge open">Aktiven</span>{% else %}<span class="badge closed">Onemogočen</span>{% endif %}</td><td>{% if account['locked_until'] or account['failed_login_count'] %}<form method="post" action="{{ url_for('unlock_user',user_id=account['id']) }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><button class="btn btn-secondary btn-small">Odkleni</button></form>{% endif %}</td></tr>{% endfor %}</tbody></table></div></div><div class="card"><h2>Zadnjih 200 poskusov prijave</h2><form method="get" class="actions" style="margin-bottom:16px"><input name="q" value="{{ query }}" placeholder="Uporabniško ime ali IP" style="max-width:300px"><select name="result" style="max-width:220px"><option value="all">Vsi poskusi</option><option value="success" {% if selected_result=='success' %}selected{% endif %}>Uspešni</option><option value="failed" {% if selected_result=='failed' %}selected{% endif %}>Neuspešni</option><option value="limited" {% if selected_result=='limited' %}selected{% endif %}>Blokirani</option></select><button class="btn btn-primary">Filtriraj</button><a class="btn btn-secondary" href="{{ url_for('security_view') }}">Počisti</a></form>{% if attempts %}<div class="table-wrap"><table><thead><tr><th>Čas</th><th>Vpisano uporabniško ime</th><th>Naslov IP</th><th>Rezultat</th></tr></thead><tbody>{% for attempt in attempts %}<tr><td class="nowrap">{{ attempt['created_at']|datetime_si }}</td><td>{{ attempt['username'] }}{% if attempt['user_callsign'] %}<br><b>{{ attempt['user_callsign'] }}</b>{% endif %}</td><td>{{ attempt['ip_address'] }}</td><td><span class="badge {{ 'open' if attempt['success'] else 'canceled' }}">{{ attempt['reason']|login_reason_si }}</span></td></tr>{% endfor %}</tbody></table></div>{% else %}<p class="empty">Poskusov prijave še ni.</p>{% endif %}</div>{% endblock %}'''

TEMPLATES["archive_v2.html"] = r'''{% extends "base.html" %}{% block content %}<div class="card"><div class="actions"><div><h1>Arhiv skedov</h1><p class="muted">Pregled odprtih in zaključenih dnevnikov.</p></div>{% if g.user['role']=='admin' %}<a class="btn btn-secondary right" href="{{ url_for('deleted_nets') }}">Koš izbrisanih skedov</a>{% endif %}</div>{% if nets %}<div class="table-wrap"><table><thead><tr><th>Datum</th><th>Sked</th><th>Status</th><th>Operater</th><th>Prijavljeni</th><th></th></tr></thead><tbody>{% for n in nets %}<tr><td class="nowrap">{{ n['net_date']|date_si }}</td><td><b>{{ n['title'] }}</b><br><span class="muted">{{ n['started_at']|time_si }}{% if n['ended_at'] %}–{{ n['ended_at']|time_si }}{% endif %}{% if n['repeater'] %} · {{ n['repeater'] }}{% endif %}</span></td><td><span class="badge {{ n['status'] }}">{{ 'Odprt' if n['status']=='open' else 'Zaključen' }}</span></td><td>{{ n['leader_name'] }} ({{ n['leader_callsign'] }})</td><td>{{ n['participant_count'] }}</td><td><div class="actions"><a class="btn btn-secondary btn-small" href="{{ url_for('net_detail',net_id=n['id']) }}">Pregled</a>{% if g.user['role']=='admin' and n['status']=='closed' %}<a class="btn btn-primary btn-small" href="{{ url_for('edit_net',net_id=n['id']) }}">Uredi</a>{% endif %}</div></td></tr>{% endfor %}</tbody></table></div>{% else %}<p class="empty">Arhiv je prazen.</p>{% endif %}</div>{% endblock %}'''

TEMPLATES["archive_v3.html"] = r'''{% extends "base.html" %}{% block content %}<div class="card"><div class="actions"><div><h1>Arhiv skedov</h1><p class="muted">Poišči dnevnik po naslovu, operaterju, imenu ali klicnem znaku prijavljenega.</p></div>{% if g.user['role']=='admin' %}<a class="btn btn-secondary right" href="{{ url_for('deleted_nets') }}">Koš izbrisanih skedov</a>{% endif %}</div><form method="get" class="report-filters"><div class="field"><label>Iskanje</label><input name="q" value="{{ query }}" maxlength="100" placeholder="Naslov, ime ali klicni znak"></div><div class="field"><label>Od datuma</label><input type="date" name="from_date" value="{{ filters['from_date'] }}"></div><div class="field"><label>Do datuma</label><input type="date" name="to_date" value="{{ filters['to_date'] }}"></div><div class="field"><label>Vrsta skeda</label><select name="schedule_type"><option value="all">Vse vrste</option><option value="saturday" {% if filters['schedule_type']=='saturday' %}selected{% endif %}>Sobotni</option><option value="monthly" {% if filters['schedule_type']=='monthly' %}selected{% endif %}>Mesečni</option><option value="other" {% if filters['schedule_type']=='other' %}selected{% endif %}>Izredni</option></select></div><div class="field"><label>Status</label><select name="status"><option value="all">Vsi statusi</option><option value="closed" {% if filters['status']=='closed' %}selected{% endif %}>Zaključeni</option><option value="open" {% if filters['status']=='open' %}selected{% endif %}>Odprti</option></select></div><button class="btn btn-primary">Poišči</button><a class="btn btn-secondary" href="{{ url_for('archive') }}">Počisti</a></form></div><div class="card"><div class="actions"><h2>Najdenih skedov: {{ total }}</h2><span class="muted right">Stran {{ page }} od {{ page_count }}</span></div>{% if nets %}<div class="table-wrap"><table><thead><tr><th>Datum</th><th>Sked</th><th>Vrsta</th><th>Status</th><th>Operater</th><th>Prijavljeni</th><th></th></tr></thead><tbody>{% for n in nets %}<tr><td class="nowrap">{{ n['net_date']|date_si }}</td><td><b>{{ n['title'] }}</b><br><span class="muted">{{ n['started_at']|time_si }}{% if n['ended_at'] %}–{{ n['ended_at']|time_si }}{% endif %}{% if n['repeater'] %} · {{ n['repeater'] }}{% endif %}</span></td><td>{% if n['schedule_type']=='saturday' %}Sobotni{% elif n['schedule_type']=='monthly' %}Mesečni{% else %}Izredni{% endif %}</td><td><span class="badge {{ n['status'] }}">{{ 'Odprt' if n['status']=='open' else 'Zaključen' }}</span></td><td>{{ n['leader_name'] }} ({{ n['leader_callsign'] }})</td><td>{{ n['participant_count'] }}</td><td><div class="actions"><a class="btn btn-secondary btn-small" href="{{ url_for('net_detail',net_id=n['id']) }}">Pregled</a>{% if g.user['role']=='admin' and n['status']=='closed' %}<a class="btn btn-primary btn-small" href="{{ url_for('edit_net',net_id=n['id']) }}">Uredi</a>{% endif %}</div></td></tr>{% endfor %}</tbody></table></div><div class="actions no-print" style="margin-top:16px">{% if has_previous %}<a class="btn btn-secondary" href="{{ url_for('archive',page=page-1,q=query,from_date=filters['from_date'],to_date=filters['to_date'],schedule_type=filters['schedule_type'],status=filters['status']) }}">← Prejšnja</a>{% endif %}{% if has_next %}<a class="btn btn-secondary right" href="{{ url_for('archive',page=page+1,q=query,from_date=filters['from_date'],to_date=filters['to_date'],schedule_type=filters['schedule_type'],status=filters['status']) }}">Naslednja →</a>{% endif %}</div>{% else %}<p class="empty">Za izbrane pogoje ni skedov.</p>{% endif %}</div>{% endblock %}'''

TEMPLATES["deleted_nets.html"] = r'''{% extends "base.html" %}{% block title %}Koš izbrisanih skedov · {{ app_name }}{% endblock %}{% block content %}<div class="card"><div class="actions"><div><h1>Koš izbrisanih skedov</h1><p class="muted">Administrator lahko pregleda razlog in shranjeno kopijo ter pomotoma izbrisani sked obnovi.</p></div><a class="btn btn-secondary right" href="{{ url_for('archive') }}">Nazaj v arhiv</a></div>{% if deletions %}<div class="table-wrap"><table><thead><tr><th>Izbris</th><th>Sked</th><th>Prijavljeni</th><th>Razlog</th><th>Status</th><th></th></tr></thead><tbody>{% for item in deletions %}<tr><td class="nowrap">{{ item['deleted_at']|datetime_si }}<br><span class="muted">{{ item['deleted_by_name'] }} ({{ item['deleted_by_callsign'] }})</span></td><td><b>{{ item['title'] }}</b><br>{{ item['net_date']|date_si }}</td><td>{{ item['participant_count'] }}</td><td>{{ item['reason'] }}</td><td>{% if item['restored_at'] %}<span class="badge open">Obnovljen</span><br><small>{{ item['restored_at']|datetime_si }}</small>{% else %}<span class="badge closed">V košu</span>{% endif %}</td><td><a class="btn btn-secondary btn-small" href="{{ url_for('deleted_net_detail',deletion_id=item['id']) }}">Podrobnosti</a></td></tr>{% endfor %}</tbody></table></div>{% else %}<p class="empty">Koš je prazen.</p>{% endif %}</div>{% endblock %}'''

TEMPLATES["deleted_net_detail.html"] = r'''{% extends "base.html" %}{% block title %}Izbrisani sked · {{ app_name }}{% endblock %}{% block content %}<div class="card"><div class="actions"><div><span class="badge {{ 'open' if deletion['restored_at'] else 'closed' }}">{{ 'Obnovljen' if deletion['restored_at'] else 'V košu' }}</span><h1>{{ deletion['title'] }}</h1><p>{{ deletion['net_date']|date_si }} · {{ deletion['participant_count'] }} prijavljenih</p></div><a class="btn btn-secondary right" href="{{ url_for('deleted_nets') }}">Nazaj v koš</a></div><h2>Razlog brisanja</h2><div class="flash warning">{{ deletion['reason'] }}</div><p class="muted">Izbris izvedel {{ deletion['deleted_by_name'] }} ({{ deletion['deleted_by_callsign'] }}) · {{ deletion['deleted_at']|datetime_si }}</p>{% if deletion['restored_at'] %}<p><b>Obnovil:</b> {{ deletion['restored_by_name'] }} ({{ deletion['restored_by_callsign'] }}) · {{ deletion['restored_at']|datetime_si }}</p>{% if restored_exists %}<a class="btn btn-primary" href="{{ url_for('net_detail',net_id=deletion['restored_net_id']) }}">Odpri obnovljeni sked</a>{% endif %}{% elif snapshot %}<form method="post" action="{{ url_for('restore_deleted_net',deletion_id=deletion['id']) }}" onsubmit="return confirm('Obnovim ta sked in vse prijavljene v arhiv?')"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><button class="btn btn-success">Obnovi sked in prijavljene</button></form>{% else %}<div class="flash danger">Shranjene kopije ni mogoče prebrati, zato obnova ni na voljo.</div>{% endif %}</div>{% if snapshot %}<div class="card"><h2>Shranjeni podatki</h2><p><b>Začetek:</b> {{ snapshot['net']['started_at']|datetime_si }}{% if snapshot['net']['ended_at'] %}<br><b>Konec:</b> {{ snapshot['net']['ended_at']|datetime_si }}{% endif %}<br><b>Operater:</b> {{ snapshot['net']['leader_name'] }} ({{ snapshot['net']['leader_callsign'] }}){% if snapshot['net']['repeater'] %}<br><b>Repetitor:</b> {{ snapshot['net']['repeater'] }}{% endif %}</p><h2>Prijavljeni: {{ snapshot['participants']|length }}</h2>{% if snapshot['participants'] %}<div class="table-wrap"><table><thead><tr><th>Ura</th><th>Klicni znak</th><th>Ime in priimek</th></tr></thead><tbody>{% for participant in snapshot['participants'] %}<tr><td>{{ participant['checkin_at']|time_si }}</td><td><b>{{ participant['callsign'] }}</b></td><td>{{ participant['full_name'] }}</td></tr>{% endfor %}</tbody></table></div>{% else %}<p class="empty">Sked ni imel prijavljenih.</p>{% endif %}</div>{% endif %}{% endblock %}'''

TEMPLATES["net_v2.html"] = TEMPLATES["net.html"].replace(
    '''{% if net['status']=='open' %}<div class="card no-print"><h2>Dodaj prijavljenega</h2>''',
    '''{% if net['notes'] or can_edit_notes %}<div class="card"><h2>Zapisnik / opombe skeda</h2>{% if can_edit_notes %}<form class="notes-editor no-print" method="post" action="{{ url_for('update_net_notes',net_id=net['id']) }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><textarea name="notes" maxlength="5000" placeholder="Obvestila kluba, tehnične težave, posebnosti skeda …">{{ net['notes'] or '' }}</textarea><div class="actions" style="margin-top:10px"><button class="btn btn-primary">Shrani zapisnik</button><span class="muted">Največ 5000 znakov</span></div></form>{% if net['notes'] %}<div class="net-notes notes-preview">{{ net['notes'] }}</div>{% endif %}{% else %}<div class="net-notes">{{ net['notes'] }}</div>{% endif %}</div>{% endif %}{% if net['status']=='open' %}<div class="card no-print"><h2>Dodaj prijavljenega</h2>''',
)

TEMPLATES["net_edit_v2.html"] = TEMPLATES["net_edit.html"].replace(
    '''</select></div><div class="actions"><button class="btn btn-primary">Shrani popravke</button>''',
    '''</select></div><div class="field"><label>Zapisnik / opombe skeda</label><textarea name="notes" maxlength="5000" placeholder="Obvestila kluba, tehnične težave, posebnosti skeda …">{{ net['notes'] or '' }}</textarea><small class="muted">Opomba ostane vidna v arhivu in na natisnjenem dnevniku.</small></div><div class="actions"><button class="btn btn-primary">Shrani popravke</button>''',
)

TEMPLATES["deleted_net_detail.html"] = TEMPLATES["deleted_net_detail.html"].replace(
    '''</p><h2>Prijavljeni: {{ snapshot['participants']|length }}</h2>''',
    '''</p>{% if snapshot['net']['notes'] %}<h2>Zapisnik / opombe skeda</h2><div class="net-notes">{{ snapshot['net']['notes'] }}</div>{% endif %}<h2>Prijavljeni: {{ snapshot['participants']|length }}</h2>''',
)

TEMPLATES["print_report.html"] = TEMPLATES["print_report.html"].replace(
    '''{% if row['ended_at'] %}–{{ row['ended_at']|time_si }}{% endif %}</td><td>{{ 'Zaključen' if row['status']=='closed' else 'Odprt' }}''',
    '''{% if row['ended_at'] %}–{{ row['ended_at']|time_si }}{% endif %}{% if row['notes'] %}<div class="net-notes muted">{{ row['notes'] }}</div>{% endif %}</td><td>{{ 'Zaključen' if row['status']=='closed' else 'Odprt' }}''',
)

TEMPLATES["callsigns_v2.html"] = r'''{% extends "base.html" %}{% block content %}<div class="card"><div class="actions"><div><h1>Imenik klicnih znakov</h1><p class="muted">Izberi klicni znak za prikaz zgodovine sodelovanja.</p></div>{% if g.user['role']=='admin' %}<a class="btn btn-primary right" href="{{ url_for('new_callsign') }}">＋ Novi vnos</a>{% endif %}</div><form method="get" class="actions no-print" style="margin-bottom:18px"><input name="q" value="{{ query }}" placeholder="Išči po klicnem znaku ali imenu" style="max-width:360px"><button class="btn btn-secondary">Išči</button>{% if query %}<a class="btn btn-secondary" href="{{ url_for('callsigns') }}">Počisti</a>{% endif %}</form>{% if entries %}<div class="table-wrap"><table><thead><tr><th>Klicni znak</th><th>Ime in priimek</th><th>Sodelovanj</th><th>Zadnja prijava</th><th>Status</th><th></th></tr></thead><tbody>{% for entry in entries %}<tr><td><a href="{{ url_for('callsign_profile',entry_id=entry['id']) }}"><b>{{ entry['callsign'] }}</b></a></td><td>{{ entry['full_name'] }}</td><td>{{ entry['use_count'] }}</td><td>{{ entry['last_used_at']|datetime_si if entry['last_used_at'] else '–' }}</td><td><span class="badge {{ 'open' if entry['active'] else 'closed' }}">{{ 'Aktiven' if entry['active'] else 'Skrit' }}</span></td><td><div class="actions"><a class="btn btn-secondary btn-small" href="{{ url_for('callsign_profile',entry_id=entry['id']) }}">Profil</a>{% if g.user['role']=='admin' %}<a class="btn btn-secondary btn-small" href="{{ url_for('edit_callsign',entry_id=entry['id']) }}">Uredi</a>{% endif %}</div></td></tr>{% endfor %}</tbody></table></div>{% else %}<p class="empty">V imeniku ni zadetkov.</p>{% endif %}</div>{% endblock %}'''

TEMPLATES["callsign_profile.html"] = r'''{% extends "base.html" %}{% block title %}{{ entry['callsign'] }} · {{ app_name }}{% endblock %}{% block content %}<div class="card"><div class="actions"><div><span class="badge {{ 'open' if entry['active'] else 'closed' }}">{{ 'Aktiven' if entry['active'] else 'Skrit' }}</span><h1>{{ entry['callsign'] }}</h1><h2>{{ entry['full_name'] }}</h2></div>{% if g.user['role']=='admin' %}<div class="actions right"><a class="btn btn-secondary" href="{{ url_for('edit_callsign',entry_id=entry['id']) }}">Uredi podatke</a><a class="btn btn-danger" href="{{ url_for('merge_callsign',entry_id=entry['id']) }}">Združi klicni znak</a></div>{% endif %}</div></div><div class="stats-grid"><div class="card stat"><span>Sodelovanj</span><strong>{{ summary['attendance_count'] }}</strong></div><div class="card stat"><span>Aktivnih let</span><strong>{{ summary['years_count'] }}</strong></div><div class="card stat"><span>Prvo sodelovanje</span><strong class="profile-date">{{ summary['first_checkin']|date_si if summary['first_checkin'] else '–' }}</strong></div><div class="card stat"><span>Zadnje sodelovanje</span><strong class="profile-date">{{ summary['last_checkin']|date_si if summary['last_checkin'] else '–' }}</strong></div></div>{% if g.user['role']=='admin' %}<div class="card"><h2>Interna opomba</h2><p class="muted">Opomba je vidna samo administratorjem in se zabeleži v revizijsko sled.</p><form method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><textarea name="notes" maxlength="2000" placeholder="Neobvezna interna opomba …">{{ entry['notes'] or '' }}</textarea><button class="btn btn-primary" style="margin-top:10px">Shrani opombo</button></form></div>{% endif %}<div class="grid"><div class="card"><h2>Sodelovanja po letih</h2>{% if yearly %}<div class="table-wrap"><table><thead><tr><th>Leto</th><th>Sodelovanj</th></tr></thead><tbody>{% for item in yearly %}<tr><td>{{ item['year'] }}</td><td><b>{{ item['attendance_count'] }}</b></td></tr>{% endfor %}</tbody></table></div>{% else %}<p class="empty">Sodelovanj še ni.</p>{% endif %}</div><div class="card"><h2>Podatki imenika</h2><p>Vnos ustvarjen: <b>{{ entry['created_at']|datetime_si }}</b>{% if entry['updated_at'] %}<br>Zadnja sprememba: <b>{{ entry['updated_at']|datetime_si }}</b>{% endif %}</p><a class="btn btn-secondary" href="{{ url_for('callsigns') }}">Nazaj v imenik</a></div></div><div class="card"><h2>Zgodovina skedov</h2>{% if history %}<div class="table-wrap"><table><thead><tr><th>Datum</th><th>Sked</th><th>Prijava</th><th>Operater</th><th></th></tr></thead><tbody>{% for item in history %}<tr><td class="nowrap">{{ item['net_date']|date_si }}</td><td><b>{{ item['title'] }}</b><br><span class="badge {{ item['status'] }}">{{ 'Zaključen' if item['status']=='closed' else 'Odprt' }}</span></td><td>{{ item['checkin_at']|time_si }}</td><td>{{ item['leader_name'] }} ({{ item['leader_callsign'] }})</td><td><a class="btn btn-secondary btn-small" href="{{ url_for('net_detail',net_id=item['net_id']) }}">Pregled</a></td></tr>{% endfor %}</tbody></table></div>{% else %}<p class="empty">Ta klicni znak še ni sodeloval v shranjenem skedu.</p>{% endif %}</div>{% endblock %}'''

TEMPLATES["callsign_merge.html"] = r'''{% extends "base.html" %}{% block content %}<div class="card" style="max-width:680px"><h1>Združi klicni znak</h1><p>Vir: <b>{{ source['callsign'] }}</b> · {{ source['full_name'] }}</p><div class="flash warning">Vsa sodelovanja vira bodo prenesena na izbrani ciljni klicni znak, vir pa bo odstranjen iz imenika. Če sta oba znaka v istem skedu, se podvojena prijava odstrani. Dejanje se zabeleži v revizijo.</div>{% if targets %}<form method="post" onsubmit="return confirm('Res združim {{ source['callsign'] }} z izbranim klicnim znakom?')"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><div class="field"><label>Ciljni klicni znak</label><select name="target_id" required><option value="">Izberi …</option>{% for target in targets %}<option value="{{ target['id'] }}">{{ target['callsign'] }} · {{ target['full_name'] }}</option>{% endfor %}</select></div><div class="actions"><button class="btn btn-danger">Združi</button><a class="btn btn-secondary" href="{{ url_for('callsign_profile',entry_id=source['id']) }}">Prekliči</a></div></form>{% else %}<p class="empty">V imeniku ni drugega klicnega znaka, s katerim bi ga lahko združil.</p>{% endif %}</div>{% endblock %}'''

TEMPLATES["base.html"] = TEMPLATES["base.html"].replace(
    '<a href="{{ url_for(\'callsigns\') }}">Imenik</a>',
    '<a href="{{ url_for(\'callsigns\') }}">Imenik</a><a href="{{ url_for(\'statistics\') }}">Statistika</a>',
).replace(
    '<a href="{{ url_for(\'users\') }}">Uporabniki</a>',
    '<a href="{{ url_for(\'users\') }}">Uporabniki</a><a href="{{ url_for(\'audit_log_view\') }}">Revizija</a><a href="{{ url_for(\'security_view\') }}">Varnost</a><a href="{{ url_for(\'backups_view\') }}">Kopije</a><a href="{{ url_for(\'csv_import_view\') }}">Uvoz</a>',
).replace(
    '</style>',
    r'''.report-filters{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;align-items:end}.report-filters .field{margin:0}.stats-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.stat{text-align:center}.stat span{display:block;color:#65717c}.stat strong{display:block;font-size:2rem;margin-top:5px}.stat .profile-date{font-size:1.25rem}.bar-chart{display:grid;gap:12px}.bar-row{display:grid;grid-template-columns:minmax(95px,1.2fr) minmax(110px,3fr) 42px minmax(62px,auto);gap:9px;align-items:center}.bar-track{height:13px;background:#e7edf2;border-radius:999px;overflow:hidden}.bar-track i{display:block;height:100%;min-width:2px;background:var(--blue);border-radius:999px}.bar-row small{color:#65717c}.audit-details{display:block;white-space:pre-wrap;overflow-wrap:anywhere;max-width:420px;margin-top:7px}.net-notes{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.55}.notes-preview{display:none}.print-report{max-width:none}@media(max-width:700px){.stats-grid{grid-template-columns:1fr 1fr}.bar-row{grid-template-columns:minmax(85px,1.3fr) minmax(80px,2fr) 34px}.bar-row>small{display:none}.report-filters{grid-template-columns:1fr}}@media print{.stats-grid{grid-template-columns:repeat(4,1fr)}.print-report table{font-size:10pt}.notes-preview{display:block}}</style>''',
)

app.jinja_loader = ChoiceLoader(
    [
        FileSystemLoader(str(APP_ROOT / "templates")),
        DictLoader(TEMPLATES),
    ]
)


@app.context_processor
def current_defaults():
    current = now_local()
    return {"now_local_value": current.strftime("%Y-%m-%d"), "current_time": current.strftime("%H:%M")}


with app.app_context():
    init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
