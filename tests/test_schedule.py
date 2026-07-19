import os
import tempfile
import unittest
from datetime import date, datetime, timedelta


TEST_DATA = tempfile.TemporaryDirectory()
os.environ["DATABASE_PATH"] = os.path.join(TEST_DATA.name, "test-skedi.db")
os.environ["ADMIN_PASSWORD"] = "test-password-123"
os.environ["SECRET_KEY"] = "test-secret-key"

from app import (  # noqa: E402
    APP_VERSION,
    SCHEDULE_MONTHLY,
    SCHEDULE_SATURDAY,
    app as flask_app,
    get_db,
    next_countdown_net,
    next_scheduled_nets,
    next_saturday_net,
    now_db,
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


if __name__ == "__main__":
    unittest.main()
