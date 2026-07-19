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
    next_countdown_net,
    next_scheduled_nets,
    saturday_net_number,
    saturday_start_time,
    scheduled_net_for_date,
)


class ScheduleTests(unittest.TestCase):
    def test_health_reports_application_version(self):
        response = flask_app.test_client().get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["version"], APP_VERSION)

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


if __name__ == "__main__":
    unittest.main()
