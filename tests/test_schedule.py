import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch


TEST_DATA = tempfile.TemporaryDirectory()
os.environ["DATABASE_PATH"] = os.path.join(TEST_DATA.name, "test-skedi.db")
os.environ["ADMIN_PASSWORD"] = "test-password-123"
os.environ["SECRET_KEY"] = "test-secret-key"

from app import (  # noqa: E402
    APP_VERSION,
    RELEASE_CHANNEL,
    SCHEDULE_MONTHLY,
    SCHEDULE_SATURDAY,
    app as flask_app,
    get_db,
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


class ScheduleTests(unittest.TestCase):
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

    def test_alpha_channel_has_visible_warning(self):
        with patch("app.RELEASE_CHANNEL", "alpha"), patch(
            "app.APP_VERSION", "1.13.0-alpha"
        ):
            response = flask_app.test_client().get("/login")
            health_response = flask_app.test_client().get("/health")

        html = response.get_data(as_text=True)
        self.assertIn("ALPHA TESTNA RAZLIČICA", html)
        self.assertIn("podatki niso produkcijski", html)
        self.assertIn("različica 1.13.0-alpha", html)
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
            self.assertIn("Ti si pravi Heker", dashboard_html)

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
                   ended_at='2040-03-02 20:30:00' WHERE id=?""",
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

        print_response = client.get(f"/reports/print?{query}")
        print_html = print_response.get_data(as_text=True)
        self.assertEqual(print_response.status_code, 200)
        self.assertIn("Poročilo skedov Radiokluba Sevnica", print_html)
        self.assertIn("Statistični testni sked", print_html)
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


if __name__ == "__main__":
    unittest.main()
