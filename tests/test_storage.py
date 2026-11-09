import os
import tempfile
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from app.parser import parse_message
from app.storage import Storage, period_bounds


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Storage(os.path.join(self.temp.name, "db.sqlite3"), ZoneInfo("UTC"))
        self.db.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def test_add_report_edit_undo_per_user(self):
        first = self.db.add_entries(101, 101, 1, parse_message("+100\n-30 материалы"))
        self.assertEqual(self.db.period_totals(101, None, None).net, 7000)
        self.assertEqual(self.db.period_totals(102, None, None).count, 0)
        self.assertFalse(self.db.delete(102, first[0]))
        self.assertTrue(self.db.edit(101, first[1], parse_message("-50 аренда")[0]))
        self.assertEqual(self.db.period_totals(101, None, None).net, 5000)
        self.assertEqual(first, self.db.add_entries(101, 101, 1, parse_message("+100\n-30 материалы")))
        self.assertEqual(self.db.period_totals(101, None, None).count, 2)
        row = self.db.undo_last(101)
        self.assertEqual(row["id"], first[1])
        self.assertEqual(self.db.period_totals(101, None, None).net, 10000)

    def test_dates(self):
        tz = ZoneInfo("Europe/Zurich")
        now = datetime(2026, 11, 9, 12, 0, tzinfo=tz)
        start, end = period_bounds("week", tz, now)
        self.assertEqual(datetime.fromtimestamp(start, tz).day, 9)
        self.assertEqual(datetime.fromtimestamp(end, tz).day, 16)
        start, end = period_bounds("month", tz, now)
        self.assertEqual(datetime.fromtimestamp(start, tz).day, 1)
        self.assertEqual(datetime.fromtimestamp(end, tz).month, 12)
