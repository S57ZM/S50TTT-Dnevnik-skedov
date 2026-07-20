import io
import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import generate_password_hash as werkzeug_generate_password_hash


TEST_DATA = tempfile.TemporaryDirectory()
os.environ["DATABASE_PATH"] = os.path.join(TEST_DATA.name, "test-skedi.db")
os.environ["BACKUP_DIR"] = os.path.join(TEST_DATA.name, "backups")
os.environ["ADMIN_PASSWORD"] = "test-password-123"
os.environ["SECRET_KEY"] = "test-secret-key"

from app import (  # noqa: E402
    APP_VERSION,
    RELEASE_CHANNEL,
    SCHEDULE_MONTHLY,
    SCHEDULE_SATURDAY,
    app as flask_app,
    get_db,
    init_db,
    next_effective_countdown_net,
    next_effective_saturday_net,
    next_countdown_net,
    next_scheduled_nets,
    next_saturday_net,
    now_db,
    regular_net_open_date,
    saturday_net_number,
    saturday_start_time,
    scheduled_net_for_date,
)
import app as app_module  # noqa: E402
import backup as backup_tools  # noqa: E402


BASELINE_DATABASE = Path(os.environ["DATABASE_PATH"])
FAST_TEST_PASSWORD_HASH = werkzeug_generate_password_hash(
    "test-password-123", method="pbkdf2:sha256:1000"
)


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.test_data = tempfile.TemporaryDirectory()
        database_path = Path(self.test_data.name) / "test-skedi.db"
        backup_directory = Path(self.test_data.name) / "backups"
        with sqlite3.connect(BASELINE_DATABASE) as source, sqlite3.connect(
            database_path
        ) as target:
            source.backup(target)
        app_module.DB_PATH = str(database_path)
        backup_tools.DATABASE_PATH = database_path
        backup_tools.BACKUP_DIR = backup_directory
        backup_tools.OFFSITE_BACKUP_DIR = None
        self.password_patcher = patch(
            "app.generate_password_hash",
            lambda password: werkzeug_generate_password_hash(
                password, method="pbkdf2:sha256:1000"
            ),
        )
        self.password_patcher.start()
        os.environ["DATABASE_PATH"] = str(database_path)
        os.environ["BACKUP_DIR"] = str(backup_directory)
        flask_app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
        with flask_app.app_context():
            get_db().execute(
                """UPDATE users SET must_change_password=0, password_hash=?""",
                (FAST_TEST_PASSWORD_HASH,),
            )
            get_db().commit()

    def tearDown(self):
        self.password_patcher.stop()
        self.test_data.cleanup()

    def create_open_net(self, title):
        with flask_app.app_context():
            db = get_db()
            admin_id = db.execute(
                "SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1"
            ).fetchone()["id"]
            cursor = db.execute(
                """INSERT INTO nets
                   (title, net_date, started_at, status, leader_id, created_at)
                   VALUES (?, '2030-01-05', '2030-01-05 20:00:00', 'open', ?, ?)""",
                (title, admin_id, now_db()),
            )
            db.commit()
            return cursor.lastrowid, admin_id

    def create_closed_net(self, title, with_participant=False):
        with flask_app.app_context():
            db = get_db()
            admin_id = db.execute(
                "SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1"
            ).fetchone()["id"]
            cursor = db.execute(
                """INSERT INTO nets
                   (title, net_date, started_at, ended_at, status, leader_id, created_at)
                   VALUES (?, '2030-02-02', '2030-02-02 20:00:00',
                           '2030-02-02 20:30:00', 'closed', ?, ?)""",
                (title, admin_id, now_db()),
            )
            if with_participant:
                db.execute(
                    """INSERT INTO participants
                       (net_id, full_name, callsign, checkin_at, created_by, created_at)
                       VALUES (?, 'Testni Radioamater', 'S58TEST',
                               '2030-02-02 20:05:00', ?, ?)""",
                    (cursor.lastrowid, admin_id, now_db()),
                )
            db.commit()
            return cursor.lastrowid, admin_id

    def authenticated_client(self, user_id):
        client = flask_app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = user_id
            session["csrf_token"] = "test-csrf-token"
        return client

    def post_delete_net(self, net_id, user_id):
        client = self.authenticated_client(user_id)
        return client.post(
            f"/nets/{net_id}/delete",
            data={"csrf_token": "test-csrf-token"},
        )

    def test_health_reports_application_version(self):
        response = flask_app.test_client().get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["version"], APP_VERSION)
        self.assertEqual(response.get_json()["channel"], RELEASE_CHANNEL)

    def test_new_participant_is_learned_and_suggested_from_directory(self):
        net_id, admin_id = self.create_open_net("Test imenika klicnih znakov")
        client = self.authenticated_client(admin_id)

        response = client.post(
            f"/nets/{net_id}/participants",
            data={
                "csrf_token": "test-csrf-token",
                "callsign": "S56DIR",
                "full_name": "Janez Imenik",
                "checkin_time": "20:10",
            },
        )

        self.assertEqual(response.status_code, 302)
        with flask_app.app_context():
            entry = get_db().execute(
                "SELECT * FROM callsign_directory WHERE callsign='S56DIR'"
            ).fetchone()
            self.assertEqual(entry["full_name"], "Janez Imenik")
            self.assertEqual(entry["use_count"], 1)

        detail_response = client.get(f"/nets/{net_id}")
        detail_html = detail_response.get_data(as_text=True)
        self.assertIn('value="S56DIR"', detail_html)
        self.assertIn('data-full-name="Janez Imenik"', detail_html)
        self.assertIn("Imenik", detail_html)

        second_net_id, _ = self.create_open_net("Drugi test imenika")
        client.post(
            f"/nets/{second_net_id}/participants",
            data={
                "csrf_token": "test-csrf-token",
                "callsign": "S56DIR",
                "full_name": "Drugače vpisano ime",
                "checkin_time": "20:20",
            },
        )
        with flask_app.app_context():
            entry = get_db().execute(
                "SELECT * FROM callsign_directory WHERE callsign='S56DIR'"
            ).fetchone()
            self.assertEqual(entry["full_name"], "Janez Imenik")
            self.assertEqual(entry["use_count"], 2)

    def test_admin_can_create_edit_and_hide_directory_entry(self):
        with flask_app.app_context():
            admin_id = get_db().execute(
                "SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1"
            ).fetchone()["id"]
        client = self.authenticated_client(admin_id)

        create_response = client.post(
            "/callsigns/new",
            data={
                "csrf_token": "test-csrf-token",
                "callsign": "S50BOOK",
                "full_name": "Ročni Vnos",
            },
        )
        self.assertEqual(create_response.status_code, 302)

        with flask_app.app_context():
            entry = get_db().execute(
                "SELECT * FROM callsign_directory WHERE callsign='S50BOOK'"
            ).fetchone()
            entry_id = entry["id"]

        edit_response = client.post(
            f"/callsigns/{entry_id}/edit",
            data={
                "csrf_token": "test-csrf-token",
                "callsign": "S50BOOK",
                "full_name": "Popravljen Ročni Vnos",
            },
        )
        self.assertEqual(edit_response.status_code, 302)

        directory_response = client.get("/callsigns?q=S50BOOK")
        directory_html = directory_response.get_data(as_text=True)
        self.assertIn("Popravljen Ročni Vnos", directory_html)
        self.assertIn("Skrit", directory_html)

        with flask_app.app_context():
            entry = get_db().execute(
                "SELECT * FROM callsign_directory WHERE id=?", (entry_id,)
            ).fetchone()
            self.assertEqual(entry["active"], 0)
            self.assertEqual(entry["full_name"], "Popravljen Ročni Vnos")

    def test_callsign_profile_shows_history_and_keeps_notes_admin_only(self):
        net_id, admin_id = self.create_open_net("Profilni testni sked")
        admin_client = self.authenticated_client(admin_id)
        response = admin_client.post(
            f"/nets/{net_id}/participants",
            data={
                "csrf_token": "test-csrf-token",
                "callsign": "S58PROFILE",
                "full_name": "Profilni Radioamater",
                "checkin_time": "20:17",
            },
        )
        self.assertEqual(response.status_code, 302)

        with flask_app.app_context():
            db = get_db()
            entry_id = db.execute(
                "SELECT id FROM callsign_directory WHERE callsign='S58PROFILE'"
            ).fetchone()["id"]
            db.execute(
                """INSERT OR IGNORE INTO users
                   (username, full_name, callsign, password_hash, role, active, created_at)
                   VALUES ('profile-leader', 'Profilni Vodja', 'S50PRF',
                           'not-used-in-test', 'leader', 1, ?)""",
                (now_db(),),
            )
            leader_id = db.execute(
                "SELECT id FROM users WHERE username='profile-leader'"
            ).fetchone()["id"]
            db.commit()

        profile_response = admin_client.get(f"/callsigns/{entry_id}")
        profile_html = profile_response.get_data(as_text=True)
        self.assertEqual(profile_response.status_code, 200)
        self.assertIn("S58PROFILE", profile_html)
        self.assertIn("Profilni testni sked", profile_html)
        self.assertIn("Sodelovanja po letih", profile_html)

        note = "Interna testna opomba, vidna samo administratorju."
        note_response = admin_client.post(
            f"/callsigns/{entry_id}",
            data={"csrf_token": "test-csrf-token", "notes": note},
        )
        self.assertEqual(note_response.status_code, 302)
        with flask_app.app_context():
            db = get_db()
            entry = db.execute(
                "SELECT notes FROM callsign_directory WHERE id=?", (entry_id,)
            ).fetchone()
            audit_row = db.execute(
                """SELECT id FROM audit_log
                   WHERE action='update_notes' AND entity_type='callsign'
                     AND entity_id=?""",
                (entry_id,),
            ).fetchone()
            self.assertEqual(entry["notes"], note)
            self.assertIsNotNone(audit_row)

        leader_client = self.authenticated_client(leader_id)
        leader_html = leader_client.get(f"/callsigns/{entry_id}").get_data(as_text=True)
        self.assertNotIn(note, leader_html)
        self.assertNotIn("Interna opomba", leader_html)
        self.assertEqual(
            leader_client.post(
                f"/callsigns/{entry_id}",
                data={"csrf_token": "test-csrf-token", "notes": "Nedovoljeno"},
            ).status_code,
            403,
        )

    def test_admin_can_merge_duplicate_callsigns_without_duplicate_attendance(self):
        first_net_id, admin_id = self.create_open_net("Prvi sked za združitev")
        second_net_id, _ = self.create_open_net("Drugi sked za združitev")
        with flask_app.app_context():
            db = get_db()
            source = db.execute(
                """INSERT INTO callsign_directory
                   (callsign, full_name, active, use_count, notes, created_by, created_at)
                   VALUES ('S58OLD', 'Staro Ime', 1, 2, 'Opomba starega vnosa', ?, ?)""",
                (admin_id, now_db()),
            )
            target = db.execute(
                """INSERT INTO callsign_directory
                   (callsign, full_name, active, use_count, notes, created_by, created_at)
                   VALUES ('S58NEW', 'Pravo Ime', 1, 1, 'Ciljna opomba', ?, ?)""",
                (admin_id, now_db()),
            )
            source_id = source.lastrowid
            target_id = target.lastrowid
            for net_id, callsign, checkin_time in (
                (first_net_id, "S58OLD", "20:05"),
                (second_net_id, "S58OLD", "20:06"),
                (second_net_id, "S58NEW", "20:07"),
            ):
                db.execute(
                    """INSERT INTO participants
                       (net_id, full_name, callsign, checkin_at, created_by, created_at)
                       VALUES (?, 'Test združitve', ?, ?, ?, ?)""",
                    (
                        net_id,
                        callsign,
                        f"2030-01-05 {checkin_time}:00",
                        admin_id,
                        now_db(),
                    ),
                )
            db.commit()

        client = self.authenticated_client(admin_id)
        response = client.post(
            f"/callsigns/{source_id}/merge",
            data={"csrf_token": "test-csrf-token", "target_id": target_id},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith(f"/callsigns/{target_id}"))

        with flask_app.app_context():
            db = get_db()
            self.assertIsNone(
                db.execute(
                    "SELECT id FROM callsign_directory WHERE id=?", (source_id,)
                ).fetchone()
            )
            target_entry = db.execute(
                "SELECT * FROM callsign_directory WHERE id=?", (target_id,)
            ).fetchone()
            attendance = db.execute(
                "SELECT net_id FROM participants WHERE callsign='S58NEW' ORDER BY net_id"
            ).fetchall()
            audit_row = db.execute(
                """SELECT details FROM audit_log
                   WHERE action='merge' AND entity_type='callsign' AND entity_id=?
                   ORDER BY id DESC LIMIT 1""",
                (target_id,),
            ).fetchone()
            self.assertEqual(len(attendance), 2)
            self.assertEqual(len({row["net_id"] for row in attendance}), 2)
            self.assertEqual(target_entry["use_count"], 2)
            self.assertIn("Opomba starega vnosa", target_entry["notes"])
            self.assertIn('"removed_duplicates": 1', audit_row["details"])

        self.assertEqual(client.get(f"/callsigns/{source_id}").status_code, 404)
        profile_html = client.get(f"/callsigns/{target_id}").get_data(as_text=True)
        self.assertIn("Prvi sked za združitev", profile_html)
        self.assertIn("Drugi sked za združitev", profile_html)

    def test_alpha_channel_has_visible_warning(self):
        with patch("app.RELEASE_CHANNEL", "alpha"), patch(
            "app.APP_VERSION", "1.21.0-alpha"
        ):
            response = flask_app.test_client().get("/login")
            health_response = flask_app.test_client().get("/health")

        html = response.get_data(as_text=True)
        self.assertIn("ALPHA TESTNA RAZLIČICA", html)
        self.assertIn("podatki niso produkcijski", html)
        self.assertIn("različica 1.21.0-alpha", html)
        self.assertEqual(health_response.get_json()["channel"], "alpha")

    def test_login_shows_countdown_and_next_saturday_number(self):
        displayed_saturday = next(
            scheduled
            for scheduled in next_scheduled_nets(include_started_today=True)
            if scheduled["schedule_type"] == SCHEDULE_SATURDAY
        )
        with flask_app.app_context():
            db = get_db()
            admin_id = db.execute(
                "SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1"
            ).fetchone()["id"]
            cursor = db.execute(
                """INSERT INTO nets
                   (title, net_date, started_at, status, leader_id,
                    schedule_type, repeater, control_callsign, created_at)
                   VALUES (?, ?, ?, 'open', ?, ?, ?, 'S50TTT', ?)""",
                (
                    "Javni testni sobotni sked",
                    displayed_saturday["date"],
                    f"{displayed_saturday['date']} {displayed_saturday['time']}:00",
                    admin_id,
                    SCHEDULE_SATURDAY,
                    displayed_saturday["repeater"],
                    now_db(),
                ),
            )
            for callsign in ("S51TEST", "S52TEST"):
                db.execute(
                    """INSERT INTO participants
                       (net_id, full_name, callsign, checkin_at, created_by, created_at)
                       VALUES (?, 'Testni udeleženec', ?, ?, ?, ?)""",
                    (
                        cursor.lastrowid,
                        callsign,
                        f"{displayed_saturday['date']} {displayed_saturday['time']}:00",
                        admin_id,
                        now_db(),
                    ),
                )

            past_saturdays = []
            displayed_date = date.fromisoformat(displayed_saturday["date"])
            for weeks_back, participant_count in ((1, 3), (2, 4)):
                past_date = displayed_date - timedelta(days=7 * weeks_back)
                scheduled = scheduled_net_for_date(SCHEDULE_SATURDAY, past_date)
                past_cursor = db.execute(
                    """INSERT INTO nets
                       (title, net_date, started_at, ended_at, status, leader_id,
                        schedule_type, repeater, control_callsign, created_at)
                       VALUES (?, ?, ?, ?, 'closed', ?, ?, ?, 'S50TTT', ?)""",
                    (
                        f"Pretekli sobotni sked {weeks_back}",
                        scheduled["date"],
                        f"{scheduled['date']} {scheduled['time']}:00",
                        f"{scheduled['date']} 21:30:00",
                        admin_id,
                        SCHEDULE_SATURDAY,
                        scheduled["repeater"],
                        now_db(),
                    ),
                )
                for index in range(participant_count):
                    db.execute(
                        """INSERT INTO participants
                           (net_id, full_name, callsign, checkin_at,
                            created_by, created_at)
                           VALUES (?, 'Pretekli udeleženec', ?, ?, ?, ?)""",
                        (
                            past_cursor.lastrowid,
                            f"S5{weeks_back}{index}T",
                            f"{scheduled['date']} {scheduled['time']}:00",
                            admin_id,
                            now_db(),
                        ),
                    )
                past_saturdays.append((past_date, participant_count))
            db.commit()

        response = flask_app.test_client().get("/login")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("data-countdown=", html)
        self.assertIn("Redni sobotni sked", html)
        self.assertIn('data-participant-count="2"', html)
        self.assertIn("Zadnja zaključena sobotna skeda", html)
        for past_date, participant_count in past_saturdays:
            self.assertIn(f"št. {saturday_net_number(past_date)}", html)
            self.assertIn(f'data-history-count="{participant_count}"', html)

    def test_login_is_temporarily_locked_and_admin_can_unlock_it(self):
        with flask_app.app_context():
            db = get_db()
            admin = db.execute(
                "SELECT * FROM users WHERE role='admin' ORDER BY id LIMIT 1"
            ).fetchone()
            admin_id = admin["id"]
            username = admin["username"]
            db.execute(
                """UPDATE users SET failed_login_count=0, locked_until=NULL,
                   last_login_at=NULL, last_login_ip=NULL WHERE id=?""",
                (admin_id,),
            )
            db.execute("DELETE FROM login_attempts")
            db.commit()

        client = flask_app.test_client()
        client.get("/login")
        with client.session_transaction() as session:
            csrf_token = session["csrf_token"]

        for _ in range(5):
            response = client.post(
                "/login",
                data={
                    "csrf_token": csrf_token,
                    "username": username,
                    "password": "napačno-geslo",
                },
            )
            self.assertEqual(response.status_code, 200)

        with flask_app.app_context():
            db = get_db()
            locked = db.execute("SELECT * FROM users WHERE id=?", (admin_id,)).fetchone()
            self.assertEqual(locked["failed_login_count"], 5)
            self.assertIsNotNone(locked["locked_until"])
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) AS n FROM login_attempts WHERE success=0"
                ).fetchone()["n"],
                5,
            )
            self.assertIsNotNone(
                db.execute(
                    """SELECT id FROM audit_log WHERE action='login_locked'
                       AND entity_type='user' AND entity_id=?""",
                    (admin_id,),
                ).fetchone()
            )

        blocked_response = client.post(
            "/login",
            data={
                "csrf_token": csrf_token,
                "username": username,
                "password": "test-password-123",
            },
        )
        self.assertEqual(blocked_response.status_code, 200)
        self.assertIn(
            "poskusi znova čez 15 minut", blocked_response.get_data(as_text=True)
        )

        admin_client = self.authenticated_client(admin_id)
        unlock_response = admin_client.post(
            f"/users/{admin_id}/unlock",
            data={"csrf_token": "test-csrf-token"},
        )
        self.assertEqual(unlock_response.status_code, 302)

        success_response = client.post(
            "/login",
            data={
                "csrf_token": csrf_token,
                "username": username,
                "password": "test-password-123",
            },
        )
        self.assertEqual(success_response.status_code, 302)
        with flask_app.app_context():
            updated = get_db().execute(
                "SELECT * FROM users WHERE id=?", (admin_id,)
            ).fetchone()
            self.assertEqual(updated["failed_login_count"], 0)
            self.assertIsNone(updated["locked_until"])
            self.assertIsNotNone(updated["last_login_at"])

        security_html = client.get("/security").get_data(as_text=True)
        self.assertIn("Varnost prijav", security_html)
        self.assertIn("Uspešna prijava", security_html)

    def test_next_summer_saturday_and_monthly_net(self):
        scheduled = next_scheduled_nets(datetime(2026, 7, 19, 12, 0))
        by_type = {item["schedule_type"]: item for item in scheduled}

        self.assertEqual(by_type[SCHEDULE_SATURDAY]["date"], "2026-07-25")
        self.assertEqual(by_type[SCHEDULE_SATURDAY]["time"], "21:00")
        self.assertEqual(by_type[SCHEDULE_MONTHLY]["date"], "2026-08-06")
        self.assertEqual(by_type[SCHEDULE_MONTHLY]["time"], "19:00")

    def test_saturday_season_boundaries(self):
        self.assertEqual(saturday_start_time(date(2026, 5, 30)), "20:00")
        self.assertEqual(saturday_start_time(date(2026, 6, 6)), "21:00")
        self.assertEqual(saturday_start_time(date(2026, 8, 29)), "21:00")
        self.assertEqual(saturday_start_time(date(2026, 9, 5)), "20:00")

    def test_monthly_net_must_be_first_thursday(self):
        valid = scheduled_net_for_date(SCHEDULE_MONTHLY, date(2026, 8, 6))
        invalid = scheduled_net_for_date(SCHEDULE_MONTHLY, date(2026, 8, 13))

        self.assertIsNotNone(valid)
        self.assertEqual(valid["time"], "19:00")
        self.assertIsNone(invalid)

    def test_regular_net_unlocks_on_friday_or_after_five_presses(self):
        reference = datetime(2026, 7, 19, 12, 0)
        saturday = scheduled_net_for_date(SCHEDULE_SATURDAY, date(2026, 7, 25))
        monthly = scheduled_net_for_date(SCHEDULE_MONTHLY, date(2026, 8, 6))

        self.assertEqual(regular_net_open_date(saturday["date"]), date(2026, 7, 24))
        self.assertEqual(regular_net_open_date(monthly["date"]), date(2026, 7, 31))

        with flask_app.app_context():
            db = get_db()
            db.execute(
                """DELETE FROM nets WHERE schedule_type IS NOT NULL
                   AND net_date IN ('2026-07-25', '2026-08-06')"""
            )
            admin_id = db.execute(
                "SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1"
            ).fetchone()["id"]
            db.commit()

        client = self.authenticated_client(admin_id)
        with patch("app.now_local", return_value=reference):
            dashboard_response = client.get("/")
            dashboard_html = dashboard_response.get_data(as_text=True)

            self.assertEqual(
                dashboard_html.count("data-early-open aria-disabled=\"true\""), 2
            )
            self.assertNotIn("Na voljo od petka", dashboard_html)
            self.assertNotIn("Za predčasno odprtje", dashboard_html)
            script_response = client.get("/static/app.js")
            try:
                self.assertIn(
                    "Ti si pravi Heker", script_response.get_data(as_text=True)
                )
            finally:
                script_response.close()

            locked_response = client.post(
                "/nets/new",
                data={
                    "csrf_token": "test-csrf-token",
                    "schedule_type": monthly["schedule_type"],
                    "net_date": monthly["date"],
                    "started_time": monthly["time"],
                },
            )
            self.assertEqual(locked_response.status_code, 302)

            with flask_app.app_context():
                self.assertIsNone(
                    get_db().execute(
                        "SELECT id FROM nets WHERE schedule_type=? AND net_date=?",
                        (SCHEDULE_MONTHLY, monthly["date"]),
                    ).fetchone()
                )

            unlocked_response = client.post(
                "/nets/new",
                data={
                    "csrf_token": "test-csrf-token",
                    "schedule_type": monthly["schedule_type"],
                    "net_date": monthly["date"],
                    "started_time": monthly["time"],
                    "early_unlock": "1",
                },
            )
            self.assertEqual(unlocked_response.status_code, 302)

        with flask_app.app_context():
            created = get_db().execute(
                "SELECT id FROM nets WHERE schedule_type=? AND net_date=?",
                (SCHEDULE_MONTHLY, monthly["date"]),
            ).fetchone()
            audit_row = get_db().execute(
                """SELECT details FROM audit_log
                   WHERE action='create' AND entity_type='net' AND entity_id=?""",
                (created["id"],),
            ).fetchone()
            self.assertIn("predčasno odprtje", audit_row["details"])

    def test_saturday_net_uses_s55usx(self):
        scheduled = scheduled_net_for_date(SCHEDULE_SATURDAY, date(2026, 7, 25))

        self.assertEqual(scheduled["repeater"], "S55USX – Sv. Rok")
        self.assertEqual(scheduled["control_callsign"], "S50TTT")

    def test_saturday_sequence_starts_in_2019(self):
        self.assertEqual(saturday_net_number(date(2019, 1, 5)), 1)
        self.assertEqual(saturday_net_number(date(2026, 7, 25)), 395)
        self.assertIsNone(saturday_net_number(date(2026, 7, 26)))

    def test_countdown_moves_to_next_saturday_after_start(self):
        scheduled = next_countdown_net(datetime(2026, 7, 25, 21, 1))

        self.assertEqual(scheduled["date"], "2026-08-01")
        self.assertEqual(scheduled["sequence_number"], 396)

        next_saturday = next_saturday_net(datetime(2026, 7, 25, 21, 1))
        self.assertEqual(next_saturday["sequence_number"], 396)

    def test_csv_import_rejects_invalid_file_and_is_admin_only(self):
        with flask_app.app_context():
            db = get_db()
            admin_id = db.execute(
                "SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1"
            ).fetchone()["id"]
            before_count = db.execute(
                "SELECT COUNT(*) AS n FROM csv_imports"
            ).fetchone()["n"]
            db.execute(
                """INSERT OR IGNORE INTO users
                   (username, full_name, callsign, password_hash,
                    role, active, created_at)
                   VALUES ('import-leader', 'Vodja Brez Uvoza', 'S50CSV',
                           'not-used-in-test', 'leader', 1, ?)""",
                (now_db(),),
            )
            leader_id = db.execute(
                "SELECT id FROM users WHERE username='import-leader'"
            ).fetchone()["id"]
            db.commit()

        admin_client = self.authenticated_client(admin_id)
        template_response = admin_client.get("/imports/csv/template")
        self.assertEqual(template_response.status_code, 200)
        self.assertIn("datum;zacetek;konec", template_response.get_data(as_text=True))

        invalid_csv = "datum;zacetek\n2033-01-01;20:00\n"
        response = admin_client.post(
            "/imports/csv",
            data={
                "csrf_token": "test-csrf-token",
                "csv_file": (io.BytesIO(invalid_csv.encode("utf-8")), "napaka.csv"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Manjkajo obvezni stolpci", response.get_data(as_text=True))
        with flask_app.app_context():
            after_count = get_db().execute(
                "SELECT COUNT(*) AS n FROM csv_imports"
            ).fetchone()["n"]
            self.assertEqual(after_count, before_count)

        leader_client = self.authenticated_client(leader_id)
        self.assertEqual(leader_client.get("/imports/csv").status_code, 403)
        self.assertEqual(leader_client.get("/imports/csv/template").status_code, 403)

    def test_csv_import_preview_and_confirm_are_atomic(self):
        with flask_app.app_context():
            db = get_db()
            admin = db.execute(
                "SELECT id, callsign FROM users WHERE role='admin' ORDER BY id LIMIT 1"
            ).fetchone()
            admin_id = admin["id"]
            operator_callsign = admin["callsign"]
            db.execute(
                """DELETE FROM nets WHERE schedule_type=?
                   AND COALESCE(scheduled_date,net_date)='2033-01-01'""",
                (SCHEDULE_SATURDAY,),
            )
            db.commit()

        csv_text = (
            "datum;zacetek;konec;naslov;vrsta;operater;"
            "klicni_znak;ime;prijava;opombe\n"
            f"2033-01-01;20:00;20:45;;sobotni;{operator_callsign};"
            "S56IMP1;Prvi Uvoženi;20:04;Zgodovinska opomba\n"
            f"2033-01-01;20:00;20:45;;sobotni;{operator_callsign};"
            "S56IMP2;Drugi Uvoženi;20:09;Zgodovinska opomba\n"
        )
        client = self.authenticated_client(admin_id)
        preview_response = client.post(
            "/imports/csv",
            data={
                "csrf_token": "test-csrf-token",
                "csv_file": (io.BytesIO(csv_text.encode("utf-8")), "zgodovina.csv"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(preview_response.status_code, 302)

        with flask_app.app_context():
            db = get_db()
            batch = db.execute(
                """SELECT * FROM csv_imports WHERE filename='zgodovina.csv'
                   ORDER BY id DESC LIMIT 1"""
            ).fetchone()
            import_id = batch["id"]
            self.assertEqual(batch["status"], "pending")
            self.assertEqual(batch["net_count"], 1)
            self.assertEqual(batch["participant_count"], 2)
            self.assertIsNone(
                db.execute(
                    """SELECT id FROM nets WHERE schedule_type=?
                       AND COALESCE(scheduled_date,net_date)='2033-01-01'""",
                    (SCHEDULE_SATURDAY,),
                ).fetchone()
            )

        preview_html = client.get(
            f"/imports/csv/{import_id}"
        ).get_data(as_text=True)
        self.assertIn("Predogled – baza še ni spremenjena", preview_html)
        self.assertIn("S56IMP1", preview_html)
        self.assertIn("S56IMP2", preview_html)

        confirm_response = client.post(
            f"/imports/csv/{import_id}/confirm",
            data={"csrf_token": "test-csrf-token"},
        )
        self.assertEqual(confirm_response.status_code, 302)

        with flask_app.app_context():
            db = get_db()
            batch = db.execute(
                "SELECT * FROM csv_imports WHERE id=?", (import_id,)
            ).fetchone()
            imported_net = db.execute(
                """SELECT * FROM nets WHERE schedule_type=?
                   AND COALESCE(scheduled_date,net_date)='2033-01-01'""",
                (SCHEDULE_SATURDAY,),
            ).fetchone()
            participants = db.execute(
                "SELECT callsign FROM participants WHERE net_id=? ORDER BY callsign",
                (imported_net["id"],),
            ).fetchall()
            directory_entry = db.execute(
                "SELECT * FROM callsign_directory WHERE callsign='S56IMP1'"
            ).fetchone()
            audit_row = db.execute(
                """SELECT details FROM audit_log
                   WHERE action='import' AND entity_type='csv_import' AND entity_id=?
                   ORDER BY id DESC LIMIT 1""",
                (import_id,),
            ).fetchone()
            self.assertEqual(batch["status"], "imported")
            self.assertIsNone(batch["data_json"])
            self.assertEqual(imported_net["status"], "closed")
            self.assertEqual(imported_net["notes"], "Zgodovinska opomba")
            self.assertEqual(
                [row["callsign"] for row in participants], ["S56IMP1", "S56IMP2"]
            )
            self.assertEqual(directory_entry["use_count"], 1)
            self.assertIn("zgodovina.csv", audit_row["details"])

        self.assertTrue(
            any(
                item["name"].startswith("pre-import-")
                for item in backup_tools.list_backups()
            )
        )
        self.assertEqual(
            client.post(
                f"/imports/csv/{import_id}/confirm",
                data={"csrf_token": "test-csrf-token"},
            ).status_code,
            404,
        )

    def test_csv_import_rolls_back_if_conflict_appears_after_preview(self):
        with flask_app.app_context():
            db = get_db()
            admin = db.execute(
                "SELECT id, callsign FROM users WHERE role='admin' ORDER BY id LIMIT 1"
            ).fetchone()
            admin_id = admin["id"]
            operator_callsign = admin["callsign"]
            db.execute(
                "DELETE FROM nets WHERE title='CSV konflikt po predogledu'"
            )
            db.commit()

        csv_text = (
            "datum;zacetek;konec;naslov;vrsta;operater;"
            "klicni_znak;ime;prijava;opombe\n"
            f"2034-04-15;20:00;20:30;CSV konflikt po predogledu;izredni;"
            f"{operator_callsign};S56ROLL;Ne sme biti uvožen;20:05;\n"
        )
        client = self.authenticated_client(admin_id)
        response = client.post(
            "/imports/csv",
            data={
                "csrf_token": "test-csrf-token",
                "csv_file": (io.BytesIO(csv_text.encode("utf-8")), "konflikt.csv"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 302)

        with flask_app.app_context():
            db = get_db()
            batch = db.execute(
                """SELECT * FROM csv_imports WHERE filename='konflikt.csv'
                   ORDER BY id DESC LIMIT 1"""
            ).fetchone()
            import_id = batch["id"]
            db.execute(
                """INSERT INTO nets
                   (title, net_date, started_at, ended_at, status,
                    leader_id, created_at)
                   VALUES ('CSV konflikt po predogledu', '2034-04-15',
                           '2034-04-15 20:00:00', '2034-04-15 20:30:00',
                           'closed', ?, ?)""",
                (admin_id, now_db()),
            )
            db.commit()

        confirm = client.post(
            f"/imports/csv/{import_id}/confirm",
            data={"csrf_token": "test-csrf-token"},
        )
        self.assertEqual(confirm.status_code, 302)
        self.assertTrue(confirm.headers["Location"].endswith(f"/imports/csv/{import_id}"))
        with flask_app.app_context():
            db = get_db()
            batch = db.execute(
                "SELECT * FROM csv_imports WHERE id=?", (import_id,)
            ).fetchone()
            net_count = db.execute(
                "SELECT COUNT(*) AS n FROM nets WHERE title='CSV konflikt po predogledu'"
            ).fetchone()["n"]
            participant = db.execute(
                "SELECT id FROM participants WHERE callsign='S56ROLL'"
            ).fetchone()
            self.assertEqual(batch["status"], "pending")
            self.assertIsNotNone(batch["data_json"])
            self.assertEqual(net_count, 1)
            self.assertIsNone(participant)

    def test_empty_open_net_can_be_deleted(self):
        net_id, admin_id = self.create_open_net("Prazen testni sked")

        response = self.post_delete_net(net_id, admin_id)

        self.assertEqual(response.status_code, 302)
        with flask_app.app_context():
            self.assertIsNone(
                get_db().execute("SELECT id FROM nets WHERE id=?", (net_id,)).fetchone()
            )

    def test_net_with_participant_cannot_be_deleted(self):
        net_id, admin_id = self.create_open_net("Testni sked z udeležencem")
        with flask_app.app_context():
            db = get_db()
            db.execute(
                """INSERT INTO participants
                   (net_id, full_name, callsign, checkin_at, created_by, created_at)
                   VALUES (?, 'Testni Radioamater', 'S59TEST',
                           '2030-01-05 20:05:00', ?, ?)""",
                (net_id, admin_id, now_db()),
            )
            db.commit()

        response = self.post_delete_net(net_id, admin_id)

        self.assertEqual(response.status_code, 302)
        with flask_app.app_context():
            self.assertIsNotNone(
                get_db().execute("SELECT id FROM nets WHERE id=?", (net_id,)).fetchone()
            )

    def test_net_leader_can_write_notes_but_other_leader_cannot(self):
        with flask_app.app_context():
            db = get_db()
            for username, full_name, callsign in (
                ("notes-owner", "Vodja Zapisnika", "S50NOTE"),
                ("notes-other", "Drugi Vodja", "S51NOTE"),
            ):
                db.execute(
                    """INSERT OR IGNORE INTO users
                       (username, full_name, callsign, password_hash,
                        role, active, created_at)
                       VALUES (?, ?, ?, 'not-used-in-test', 'leader', 1, ?)""",
                    (username, full_name, callsign, now_db()),
                )
            owner_id = db.execute(
                "SELECT id FROM users WHERE username='notes-owner'"
            ).fetchone()["id"]
            other_id = db.execute(
                "SELECT id FROM users WHERE username='notes-other'"
            ).fetchone()["id"]
            cursor = db.execute(
                """INSERT INTO nets
                   (title, net_date, started_at, status, leader_id, created_at)
                   VALUES ('Sked z zapisnikom', '2032-05-08',
                           '2032-05-08 20:00:00', 'open', ?, ?)""",
                (owner_id, now_db()),
            )
            net_id = cursor.lastrowid
            db.commit()

        owner_client = self.authenticated_client(owner_id)
        notes = "Obvestilo radiokluba\nTežava na repetitorju <test>."
        response = owner_client.post(
            f"/nets/{net_id}/notes",
            data={"csrf_token": "test-csrf-token", "notes": notes},
        )
        self.assertEqual(response.status_code, 302)
        owner_html = owner_client.get(f"/nets/{net_id}").get_data(as_text=True)
        self.assertIn("Shrani zapisnik", owner_html)
        self.assertIn("&lt;test&gt;", owner_html)

        other_client = self.authenticated_client(other_id)
        other_html = other_client.get(f"/nets/{net_id}").get_data(as_text=True)
        self.assertIn("Obvestilo radiokluba", other_html)
        self.assertNotIn("Shrani zapisnik", other_html)
        forbidden = other_client.post(
            f"/nets/{net_id}/notes",
            data={"csrf_token": "test-csrf-token", "notes": "Nedovoljena sprememba"},
        )
        self.assertEqual(forbidden.status_code, 403)

        with flask_app.app_context():
            db = get_db()
            stored = db.execute(
                "SELECT notes FROM nets WHERE id=?", (net_id,)
            ).fetchone()["notes"]
            audit_row = db.execute(
                """SELECT id FROM audit_log
                   WHERE action='update_notes' AND entity_type='net' AND entity_id=?
                   ORDER BY id DESC LIMIT 1""",
                (net_id,),
            ).fetchone()
            self.assertEqual(stored, notes)
            self.assertIsNotNone(audit_row)

    def test_archive_search_filters_and_pagination(self):
        matching_id, admin_id = self.create_closed_net(
            "Posebni mesečni arhivski sked", with_participant=False
        )
        other_id, _ = self.create_closed_net(
            "Drugi sobotni arhivski sked", with_participant=False
        )
        with flask_app.app_context():
            db = get_db()
            db.execute(
                """UPDATE nets SET net_date='2042-03-06',
                   started_at='2042-03-06 19:00:00',
                   ended_at='2042-03-06 19:30:00', schedule_type=? WHERE id=?""",
                (SCHEDULE_MONTHLY, matching_id),
            )
            db.execute(
                """UPDATE nets SET net_date='2041-03-02',
                   started_at='2041-03-02 20:00:00',
                   ended_at='2041-03-02 20:30:00', schedule_type=? WHERE id=?""",
                (SCHEDULE_SATURDAY, other_id),
            )
            db.execute(
                """INSERT INTO participants
                   (net_id, full_name, callsign, checkin_at, created_by, created_at)
                   VALUES (?, 'Iskani Udeleženec', 'S59ARHIV',
                           '2042-03-06 19:05:00', ?, ?)""",
                (matching_id, admin_id, now_db()),
            )
            for day_number in range(1, 27):
                day_value = f"2045-01-{day_number:02d}"
                db.execute(
                    """INSERT INTO nets
                       (title, net_date, started_at, ended_at, status,
                        leader_id, created_at)
                       VALUES (?, ?, ?, ?, 'closed', ?, ?)""",
                    (
                        f"ARHIV-PAGE-{day_number:02d}",
                        day_value,
                        f"{day_value} 20:00:00",
                        f"{day_value} 20:30:00",
                        admin_id,
                        now_db(),
                    ),
                )
            db.commit()

        client = self.authenticated_client(admin_id)
        search_html = client.get("/archive?q=S59ARHIV").get_data(as_text=True)
        self.assertIn("Posebni mesečni arhivski sked", search_html)
        self.assertNotIn("Drugi sobotni arhivski sked", search_html)
        self.assertIn("Najdenih skedov: 1", search_html)

        filtered_html = client.get(
            "/archive?from_date=2042-01-01&to_date=2042-12-31"
            "&schedule_type=monthly&status=closed"
        ).get_data(as_text=True)
        self.assertIn("Posebni mesečni arhivski sked", filtered_html)
        self.assertNotIn("Drugi sobotni arhivski sked", filtered_html)

        first_page = client.get("/archive?q=ARHIV-PAGE").get_data(as_text=True)
        second_page = client.get(
            "/archive?q=ARHIV-PAGE&page=2"
        ).get_data(as_text=True)
        self.assertIn("Najdenih skedov: 26", first_page)
        self.assertIn("Stran 1 od 2", first_page)
        self.assertIn("Naslednja", first_page)
        self.assertNotIn("ARHIV-PAGE-01", first_page)
        self.assertIn("ARHIV-PAGE-01", second_page)
        self.assertIn("Prejšnja", second_page)

    def test_admin_can_edit_closed_net_and_change_participant_date(self):
        net_id, admin_id = self.create_closed_net(
            "Napačen naslov zaključenega skeda", with_participant=True
        )
        client = self.authenticated_client(admin_id)

        edit_page = client.get(f"/nets/{net_id}/edit")
        detail_page = client.get(f"/nets/{net_id}")
        archive_page = client.get("/archive")

        self.assertEqual(edit_page.status_code, 200)
        self.assertIn("Uredi zaključeni sked", edit_page.get_data(as_text=True))
        self.assertIn("Zapisnik / opombe skeda", edit_page.get_data(as_text=True))
        self.assertIn("Razlog brisanja", edit_page.get_data(as_text=True))
        self.assertIn(
            f'action="/nets/{net_id}/delete-closed"',
            edit_page.get_data(as_text=True),
        )
        self.assertIn("Izbriši sked", detail_page.get_data(as_text=True))
        self.assertIn("Uredi", archive_page.get_data(as_text=True))

        response = client.post(
            f"/nets/{net_id}/edit",
            data={
                "csrf_token": "test-csrf-token",
                "title": "Popravljen zaključeni sked",
                "net_date": "2030-02-09",
                "started_time": "21:00",
                "ended_time": "00:41",
                "leader_id": str(admin_id),
                "notes": "Naknadno dopolnjen zapisnik skeda.",
            },
        )

        self.assertEqual(response.status_code, 302)
        with flask_app.app_context():
            db = get_db()
            net = db.execute("SELECT * FROM nets WHERE id=?", (net_id,)).fetchone()
            participant = db.execute(
                "SELECT * FROM participants WHERE net_id=?", (net_id,)
            ).fetchone()
            audit_row = db.execute(
                """SELECT * FROM audit_log
                   WHERE action='update' AND entity_type='net' AND entity_id=?
                   ORDER BY id DESC LIMIT 1""",
                (net_id,),
            ).fetchone()

            self.assertEqual(net["title"], "Popravljen zaključeni sked")
            self.assertEqual(net["started_at"], "2030-02-09 21:00:00")
            self.assertEqual(net["notes"], "Naknadno dopolnjen zapisnik skeda.")
            self.assertEqual(net["ended_at"], "2030-02-10 00:41:00")
            self.assertTrue(participant["checkin_at"].startswith("2030-02-09 "))
            self.assertIn('"before"', audit_row["details"])
            self.assertIn('"after"', audit_row["details"])

    def test_admin_can_add_and_delete_participant_in_closed_net_editor(self):
        net_id, admin_id = self.create_closed_net("Zaključeni sked za naknadni vnos")
        client = self.authenticated_client(admin_id)

        editor_response = client.get(f"/nets/{net_id}/edit")
        editor_html = editor_response.get_data(as_text=True)
        self.assertIn("Dodaj prijavljenega", editor_html)
        self.assertIn('name="return_to" value="net_edit"', editor_html)

        add_response = client.post(
            f"/nets/{net_id}/participants",
            data={
                "csrf_token": "test-csrf-token",
                "return_to": "net_edit",
                "callsign": "S54CLOSED",
                "full_name": "Naknadno Dodani Član",
                "checkin_time": "20:15",
            },
        )
        self.assertEqual(add_response.status_code, 302)
        self.assertTrue(add_response.headers["Location"].endswith(f"/nets/{net_id}/edit"))

        with flask_app.app_context():
            db = get_db()
            participant = db.execute(
                "SELECT * FROM participants WHERE net_id=? AND callsign='S54CLOSED'",
                (net_id,),
            ).fetchone()
            directory_entry = db.execute(
                "SELECT * FROM callsign_directory WHERE callsign='S54CLOSED'"
            ).fetchone()
            self.assertIsNotNone(participant)
            self.assertEqual(directory_entry["full_name"], "Naknadno Dodani Član")
            participant_id = participant["id"]

        delete_response = client.post(
            f"/participants/{participant_id}/delete",
            data={"csrf_token": "test-csrf-token", "return_to": "net_edit"},
        )
        self.assertEqual(delete_response.status_code, 302)
        self.assertTrue(
            delete_response.headers["Location"].endswith(f"/nets/{net_id}/edit")
        )

        with flask_app.app_context():
            db = get_db()
            self.assertIsNone(
                db.execute(
                    "SELECT id FROM participants WHERE id=?", (participant_id,)
                ).fetchone()
            )
            leader_cursor = db.execute(
                """INSERT INTO users
                   (username, full_name, callsign, password_hash, role, active, created_at)
                   VALUES ('closed-leader', 'Testni Vodja', 'S53LEAD',
                           'not-used-in-test', 'leader', 1, ?)""",
                (now_db(),),
            )
            db.commit()
            leader_id = leader_cursor.lastrowid

        leader_client = self.authenticated_client(leader_id)
        forbidden_response = leader_client.post(
            f"/nets/{net_id}/participants",
            data={
                "csrf_token": "test-csrf-token",
                "callsign": "S52NOADMIN",
                "full_name": "Nedovoljeni Vnos",
                "checkin_time": "20:20",
            },
        )
        self.assertEqual(forbidden_response.status_code, 403)

    def test_admin_can_create_and_download_verified_backup(self):
        with flask_app.app_context():
            db = get_db()
            admin_id = db.execute(
                "SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1"
            ).fetchone()["id"]
            db.execute(
                """INSERT OR IGNORE INTO users
                   (username, full_name, callsign, password_hash, role, active, created_at)
                   VALUES ('backup-leader', 'Vodja Brez Kopij', 'S50BKP',
                           'not-used-in-test', 'leader', 1, ?)""",
                (now_db(),),
            )
            leader_id = db.execute(
                "SELECT id FROM users WHERE username='backup-leader'"
            ).fetchone()["id"]
            db.commit()

        admin_client = self.authenticated_client(admin_id)
        response = admin_client.post(
            "/backups/create", data={"csrf_token": "test-csrf-token"}
        )
        self.assertEqual(response.status_code, 302)

        page_response = admin_client.get("/backups")
        html = page_response.get_data(as_text=True)
        self.assertEqual(page_response.status_code, 200)
        self.assertIn("Varnostne kopije", html)
        self.assertIn("manual-skedi-", html)
        self.assertIn("Nova varnostna kopija", response.headers.get("Location", "") + html)

        backup_names = sorted(
            (
                name
                for name in os.listdir(os.environ["BACKUP_DIR"])
                if name.endswith(".sqlite3")
            ),
            reverse=True,
        )
        self.assertTrue(backup_names)
        download_response = admin_client.get(
            f"/backups/{backup_names[0]}/download"
        )
        self.assertEqual(download_response.status_code, 200)
        self.assertTrue(download_response.data.startswith(b"SQLite format 3"))
        self.assertIn("attachment", download_response.headers["Content-Disposition"])
        download_response.close()

        with flask_app.app_context():
            audit_row = get_db().execute(
                """SELECT details FROM audit_log
                   WHERE action='create' AND entity_type='backup'
                   ORDER BY id DESC LIMIT 1"""
            ).fetchone()
            self.assertIn("manual-skedi-", audit_row["details"])

        leader_client = self.authenticated_client(leader_id)
        self.assertEqual(leader_client.get("/backups").status_code, 403)

    def test_backup_restore_is_verified_and_keeps_safety_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "restore-test.db"
            backup_directory = Path(directory) / "copies"
            with sqlite3.connect(database_path) as connection:
                connection.execute("CREATE TABLE marker(value TEXT NOT NULL)")
                connection.execute("INSERT INTO marker(value) VALUES ('original')")

            with patch("backup.DATABASE_PATH", database_path), patch(
                "backup.BACKUP_DIR", backup_directory
            ):
                created = backup_tools.create_backup("manual")
                self.assertTrue(backup_tools.verify_database(created))
                with sqlite3.connect(database_path) as connection:
                    connection.execute("UPDATE marker SET value='changed'")

                backup_tools.restore_backup(created.name, confirmed=True)
                with sqlite3.connect(database_path) as connection:
                    value = connection.execute("SELECT value FROM marker").fetchone()[0]
                self.assertEqual(value, "original")
                self.assertTrue(
                    any(
                        item["name"].startswith("pre-restore-")
                        for item in backup_tools.list_backups()
                    )
                )

    def test_closed_net_deletion_requires_a_reason(self):
        net_id, admin_id = self.create_closed_net("Sked brez razloga za brisanje")
        client = self.authenticated_client(admin_id)

        response = client.post(
            f"/nets/{net_id}/delete-closed",
            data={"csrf_token": "test-csrf-token", "reason": "premalo"},
        )

        self.assertEqual(response.status_code, 200)
        with flask_app.app_context():
            db = get_db()
            self.assertIsNotNone(
                db.execute("SELECT id FROM nets WHERE id=?", (net_id,)).fetchone()
            )
            self.assertIsNone(
                db.execute(
                    "SELECT id FROM net_deletions WHERE original_net_id=?", (net_id,)
                ).fetchone()
            )

    def test_closed_net_deletion_keeps_reason_and_snapshot(self):
        net_id, admin_id = self.create_closed_net(
            "Podvojen zaključeni sked", with_participant=True
        )
        client = self.authenticated_client(admin_id)
        reason = "Dnevnik je bil po pomoti ustvarjen dvakrat."

        response = client.post(
            f"/nets/{net_id}/delete-closed",
            data={"csrf_token": "test-csrf-token", "reason": reason},
        )

        self.assertEqual(response.status_code, 302)
        with flask_app.app_context():
            db = get_db()
            deletion = db.execute(
                "SELECT * FROM net_deletions WHERE original_net_id=?", (net_id,)
            ).fetchone()
            audit_row = db.execute(
                """SELECT * FROM audit_log
                   WHERE action='delete' AND entity_type='net' AND entity_id=?
                   ORDER BY id DESC LIMIT 1""",
                (net_id,),
            ).fetchone()

            self.assertIsNone(
                db.execute("SELECT id FROM nets WHERE id=?", (net_id,)).fetchone()
            )
            self.assertEqual(deletion["reason"], reason)
            self.assertEqual(deletion["participant_count"], 1)
            self.assertIn("S58TEST", deletion["snapshot"])
            self.assertIn(reason, audit_row["details"])

    def test_deleted_net_can_be_reviewed_and_restored_only_once(self):
        title = "Pomotoma izbrisani sked za obnovitev"
        net_id, admin_id = self.create_closed_net(title, with_participant=True)
        with flask_app.app_context():
            db = get_db()
            db.execute(
                "UPDATE nets SET notes='Pomembna ohranjena opomba.' WHERE id=?",
                (net_id,),
            )
            db.commit()
        client = self.authenticated_client(admin_id)
        delete_response = client.post(
            f"/nets/{net_id}/delete-closed",
            data={
                "csrf_token": "test-csrf-token",
                "reason": "Sked je bil izbrisan samo zaradi testa obnovitve.",
            },
        )
        self.assertEqual(delete_response.status_code, 302)

        with flask_app.app_context():
            db = get_db()
            deletion = db.execute(
                """SELECT * FROM net_deletions WHERE original_net_id=?
                   ORDER BY id DESC LIMIT 1""",
                (net_id,),
            ).fetchone()
            deletion_id = deletion["id"]
            self.assertIsNone(deletion["restored_at"])

        trash_html = client.get("/deleted-nets").get_data(as_text=True)
        detail_html = client.get(
            f"/deleted-nets/{deletion_id}"
        ).get_data(as_text=True)
        self.assertIn(title, trash_html)
        self.assertIn("Obnovi sked in prijavljene", detail_html)
        self.assertIn("S58TEST", detail_html)

        restore_response = client.post(
            f"/deleted-nets/{deletion_id}/restore",
            data={"csrf_token": "test-csrf-token"},
        )
        self.assertEqual(restore_response.status_code, 302)

        with flask_app.app_context():
            db = get_db()
            deletion = db.execute(
                "SELECT * FROM net_deletions WHERE id=?", (deletion_id,)
            ).fetchone()
            restored_net = db.execute(
                "SELECT * FROM nets WHERE id=?", (deletion["restored_net_id"],)
            ).fetchone()
            restored_participant = db.execute(
                "SELECT * FROM participants WHERE net_id=?",
                (deletion["restored_net_id"],),
            ).fetchone()
            audit_row = db.execute(
                """SELECT details FROM audit_log
                   WHERE action='restore' AND entity_type='net' AND entity_id=?
                   ORDER BY id DESC LIMIT 1""",
                (deletion["restored_net_id"],),
            ).fetchone()
            self.assertIsNotNone(deletion["restored_at"])
            self.assertEqual(deletion["restored_by"], admin_id)
            self.assertEqual(restored_net["title"], title)
            self.assertEqual(restored_net["status"], "closed")
            self.assertEqual(restored_net["notes"], "Pomembna ohranjena opomba.")
            self.assertEqual(restored_participant["callsign"], "S58TEST")
            self.assertIn(f'"deletion_id": {deletion_id}', audit_row["details"])

        second_restore = client.post(
            f"/deleted-nets/{deletion_id}/restore",
            data={"csrf_token": "test-csrf-token"},
        )
        self.assertEqual(second_restore.status_code, 302)
        with flask_app.app_context():
            count = get_db().execute(
                "SELECT COUNT(*) AS n FROM nets WHERE title=?", (title,)
            ).fetchone()["n"]
            self.assertEqual(count, 1)


    def test_admin_can_cancel_and_restore_regular_net(self):
        original_date = "2027-01-02"
        with flask_app.app_context():
            db = get_db()
            db.execute(
                "DELETE FROM schedule_exceptions WHERE schedule_type=? AND scheduled_date=?",
                (SCHEDULE_SATURDAY, original_date),
            )
            db.execute(
                """DELETE FROM nets WHERE schedule_type=?
                   AND COALESCE(scheduled_date,net_date)=?""",
                (SCHEDULE_SATURDAY, original_date),
            )
            admin_id = db.execute(
                "SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1"
            ).fetchone()["id"]
            db.commit()

        client = self.authenticated_client(admin_id)
        response = client.post(
            f"/schedule/{SCHEDULE_SATURDAY}/{original_date}/exception",
            data={
                "csrf_token": "test-csrf-token",
                "action": "canceled",
                "reason": "Repetitor zaradi vzdrževanja ne bo dosegljiv.",
            },
        )
        self.assertEqual(response.status_code, 302)

        with flask_app.app_context():
            next_saturday = next_effective_saturday_net(datetime(2027, 1, 1, 12, 0))
            self.assertEqual(next_saturday["date"], "2027-01-09")
            audit_row = get_db().execute(
                """SELECT details FROM audit_log
                   WHERE action='create' AND entity_type='schedule_exception'
                   ORDER BY id DESC LIMIT 1"""
            ).fetchone()
            self.assertIn("vzdrževanja", audit_row["details"])

        with patch("app.now_local", return_value=datetime(2027, 1, 1, 12, 0)):
            dashboard = client.get("/").get_data(as_text=True)
        self.assertIn("Odpovedan", dashboard)
        self.assertIn("Repetitor zaradi vzdrževanja", dashboard)
        self.assertNotIn(
            f'name="scheduled_date" value="{original_date}"', dashboard
        )

        restore_response = client.post(
            f"/schedule/{SCHEDULE_SATURDAY}/{original_date}/exception/delete",
            data={"csrf_token": "test-csrf-token"},
        )
        self.assertEqual(restore_response.status_code, 302)
        with flask_app.app_context():
            self.assertIsNone(
                get_db().execute(
                    """SELECT id FROM schedule_exceptions
                       WHERE schedule_type=? AND scheduled_date=?""",
                    (SCHEDULE_SATURDAY, original_date),
                ).fetchone()
            )

    def test_postponed_net_keeps_original_saturday_number(self):
        original_date = "2027-01-02"
        new_date = "2027-01-03"
        with flask_app.app_context():
            db = get_db()
            db.execute(
                "DELETE FROM schedule_exceptions WHERE schedule_type=? AND scheduled_date=?",
                (SCHEDULE_SATURDAY, original_date),
            )
            db.execute(
                """DELETE FROM nets WHERE schedule_type=?
                   AND COALESCE(scheduled_date,net_date)=?""",
                (SCHEDULE_SATURDAY, original_date),
            )
            admin_id = db.execute(
                "SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1"
            ).fetchone()["id"]
            db.commit()

        client = self.authenticated_client(admin_id)
        response = client.post(
            f"/schedule/{SCHEDULE_SATURDAY}/{original_date}/exception",
            data={
                "csrf_token": "test-csrf-token",
                "action": "postponed",
                "new_date": new_date,
                "new_time": "20:30",
                "reason": "Termin je prestavljen zaradi klubskega dogodka.",
            },
        )
        self.assertEqual(response.status_code, 302)

        with flask_app.app_context():
            countdown = next_effective_countdown_net(datetime(2027, 1, 2, 20, 1))
            self.assertEqual(countdown["date"], new_date)
            self.assertEqual(countdown["time"], "20:30")
            self.assertEqual(
                countdown["sequence_number"],
                saturday_net_number(date.fromisoformat(original_date)),
            )

        with patch("app.now_local", return_value=datetime(2027, 1, 2, 12, 0)):
            open_response = client.post(
                "/nets/new",
                data={
                    "csrf_token": "test-csrf-token",
                    "schedule_type": SCHEDULE_SATURDAY,
                    "scheduled_date": original_date,
                    "net_date": new_date,
                    "started_time": "20:30",
                },
            )
        self.assertEqual(open_response.status_code, 302)

        with flask_app.app_context():
            created = get_db().execute(
                """SELECT * FROM nets WHERE schedule_type=?
                   AND scheduled_date=?""",
                (SCHEDULE_SATURDAY, original_date),
            ).fetchone()
            self.assertEqual(created["net_date"], new_date)
            self.assertEqual(created["started_at"], f"{new_date} 20:30:00")
            self.assertIn(
                f"št. {saturday_net_number(date.fromisoformat(original_date))}",
                created["title"],
            )
            self.assertIn("prestavljen", created["title"])


    def test_statistics_csv_and_print_report_use_same_filters(self):
        net_id, admin_id = self.create_closed_net(
            "Statistični testni sked", with_participant=True
        )
        with flask_app.app_context():
            db = get_db()
            db.execute(
                """UPDATE nets SET net_date='2040-03-02',
                   started_at='2040-03-02 20:00:00',
                   ended_at='2040-03-02 20:30:00',
                   notes='Opomba v izvozu in poročilu.' WHERE id=?""",
                (net_id,),
            )
            db.execute(
                "UPDATE participants SET checkin_at='2040-03-02 20:05:00' WHERE net_id=?",
                (net_id,),
            )
            db.commit()

        client = self.authenticated_client(admin_id)
        query = "from_date=2040-03-01&to_date=2040-03-31&status=closed"
        statistics_response = client.get(f"/statistics?{query}")
        statistics_html = statistics_response.get_data(as_text=True)
        self.assertEqual(statistics_response.status_code, 200)
        self.assertIn("Statistika skedov", statistics_html)
        self.assertIn("2040-03", statistics_html)
        self.assertIn("S58TEST", statistics_html)
        self.assertIn("Izvozi CSV", statistics_html)

        csv_response = client.get(f"/reports/export.csv?{query}")
        csv_text = csv_response.get_data(as_text=True)
        self.assertEqual(csv_response.status_code, 200)
        self.assertIn("text/csv", csv_response.content_type)
        self.assertTrue(csv_text.startswith("\ufeff"))
        self.assertIn("Statistični testni sked", csv_text)
        self.assertIn("S58TEST", csv_text)
        self.assertIn("Opomba v izvozu in poročilu.", csv_text)

        print_response = client.get(f"/reports/print?{query}")
        print_html = print_response.get_data(as_text=True)
        self.assertEqual(print_response.status_code, 200)
        self.assertIn("Poročilo skedov Radiokluba Sevnica", print_html)
        self.assertIn("Statistični testni sked", print_html)
        self.assertIn("Opomba v izvozu in poročilu.", print_html)
        self.assertIn("Shrani kot PDF", print_html)

    def test_audit_view_is_filterable_and_admin_only(self):
        with flask_app.app_context():
            db = get_db()
            admin_id = db.execute(
                "SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1"
            ).fetchone()["id"]
            db.execute(
                """INSERT INTO audit_log
                   (user_id, action, entity_type, entity_id, details, created_at)
                   VALUES (?, 'report_test', 'report', 987654,
                           'Edinstven revizijski preizkus', '2041-04-05 12:00:00')""",
                (admin_id,),
            )
            db.execute(
                """INSERT OR IGNORE INTO users
                   (username, full_name, callsign, password_hash, role, active, created_at)
                   VALUES ('audit-leader', 'Revizijski Vodja', 'S50AUD',
                           'not-used-in-test', 'leader', 1, ?)""",
                (now_db(),),
            )
            leader_id = db.execute(
                "SELECT id FROM users WHERE username='audit-leader'"
            ).fetchone()["id"]
            db.commit()

        admin_client = self.authenticated_client(admin_id)
        response = admin_client.get(
            "/audit?action=report_test&entity_type=report&from_date=2041-04-05&to_date=2041-04-05"
        )
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Revizijska sled", html)
        self.assertIn("Edinstven revizijski preizkus", html)
        self.assertIn("#987654", html)

        leader_client = self.authenticated_client(leader_id)
        self.assertEqual(leader_client.get("/audit").status_code, 403)
        self.assertEqual(leader_client.get("/reports/export.csv").status_code, 403)

    def test_only_net_owner_or_admin_can_manage_an_open_net(self):
        with flask_app.app_context():
            db = get_db()
            for username, callsign in (("owner", "S50OWN"), ("other", "S50OTH")):
                db.execute(
                    """INSERT INTO users
                       (username, full_name, callsign, password_hash, role,
                        active, created_at)
                       VALUES (?, ?, ?, 'not-used', 'leader', 1, ?)""",
                    (username, username.title(), callsign, now_db()),
                )
            owner_id = db.execute(
                "SELECT id FROM users WHERE username='owner'"
            ).fetchone()["id"]
            other_id = db.execute(
                "SELECT id FROM users WHERE username='other'"
            ).fetchone()["id"]
            net_id = db.execute(
                """INSERT INTO nets
                   (title, net_date, started_at, status, leader_id, created_at)
                   VALUES ('Lastniški sked', '2042-01-04',
                           '2042-01-04 20:00:00', 'open', ?, ?)""",
                (owner_id, now_db()),
            ).lastrowid
            db.commit()

        other = self.authenticated_client(other_id)
        denied_add = other.post(
            f"/nets/{net_id}/participants",
            data={
                "csrf_token": "test-csrf-token",
                "callsign": "S51DEN",
                "full_name": "Nedovoljeni vnos",
                "checkin_time": "20:05",
            },
        )
        denied_close = other.post(
            f"/nets/{net_id}/close", data={"csrf_token": "test-csrf-token"}
        )
        self.assertEqual(denied_add.status_code, 403)
        self.assertEqual(denied_close.status_code, 403)

        owner = self.authenticated_client(owner_id)
        allowed = owner.post(
            f"/nets/{net_id}/participants",
            data={
                "csrf_token": "test-csrf-token",
                "callsign": "S51YES",
                "full_name": "Dovoljeni vnos",
                "checkin_time": "20:05",
            },
        )
        self.assertEqual(allowed.status_code, 302)

    def test_last_active_admin_cannot_be_demoted(self):
        with flask_app.app_context():
            admin = get_db().execute(
                "SELECT * FROM users WHERE role='admin' AND active=1 LIMIT 1"
            ).fetchone()
            admin_id = admin["id"]
        client = self.authenticated_client(admin_id)
        response = client.post(
            f"/users/{admin_id}/edit",
            data={
                "csrf_token": "test-csrf-token",
                "full_name": admin["full_name"],
                "callsign": admin["callsign"],
                "role": "leader",
                "active": "1",
                "password": "",
            },
            follow_redirects=True,
        )
        self.assertIn("vsaj en aktiven administrator", response.get_data(as_text=True))
        with flask_app.app_context():
            role = get_db().execute(
                "SELECT role FROM users WHERE id=?", (admin_id,)
            ).fetchone()["role"]
            self.assertEqual(role, "admin")

    def test_new_user_must_change_temporary_password(self):
        with flask_app.app_context():
            admin_id = get_db().execute(
                "SELECT id FROM users WHERE role='admin' LIMIT 1"
            ).fetchone()["id"]
        admin = self.authenticated_client(admin_id)
        created = admin.post(
            "/users/new",
            data={
                "csrf_token": "test-csrf-token",
                "username": "temporary-user",
                "full_name": "Začasni Uporabnik",
                "callsign": "S50TMP",
                "role": "leader",
                "password": "temporary-pass-123",
            },
        )
        self.assertEqual(created.status_code, 302)
        with flask_app.app_context():
            user = get_db().execute(
                "SELECT * FROM users WHERE username='temporary-user'"
            ).fetchone()
            self.assertEqual(user["must_change_password"], 1)
        response = self.authenticated_client(user["id"]).get("/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/change-password"))

    def test_public_schedule_and_calendar_are_available_without_login(self):
        client = flask_app.test_client()
        schedule = client.get("/urnik")
        calendar = client.get("/urnik.ics")
        self.assertEqual(schedule.status_code, 200)
        self.assertIn("Javni urnik skedov", schedule.get_data(as_text=True))
        self.assertEqual(calendar.status_code, 200)
        self.assertIn("text/calendar", calendar.content_type)
        self.assertIn("BEGIN:VCALENDAR", calendar.get_data(as_text=True))
        self.assertIn("S50TTT", calendar.get_data(as_text=True))

    def test_owner_can_undo_last_participant(self):
        net_id, admin_id = self.create_open_net("Razveljavitev prijave")
        client = self.authenticated_client(admin_id)
        client.post(
            f"/nets/{net_id}/participants",
            data={
                "csrf_token": "test-csrf-token",
                "callsign": "S50UNDO",
                "full_name": "Zadnji Vnos",
                "checkin_time": "20:10",
            },
        )
        response = client.post(
            f"/nets/{net_id}/participants/undo-last",
            data={"csrf_token": "test-csrf-token"},
        )
        self.assertEqual(response.status_code, 302)
        with flask_app.app_context():
            db = get_db()
            count = db.execute(
                "SELECT COUNT(*) AS n FROM participants WHERE net_id=?", (net_id,)
            ).fetchone()["n"]
            audit_row = db.execute(
                """SELECT id FROM audit_log WHERE action='undo'
                   AND entity_type='participant' ORDER BY id DESC LIMIT 1"""
            ).fetchone()
            self.assertEqual(count, 0)
            self.assertIsNotNone(audit_row)

    def test_similar_callsign_produces_non_blocking_warning(self):
        net_id, admin_id = self.create_open_net("Opozorilo klicnega znaka")
        with flask_app.app_context():
            db = get_db()
            db.execute(
                """INSERT INTO callsign_directory
                   (callsign, full_name, active, use_count, created_by, created_at)
                   VALUES ('S57ZM', 'Znani Operater', 1, 0, ?, ?)""",
                (admin_id, now_db()),
            )
            db.commit()
        response = self.authenticated_client(admin_id).post(
            f"/nets/{net_id}/participants",
            data={
                "csrf_token": "test-csrf-token",
                "callsign": "S57ZN",
                "full_name": "Možen tipkarski vnos",
                "checkin_time": "20:10",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("podoben je S57ZM", response.get_data(as_text=True))

    def test_health_and_security_headers_report_real_state(self):
        response = flask_app.test_client().get("/health")
        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["database"], "ok")
        self.assertEqual(payload["schema_version"], payload["schema_latest"])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("default-src", response.headers["Content-Security-Policy"])

    def test_backup_retention_is_separate_by_kind(self):
        backup_tools.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        names = [
            "auto-skedi-20260101-000000-000001.sqlite3",
            "auto-skedi-20260102-000000-000001.sqlite3",
            "auto-skedi-20260103-000000-000001.sqlite3",
            "manual-skedi-20260101-000000-000001.sqlite3",
            "manual-skedi-20260102-000000-000001.sqlite3",
            "pre-import-skedi-20260101-000000-000001.sqlite3",
            "pre-restore-skedi-20260102-000000-000001.sqlite3",
        ]
        for index, name in enumerate(names):
            path = backup_tools.BACKUP_DIR / name
            path.write_bytes(b"test")
            os.utime(path, (index + 1, index + 1))
        with patch("backup.BACKUP_AUTO_RETENTION", 2), patch(
            "backup.BACKUP_MANUAL_RETENTION", 1
        ), patch("backup.BACKUP_SAFETY_RETENTION", 1):
            backup_tools.prune_backups()
        remaining = [item["name"] for item in backup_tools.list_backups()]
        self.assertEqual(sum(name.startswith("auto-") for name in remaining), 2)
        self.assertEqual(sum(name.startswith("manual-") for name in remaining), 1)
        self.assertEqual(
            sum(name.startswith(("pre-import-", "pre-restore-")) for name in remaining),
            1,
        )

    def test_versioned_migration_preserves_an_existing_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            old_database = Path(directory) / "old-skedi.db"
            with sqlite3.connect(old_database) as db:
                db.executescript(
                    """CREATE TABLE users (
                           id INTEGER PRIMARY KEY AUTOINCREMENT,
                           username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                           full_name TEXT NOT NULL,
                           callsign TEXT NOT NULL COLLATE NOCASE,
                           password_hash TEXT NOT NULL,
                           role TEXT NOT NULL,
                           active INTEGER NOT NULL DEFAULT 1,
                           created_at TEXT NOT NULL
                       );
                       INSERT INTO users
                           (username, full_name, callsign, password_hash,
                            role, active, created_at)
                       VALUES ('existing-admin', 'Obstoječi Administrator',
                               'S50OLD', 'old-hash', 'admin', 1,
                               '2025-01-01 12:00:00');"""
                )
            with patch("app.DB_PATH", str(old_database)):
                with flask_app.app_context():
                    init_db()
                    db = get_db()
                    columns = {
                        row["name"] for row in db.execute("PRAGMA table_info(users)")
                    }
                    existing = db.execute(
                        "SELECT * FROM users WHERE username='existing-admin'"
                    ).fetchone()
                    version = db.execute(
                        "SELECT MAX(version) AS n FROM schema_migrations"
                    ).fetchone()["n"]
            self.assertIn("must_change_password", columns)
            self.assertEqual(existing["callsign"], "S50OLD")
            self.assertEqual(existing["must_change_password"], 0)
            self.assertEqual(version, 1)


if __name__ == "__main__":
    unittest.main()
