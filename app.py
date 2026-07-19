import hmac
import json
import os
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

from flask import (
    Flask, abort, flash, g, redirect, render_template, request, session, url_for
)
from jinja2 import DictLoader
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash


APP_NAME = "S50TTT Dnevnik skedov"
BASE_VERSION = "1.11.0"
RELEASE_CHANNEL = os.environ.get("RELEASE_CHANNEL", "stable").strip().lower()
if RELEASE_CHANNEL not in {"stable", "alpha"}:
    RELEASE_CHANNEL = "stable"
APP_VERSION = (
    f"{BASE_VERSION}-alpha" if RELEASE_CHANNEL == "alpha" else BASE_VERSION
)
DB_PATH = os.environ.get("DATABASE_PATH", "/app/data/skedi.db")
TIMEZONE = ZoneInfo(os.environ.get("TZ", "Europe/Ljubljana"))
SCHEDULE_MONTHLY = "monthly"
SCHEDULE_SATURDAY = "saturday"
SCHEDULE_TYPES = {SCHEDULE_MONTHLY, SCHEDULE_SATURDAY}
SATURDAY_SERIES_START = date(2019, 1, 5)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=1024 * 1024,
)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


def now_local():
    return datetime.now(TIMEZONE).replace(tzinfo=None)


def now_db():
    return now_local().strftime("%Y-%m-%d %H:%M:%S")


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


def regular_net_open_date(net_date):
    if isinstance(net_date, str):
        net_date = date.fromisoformat(net_date)
    days_since_friday = (net_date.weekday() - 4) % 7
    return net_date - timedelta(days=days_since_friday)


def scheduled_net_participant_count(schedule_type, net_date):
    return get_db().execute(
        """SELECT COUNT(p.id) AS n
           FROM nets n LEFT JOIN participants p ON p.net_id=n.id
           WHERE n.schedule_type=? AND n.net_date=?""",
        (schedule_type, net_date),
    ).fetchone()["n"]


def recent_closed_saturday_summaries(limit=2):
    rows = get_db().execute(
        """SELECT n.net_date, COUNT(p.id) AS participant_count
           FROM nets n LEFT JOIN participants p ON p.net_id=n.id
           WHERE n.schedule_type=? AND n.status='closed' AND n.net_date<=?
           GROUP BY n.id ORDER BY n.net_date DESC LIMIT ?""",
        (SCHEDULE_SATURDAY, now_local().date().isoformat(), limit),
    ).fetchall()
    summaries = []
    for row in rows:
        summary = dict(row)
        summary["sequence_number"] = saturday_net_number(
            date.fromisoformat(row["net_date"])
        )
        summaries.append(summary)
    return summaries


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
            deleted_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS callsign_directory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            callsign TEXT NOT NULL UNIQUE COLLATE NOCASE,
            full_name TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            use_count INTEGER NOT NULL DEFAULT 0,
            last_used_at TEXT,
            created_by INTEGER NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL,
            updated_by INTEGER REFERENCES users(id),
            updated_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_nets_date ON nets(net_date DESC);
        CREATE INDEX IF NOT EXISTS idx_participants_net ON participants(net_id, checkin_at);
        CREATE INDEX IF NOT EXISTS idx_net_deletions_date
            ON net_deletions(deleted_at DESC);
        CREATE INDEX IF NOT EXISTS idx_callsign_directory_active
            ON callsign_directory(active, callsign);
        """
    )

    net_columns = {row["name"] for row in db.execute("PRAGMA table_info(nets)")}
    for column, declaration in {
        "schedule_type": "TEXT",
        "repeater": "TEXT",
        "control_callsign": "TEXT",
    }.items():
        if column not in net_columns:
            try:
                db.execute(f"ALTER TABLE nets ADD COLUMN {column} {declaration}")
            except sqlite3.OperationalError as error:
                if "duplicate column name" not in str(error).lower():
                    raise
    db.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_nets_schedule_date
           ON nets(schedule_type, net_date) WHERE schedule_type IS NOT NULL"""
    )
    db.commit()

    if db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0:
        username = os.environ.get("ADMIN_USERNAME", "S57ZM").strip()
        password = os.environ.get("ADMIN_PASSWORD", "").strip()
        if not password:
            raise RuntimeError("ADMIN_PASSWORD mora biti nastavljen ob prvem zagonu.")
        db.execute(
            """INSERT INTO users
               (username, full_name, callsign, password_hash, role, active, created_at)
               VALUES (?, ?, ?, ?, 'admin', 1, ?)""",
            (
                username,
                os.environ.get("ADMIN_NAME", "Marko Zidar").strip() or "Administrator",
                os.environ.get("ADMIN_CALLSIGN", "S57ZM").strip().upper() or "S57ZM",
                generate_password_hash(password),
                now_db(),
            ),
        )
        db.commit()

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


@app.before_request
def load_user_and_csrf():
    g.user = None
    if session.get("user_id"):
        g.user = get_db().execute(
            "SELECT * FROM users WHERE id = ? AND active = 1", (session["user_id"],)
        ).fetchone()
        if g.user is None:
            session.clear()

    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)

    if request.method == "POST":
        submitted = request.form.get("csrf_token", "")
        expected = session.get("csrf_token", "")
        if not submitted or not expected or not hmac.compare_digest(submitted, expected):
            abort(400, "Neveljaven varnostni žeton. Osveži stran in poskusi ponovno.")


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
    return len(password) >= 10


@app.route("/health")
def health():
    return {
        "status": "ok",
        "version": APP_VERSION,
        "channel": RELEASE_CHANNEL,
    }


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_db().execute(
            "SELECT * FROM users WHERE username = ? AND active = 1", (username,)
        ).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["csrf_token"] = secrets.token_urlsafe(32)
            return redirect(url_for("dashboard"))
        flash("Napačno uporabniško ime ali geslo.", "danger")
    displayed_saturday = next(
        scheduled
        for scheduled in next_scheduled_nets(include_started_today=True)
        if scheduled["schedule_type"] == SCHEDULE_SATURDAY
    )
    return render_template(
        "login.html",
        countdown_net=next_countdown_net(),
        next_saturday=displayed_saturday,
        saturday_participant_count=scheduled_net_participant_count(
            SCHEDULE_SATURDAY, displayed_saturday["date"]
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
    scheduled_nets = next_scheduled_nets()
    current_date = now_local().date()
    for scheduled in scheduled_nets:
        existing = db.execute(
            """SELECT id, status FROM nets
               WHERE schedule_type=? AND net_date=?""",
            (scheduled["schedule_type"], scheduled["date"]),
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
        "dashboard.html",
        open_nets=open_nets,
        recent=recent,
        scheduled_nets=scheduled_nets,
    )


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
        scheduled_info = scheduled_net_for_date(schedule_type, parsed_start.date())
        if scheduled_info is None or started_time != scheduled_info["time"]:
            flash("Datum ali ura ne ustrezata pravilom izbranega rednega skeda.", "danger")
            return redirect(url_for("dashboard"))
        title = scheduled_info["title"]
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
            "SELECT id FROM nets WHERE schedule_type=? AND net_date=?",
            (schedule_type, net_date),
        ).fetchone()
        if existing:
            flash("Dnevnik za ta redni sked že obstaja.", "warning")
            return redirect(url_for("net_detail", net_id=existing["id"]))

    try:
        cur = db.execute(
            """INSERT INTO nets
               (title, net_date, started_at, status, leader_id, schedule_type,
                repeater, control_callsign, created_at)
               VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?)""",
            (
                title[:120],
                net_date,
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
                "SELECT id FROM nets WHERE schedule_type=? AND net_date=?",
                (schedule_type, net_date),
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
    return render_template(
        "net.html",
        net=net,
        participants=participants,
        now=now_local(),
        can_delete_net=can_delete_net,
        directory_entries=directory_entries,
    )


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

        before = {
            "title": net["title"],
            "net_date": net["net_date"],
            "started_at": net["started_at"],
            "ended_at": net["ended_at"],
            "leader_id": net["leader_id"],
        }
        after = {
            "title": title[:180],
            "net_date": net_date,
            "started_at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
            "ended_at": ended_at.strftime("%Y-%m-%d %H:%M:%S"),
            "leader_id": leader_id,
        }
        try:
            db.execute(
                """UPDATE nets
                   SET title=?, net_date=?, started_at=?, ended_at=?, leader_id=?
                   WHERE id=?""",
                (
                    after["title"],
                    after["net_date"],
                    after["started_at"],
                    after["ended_at"],
                    after["leader_id"],
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
        "net_edit.html",
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
    if net["status"] != "open" and g.user["role"] != "admin":
        abort(403)
    full_name = request.form.get("full_name", "").strip()
    callsign = request.form.get("callsign", "").strip().upper().replace(" ", "")
    checkin_time = request.form.get("checkin_time", now_local().strftime("%H:%M"))
    if not full_name or not callsign:
        flash("Vpiši ime in klicni znak.", "danger")
        return redirect(participant_return_url(net_id))
    try:
        datetime.strptime(checkin_time, "%H:%M")
    except ValueError:
        flash("Ura prijave ni veljavna.", "danger")
        return redirect(participant_return_url(net_id))
    db = get_db()
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
    if net["status"] != "open" and g.user["role"] != "admin":
        abort(403)
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        callsign = request.form.get("callsign", "").strip().upper().replace(" ", "")
        checkin_time = request.form.get("checkin_time", "")
        if not full_name or not callsign:
            flash("Vpiši ime in klicni znak.", "danger")
            return redirect(request.url)
        try:
            datetime.strptime(checkin_time, "%H:%M")
            db = get_db()
            db.execute(
                """UPDATE participants SET full_name=?, callsign=?, checkin_at=?,
                   updated_by=?, updated_at=? WHERE id=?""",
                (full_name[:120], callsign[:24], f"{net['net_date']} {checkin_time}:00", g.user["id"], now_db(), participant_id),
            )
            db.commit()
        except ValueError:
            flash("Ura prijave ni veljavna.", "danger")
            return redirect(request.url)
        except sqlite3.IntegrityError:
            flash(f"Klicni znak {callsign} je v tem skedu že vpisan.", "warning")
            return redirect(request.url)
        audit("update", "participant", participant_id, f"{callsign} – {full_name}")
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
    if net["status"] != "open" and g.user["role"] != "admin":
        abort(403)
    get_db().execute("DELETE FROM participants WHERE id=?", (participant_id,))
    get_db().commit()
    audit("delete", "participant", participant_id, f"{participant['callsign']} – {participant['full_name']}")
    flash("Vnos je izbrisan.", "success")
    return redirect(participant_return_url(net["id"]))


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
                    "repeater",
                    "control_callsign",
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
        flash("Zaključeni sked je izbrisan, razlog in kopija podatkov pa sta shranjena.", "success")
        return redirect(url_for("archive"))

    return render_template(
        "net_delete.html", net=net, participant_count=participant_count, reason=""
    )


@app.post("/nets/<int:net_id>/close")
@login_required
def close_net(net_id):
    net = fetch_net(net_id)
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


@app.route("/archive")
@login_required
def archive():
    rows = get_db().execute(
        """SELECT n.*, u.full_name AS leader_name, u.callsign AS leader_callsign,
                  COUNT(p.id) AS participant_count
           FROM nets n JOIN users u ON u.id=n.leader_id
           LEFT JOIN participants p ON p.net_id=n.id
           GROUP BY n.id ORDER BY n.started_at DESC"""
    ).fetchall()
    return render_template("archive.html", nets=rows)


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
    return render_template("callsigns.html", entries=rows, query=query)


def fetch_callsign_entry(entry_id):
    row = get_db().execute(
        "SELECT * FROM callsign_directory WHERE id=?", (entry_id,)
    ).fetchone()
    if row is None:
        abort(404)
    return row


@app.route("/callsigns/new", methods=["GET", "POST"])
@admin_required
def new_callsign():
    if request.method == "POST":
        callsign = request.form.get("callsign", "").strip().upper().replace(" ", "")
        full_name = request.form.get("full_name", "").strip()
        if not callsign or not full_name:
            flash("Vpiši klicni znak in ime.", "danger")
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
        callsign = request.form.get("callsign", "").strip().upper().replace(" ", "")
        full_name = request.form.get("full_name", "").strip()
        active = 1 if request.form.get("active") == "1" else 0
        if not callsign or not full_name:
            flash("Vpiši klicni znak in ime.", "danger")
        else:
            try:
                get_db().execute(
                    """UPDATE callsign_directory
                       SET callsign=?, full_name=?, active=?, updated_by=?, updated_at=?
                       WHERE id=?""",
                    (
                        callsign[:24],
                        full_name[:120],
                        active,
                        g.user["id"],
                        now_db(),
                        entry_id,
                    ),
                )
                get_db().commit()
                audit("update", "callsign", entry_id, f"{callsign} – {full_name}")
                flash("Vnos v imeniku je posodobljen.", "success")
                return redirect(url_for("callsigns"))
            except sqlite3.IntegrityError:
                get_db().rollback()
                flash("Ta klicni znak je že v imeniku.", "warning")
    return render_template("callsign_form.html", entry=entry)


@app.route("/users")
@admin_required
def users():
    rows = get_db().execute("SELECT * FROM users ORDER BY active DESC, full_name").fetchall()
    return render_template("users.html", users=rows)


@app.route("/users/new", methods=["GET", "POST"])
@admin_required
def new_user():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        full_name = request.form.get("full_name", "").strip()
        callsign = request.form.get("callsign", "").strip().upper().replace(" ", "")
        password = request.form.get("password", "")
        role = request.form.get("role", "leader")
        if not username or not full_name or not callsign or role not in {"admin", "leader"}:
            flash("Izpolni vsa zahtevana polja.", "danger")
        elif not valid_password(password):
            flash("Geslo mora imeti najmanj 10 znakov.", "danger")
        else:
            try:
                cur = get_db().execute(
                    """INSERT INTO users(username, full_name, callsign, password_hash, role, active, created_at)
                       VALUES (?, ?, ?, ?, ?, 1, ?)""",
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
        callsign = request.form.get("callsign", "").strip().upper().replace(" ", "")
        role = request.form.get("role", "leader")
        active = 1 if request.form.get("active") == "1" else 0
        password = request.form.get("password", "")
        if user_id == g.user["id"] and not active:
            flash("Svojega računa ne moreš onemogočiti.", "danger")
        elif not full_name or not callsign or role not in {"admin", "leader"}:
            flash("Izpolni vsa zahtevana polja.", "danger")
        elif password and not valid_password(password):
            flash("Novo geslo mora imeti najmanj 10 znakov.", "danger")
        else:
            if password:
                get_db().execute(
                    """UPDATE users SET full_name=?, callsign=?, role=?, active=?, password_hash=?
                       WHERE id=?""",
                    (full_name[:120], callsign[:24], role, active, generate_password_hash(password), user_id),
                )
            else:
                get_db().execute(
                    "UPDATE users SET full_name=?, callsign=?, role=?, active=? WHERE id=?",
                    (full_name[:120], callsign[:24], role, active, user_id),
                )
            get_db().commit()
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
            flash("Novo geslo mora imeti najmanj 10 znakov.", "danger")
        elif new != confirm:
            flash("Novi gesli se ne ujemata.", "danger")
        else:
            get_db().execute(
                "UPDATE users SET password_hash=? WHERE id=?",
                (generate_password_hash(new), g.user["id"]),
            )
            get_db().commit()
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


TEMPLATES = {
"base.html": r'''<!doctype html>
<html lang="sl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{% block title %}{{ app_name }}{% endblock %}</title>
<style>
:root{--blue:#145da0;--blue2:#0d477d;--light:#eef5fb;--line:#d7e0e8;--danger:#b42318;--success:#117a43;--text:#17202a}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:var(--text);background:#f5f7f9}
header{background:linear-gradient(135deg,var(--blue2),var(--blue));color:white;box-shadow:0 2px 8px #0003}.nav{max-width:1100px;margin:auto;padding:14px 18px;display:flex;align-items:center;gap:18px;flex-wrap:wrap}.brand{font-weight:800;font-size:1.12rem;margin-right:auto}.nav a,.link-button{color:white;text-decoration:none;font-weight:650;background:none;border:0;padding:0;cursor:pointer;font:inherit}.user{font-size:.9rem;opacity:.9}
main{max-width:1100px;margin:24px auto;padding:0 16px}.card{background:white;border:1px solid var(--line);border-radius:14px;padding:20px;margin-bottom:18px;box-shadow:0 2px 10px #1020300c}.card h1,.card h2{margin-top:0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}.actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.schedule-box{border:1px solid var(--line);border-radius:12px;padding:16px;background:var(--light)}.schedule-box h2{margin:10px 0 8px}
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

app.jinja_loader = DictLoader(TEMPLATES)


@app.context_processor
def current_defaults():
    current = now_local()
    return {"now_local_value": current.strftime("%Y-%m-%d"), "current_time": current.strftime("%H:%M")}


with app.app_context():
    init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
