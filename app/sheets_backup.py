"""Full, restorable Google Sheets snapshots of the SQLite ledger.

The snapshot is finalized by writing its hash-bearing manifest *last*.
An interrupted upload cannot be restored as a valid backup.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import logging
import re
from uuid import uuid4
from zoneinfo import ZoneInfo

from .storage import Storage

MAGIC = "PLUSMINUS_BACKUP_V1"
CURRENT_TITLE = "Операции"
SNAPSHOT_PATTERN = re.compile(r"^backup_(\d{8})_(\d{6})_([0-9a-f]{6})$")
HEADERS = [
    "Дата и время", "Тип", "Сумма", "Комментарий", "ID", "Telegram ID",
    "amount_minor", "operation_ts", "created_ts", "deleted_ts",
    "source_chat_id", "source_message_id", "source_line",
]
WIDTH = len(HEADERS)
# Values stay TEXT to avoid Google Sheets rounding large Telegram IDs and timestamps.
SOURCE_FIELDS = [
    "id", "user_id", "amount_minor", "comment", "operation_ts",
    "created_ts", "deleted_ts", "source_chat_id", "source_message_id", "source_line",
]


class BackupError(ValueError):
    """Invalid configuration or snapshot that cannot safely be restored."""


@dataclass(frozen=True)
class BackupResult:
    title: str
    count: int
    spreadsheet_id: str

    @property
    def url(self) -> str:
        return f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}/edit"


def _checksum(rows: list[list[str]]) -> str:
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _format_money(amount_minor: int) -> str:
    # A raw string, never a floating-point number.
    return f"{'-' if amount_minor < 0 else ''}{abs(amount_minor) // 100}.{abs(amount_minor) % 100:02d}"


def make_snapshot(rows: list[dict], tz: ZoneInfo, created: datetime) -> list[list[str]]:
    body: list[list[str]] = []
    for tx in rows:
        amount = int(tx["amount_minor"])
        body.append([
            datetime.fromtimestamp(int(tx["operation_ts"]), tz).strftime("%Y-%m-%d %H:%M:%S"),
            "+" if amount > 0 else "-",
            _format_money(amount),
            str(tx["comment"]),
            *["" if tx[field] is None else str(tx[field]) for field in SOURCE_FIELDS[:3]],
            *["" if tx[field] is None else str(tx[field]) for field in SOURCE_FIELDS[4:]],
        ])
    manifest = [MAGIC, str(len(body)), _checksum(body), created.isoformat(), tz.key]
    return [manifest, HEADERS[:], *body]


def validate_snapshot(values: list[list[object]]) -> list[dict]:
    if len(values) < 2:
        raise BackupError("Нет заголовка и метаданных резервной копии")
    manifest = [str(v) for v in values[0]]
    if len(manifest) < 5 or manifest[0] != MAGIC:
        raise BackupError("Неверная версия резервной копии или копирование не завершено")
    if [str(v) for v in values[1]] != HEADERS:
        raise BackupError("Заголовки таблицы изменены")
    try:
        expected_count = int(manifest[1])
    except ValueError as exc:
        raise BackupError("Неверное количество операций в резервной копии") from exc
    if expected_count < 0 or len(values) - 2 != expected_count:
        raise BackupError("Резервная копия неполная: число строк не совпадает")
    body = [[str(c) for c in row] + [""] * (WIDTH - len(row)) for row in values[2:]]
    if any(len(row) != WIDTH for row in body) or _checksum(body) != manifest[2]:
        raise BackupError("Контрольная сумма не совпала: таблица изменена или повреждена")
    result: list[dict] = []
    seen_ids: set[int] = set()
    for row in body:
        fields = {
            "id": row[4], "user_id": row[5], "amount_minor": row[6],
            "comment": row[3], "operation_ts": row[7], "created_ts": row[8],
            "deleted_ts": row[9], "source_chat_id": row[10],
            "source_message_id": row[11], "source_line": row[12],
        }
        try:
            for key in ("id", "user_id", "amount_minor", "operation_ts", "created_ts"):
                fields[key] = int(fields[key])
            for key in ("deleted_ts", "source_chat_id", "source_message_id", "source_line"):
                fields[key] = int(fields[key]) if fields[key] != "" else None
        except ValueError as exc:
            raise BackupError("Недопустимое числовое значение в резервной копии") from exc
        if fields["id"] <= 0 or fields["user_id"] <= 0 or fields["amount_minor"] == 0:
            raise BackupError("Недопустимый ID пользователя, операции или сумма")
        if fields["id"] in seen_ids:
            raise BackupError("Дублирующиеся ID операций")
        seen_ids.add(fields["id"])
        result.append(fields)
    return result


class SheetsBackup:
    def __init__(
        self, spreadsheet_id: str, credentials_file: str, tz: ZoneInfo,
        retention_days: int = 30, service=None,
    ):
        self.spreadsheet_id = spreadsheet_id.strip()
        self.credentials_file = credentials_file
        self.tz = tz
        self.retention_days = retention_days
        self._provided_service = service
        if not self.spreadsheet_id:
            raise BackupError("GOOGLE_SHEET_ID пуст")
        if retention_days < 1:
            raise BackupError("BACKUP_RETENTION_DAYS должен быть >= 1")

    def _service(self):
        if self._provided_service is not None:
            return self._provided_service
        if not Path(self.credentials_file).is_file():
            raise BackupError(f"Нет файла ключа Google: {self.credentials_file}")
        # Imports are isolated: without Google config the bot works without Google libraries.
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        creds = service_account.Credentials.from_service_account_file(
            self.credentials_file,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        return build("sheets", "v4", credentials=creds, cache_discovery=False)

    @staticmethod
    def _execute(request):
        return request.execute(num_retries=3)

    def _sheets(self, svc) -> list[dict]:
        info = self._execute(svc.spreadsheets().get(
            spreadsheetId=self.spreadsheet_id, fields="sheets(properties(sheetId,title))"
        ))
        return [sheet["properties"] for sheet in info.get("sheets", [])]

    def list_snapshots(self) -> list[str]:
        titles = [s["title"] for s in self._sheets(self._service())]
        return sorted((t for t in titles if SNAPSHOT_PATTERN.fullmatch(t)), reverse=True)

    def _read(self, svc, title: str) -> list[list[object]]:
        return self._execute(svc.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{title}'!A1:M",
            valueRenderOption="UNFORMATTED_VALUE",
        )).get("values", [])

    def read_verified_snapshot(self, title: str) -> list[dict]:
        if not SNAPSHOT_PATTERN.fullmatch(title):
            raise BackupError("Восстановление разрешено только из листов backup_...")
        if title not in self.list_snapshots():
            raise BackupError("Указанный лист не найден")
        return validate_snapshot(self._read(self._service(), title))

    def export(self, storage: Storage, *, now: datetime | None = None) -> BackupResult:
        now = (now or datetime.now(self.tz)).astimezone(self.tz)
        # ONE consistent read, including soft-deleted transactions and original IDs.
        rows = storage.export_all()
        values = make_snapshot(rows, self.tz, now)
        svc = self._service()
        sheet_api = svc.spreadsheets()
        known = self._sheets(svc)
        suffix = uuid4().hex[:6]
        title = f"backup_{now:%Y%m%d_%H%M%S}_{suffix}"
        if any(s["title"] == title for s in known):
            raise BackupError("Повторяющееся имя резервной копии")
        created = self._execute(sheet_api.batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {
                "title": title,
                "gridProperties": {"rowCount": max(100, len(values)), "columnCount": WIDTH,
                                   "frozenRowCount": 2},
            }}}]},
        ))
        sheet_id = created["replies"][0]["addSheet"]["properties"]["sheetId"]
        # Write all payload rows BEFORE the manifest, so incomplete sheets are invalid.
        for offset in range(1, len(values), 500):
            chunk = values[offset:offset + 500]
            self._execute(sheet_api.values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{title}'!A{offset+1}:M{offset+len(chunk)}",
                valueInputOption="RAW", body={"values": chunk},
            ))
        self._execute(sheet_api.values().update(
            spreadsheetId=self.spreadsheet_id, range=f"'{title}'!A1:E1",
            valueInputOption="RAW", body={"values": [values[0]]},
        ))
        # Don't advertise completion until a read-back has verified the full snapshot.
        validated = validate_snapshot(self._read(svc, title))
        if len(validated) != len(rows):
            raise BackupError("Не удалось проверить количество операций после выгрузки")
        # Atomic switch of the easy-to-read current view. Historical snapshots untouched.
        current = next((s for s in known if s["title"] == CURRENT_TITLE), None)
        new_id = self._unused_id(known, sheet_id)
        requests = [{"duplicateSheet": {
            "sourceSheetId": sheet_id, "newSheetId": new_id,
            "newSheetName": f"__newcurrent_{suffix}" if current else CURRENT_TITLE,
        }}]
        if current:
            requests += [
                {"deleteSheet": {"sheetId": current["sheetId"]}},
                {"updateSheetProperties": {"properties": {
                    "sheetId": new_id, "title": CURRENT_TITLE,
                }, "fields": "title"}},
            ]
        self._execute(sheet_api.batchUpdate(
            spreadsheetId=self.spreadsheet_id, body={"requests": requests},
        ))
        try:
            self._prune(svc, now)
        except Exception:
            # A snapshot verified and published is still a successful backup.
            logging.exception("Old Sheets backup cleanup failed")
        return BackupResult(title=title, count=len(rows), spreadsheet_id=self.spreadsheet_id)

    @staticmethod
    def _unused_id(known: list[dict], other: int) -> int:
        occupied = {p["sheetId"] for p in known} | {other}
        # Sheet IDs must be unique within a spreadsheet.
        from secrets import randbelow
        while True:
            candidate = randbelow(2_000_000_000) + 1
            if candidate not in occupied:
                return candidate

    def _prune(self, svc, now: datetime) -> None:
        cutoff = now.date() - timedelta(days=self.retention_days)
        deletions: list[dict] = []
        for item in self._sheets(svc):
            match = SNAPSHOT_PATTERN.fullmatch(item["title"])
            if match and datetime.strptime(match.group(1), "%Y%m%d").date() < cutoff:
                deletions.append({"deleteSheet": {"sheetId": item["sheetId"]}})
        if deletions:
            self._execute(svc.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id, body={"requests": deletions},
            ))
