"""Offline, dependency-free Sheets API simulation tests."""
from copy import deepcopy
from datetime import datetime, timedelta
import os
import tempfile
import unittest
from zoneinfo import ZoneInfo

from app.parser import parse_message
from app.sheets_backup import (
    BackupError, CURRENT_TITLE, SheetsBackup, make_snapshot, validate_snapshot,
)
from app.storage import Storage


class Request:
    def __init__(self, callback):
        self.callback = callback

    def execute(self, num_retries=0):
        return self.callback()


class FakeSheets:
    def __init__(self):
        self.tabs = {}
        self.next_id = 100

    def spreadsheets(self):
        return self

    def values(self):
        return FakeValues(self)

    def get(self, **kwargs):
        def do():
            return {"sheets": [{"properties": {"sheetId": t["id"], "title": title}}
                               for title, t in self.tabs.items()]}
        return Request(do)

    def batchUpdate(self, body, **kwargs):
        def do():
            replies = []
            for request in body["requests"]:
                if "addSheet" in request:
                    title = request["addSheet"]["properties"]["title"]
                    self.next_id += 1
                    self.tabs[title] = {"id": self.next_id, "values": []}
                    replies.append({"addSheet": {"properties": {"sheetId": self.next_id}}})
                elif "duplicateSheet" in request:
                    q = request["duplicateSheet"]
                    source = next(t for t in self.tabs.values() if t["id"] == q["sourceSheetId"])
                    self.tabs[q["newSheetName"]] = {"id": q["newSheetId"], "values": deepcopy(source["values"])}
                    replies.append({})
                elif "deleteSheet" in request:
                    target = request["deleteSheet"]["sheetId"]
                    name = next(k for k, v in self.tabs.items() if v["id"] == target)
                    del self.tabs[name]
                    replies.append({})
                elif "updateSheetProperties" in request:
                    q = request["updateSheetProperties"]["properties"]
                    name = next(k for k, v in self.tabs.items() if v["id"] == q["sheetId"])
                    self.tabs[q["title"]] = self.tabs.pop(name)
                    replies.append({})
                else:
                    raise AssertionError(request)
            return {"replies": replies}
        return Request(do)


class FakeValues:
    def __init__(self, parent):
        self.parent = parent

    def update(self, range, body, **kwargs):
        def do():
            name, cells = range.split("!", 1)
            name = name.strip("'")
            start = int("".join(c for c in cells.split(":")[0] if c.isdigit())) - 1
            values = self.parent.tabs[name]["values"]
            for row_idx, new_row in enumerate(body["values"], start=start):
                while len(values) <= row_idx:
                    values.append([])
                values[row_idx] = deepcopy(new_row)
            return {"updatedRows": len(body["values"])}
        return Request(do)

    def get(self, range, **kwargs):
        def do():
            name = range.split("!", 1)[0].strip("'")
            return {"values": deepcopy(self.parent.tabs[name]["values"])}
        return Request(do)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.tz = ZoneInfo("Europe/Zurich")
        self.store = Storage(os.path.join(self.temp.name, "db.sqlite3"), self.tz)
        self.store.initialize()
        self.fake = FakeSheets()
        self.backup = SheetsBackup("fake-id", "unused", self.tz, service=self.fake)

    def tearDown(self):
        self.temp.cleanup()

    def test_export_soft_deleted_edit_recover_and_new_ids(self):
        ids = self.store.add_entries(101, 101, 5, parse_message("+100 ногти\n-50 аренда"))
        self.store.add_entries(202, 202, 8, parse_message("+250"))
        self.store.edit(101, ids[0], parse_message("+120 ногти")[0])
        self.store.delete(101, ids[1])
        snapshot = self.backup.export(self.store, now=datetime(2026, 11, 9, 4, 0, tzinfo=self.tz))
        self.assertIn(CURRENT_TITLE, self.fake.tabs)
        self.assertEqual(snapshot.count, 3)
        verified = self.backup.read_verified_snapshot(snapshot.title)
        self.assertEqual(verified, self.store.export_all())
        self.assertEqual(self.store.period_totals(101, None, None).net, 12000)

        self.store.add_entries(101, 101, 100, parse_message("-900 ошибка"))
        self.store.restore_all(verified)
        self.assertEqual(self.store.period_totals(101, None, None).net, 12000)
        self.assertEqual(self.store.period_totals(202, None, None).net, 25000)
        self.assertEqual(self.store.export_all(), verified)
        self.assertEqual(self.store.add_entries(101, 101, 101, parse_message("+5"))[0], 4)

    def test_snapshot_detects_changes_and_incomplete_upload(self):
        self.store.add_entries(1, 1, 1, parse_message("+20 =SUM(A1:A20)"))
        snapshot = self.backup.export(self.store, now=datetime(2026, 11, 9, tzinfo=self.tz))
        self.fake.tabs[snapshot.title]["values"][2][3] = "broken"
        with self.assertRaises(BackupError):
            self.backup.read_verified_snapshot(snapshot.title)
        original = make_snapshot(self.store.export_all(), self.tz, datetime.now(self.tz))
        original[0] = []
        with self.assertRaises(BackupError):
            validate_snapshot(original)
        self.assertNotIn("broken", str(self.store.export_all()))

    def test_multiple_snapshots_and_retention(self):
        day1 = datetime(2026, 10, 1, tzinfo=self.tz)
        first = self.backup.export(self.store, now=day1)
        second = self.backup.export(self.store, now=day1 + timedelta(days=40))
        self.assertNotIn(first.title, self.fake.tabs)
        self.assertIn(second.title, self.fake.tabs)
        self.assertIn(CURRENT_TITLE, self.fake.tabs)
        self.assertEqual(self.backup.list_snapshots(), [second.title])

    def test_invalid_restore_rejected_without_touching_db(self):
        self.store.add_entries(101, 101, 1, parse_message("+100"))
        with self.assertRaises(BackupError):
            self.backup.read_verified_snapshot("Операции")
        self.assertEqual(self.store.period_totals(101, None, None).income, 10000)


if __name__ == "__main__":
    unittest.main()
