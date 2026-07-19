import os
import tempfile
import unittest
from datetime import date, datetime


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

    def post_delete_net(self, net_id, user_id):
        client = flask_app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = user_id
            session["csrf_token"] = "test-csrf-token"
        return client.post(
            f"/nets/{net_id}/delete",
            data={"csrf_token": "test-csrf-token"},
        )

    def test_health_reports_application_version(self):
        response = flask_app.test_client().get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["version"], APP_VERSION)

    def test_login_shows_countdown_and_next_saturday_number(self):
        response = flask_app.test_client().get("/login")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("data-countdown=", html)
        self.assertIn("Naslednji redni sobotni sked", html)

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


if __name__ == "__main__":
    unittest.main()
