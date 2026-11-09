"""Small SQLite ledger with per-user access control."""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager, closing
from collections.abc import Iterator
from datetime import datetime, timedelta, time
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from .parser import Entry


@dataclass(frozen=True)
class Totals:
    income: int
    expenses: int  # non-negative total expenses
    count: int

    @property
    def net(self) -> int:
        return self.income - self.expenses


class Storage:
    def __init__(self, path: str, tz: ZoneInfo):
        self.path = path
        self.tz = tz

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    amount_minor INTEGER NOT NULL CHECK(amount_minor != 0),
                    comment TEXT NOT NULL DEFAULT '',
                    operation_ts INTEGER NOT NULL,
                    created_ts INTEGER NOT NULL,
                    deleted_ts INTEGER,
                    source_chat_id INTEGER,
                    source_message_id INTEGER,
                    source_line INTEGER,
                    UNIQUE (source_chat_id, source_message_id, source_line)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tx_user_time ON transactions (user_id, operation_ts)")
            conn.execute("""CREATE TABLE IF NOT EXISTS backup_state
                         (key TEXT PRIMARY KEY, value TEXT NOT NULL)""")

    def add_entries(
        self, user_id: int, chat_id: int, message_id: int, entries: list[Entry]
    ) -> list[int]:
        now = datetime.now(self.tz)
        created_ts = int(now.timestamp())
        ids: list[int] = []
        with self._connection() as conn:
            for line, entry in enumerate(entries):
                date = now.date() - timedelta(days=1) if entry.yesterday else now.date()
                operation_ts = int(datetime.combine(date, now.timetz().replace(tzinfo=None), self.tz).timestamp())
                conn.execute(
                    """INSERT OR IGNORE INTO transactions
                        (user_id, amount_minor, comment, operation_ts, created_ts,
                         source_chat_id, source_message_id, source_line)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (user_id, entry.amount_minor, entry.comment, operation_ts, created_ts,
                     chat_id, message_id, line),
                )
                stored = conn.execute(
                    """SELECT id FROM transactions
                       WHERE source_chat_id = ? AND source_message_id = ? AND source_line = ?""",
                    (chat_id, message_id, line),
                ).fetchone()
                ids.append(stored["id"])
        return ids

    def get(self, user_id: int, transaction_id: int) -> sqlite3.Row | None:
        with self._connection() as conn:
            return conn.execute(
                "SELECT * FROM transactions WHERE id = ? AND user_id = ? AND deleted_ts IS NULL",
                (transaction_id, user_id),
            ).fetchone()

    def delete(self, user_id: int, transaction_id: int) -> bool:
        with self._connection() as conn:
            cur = conn.execute(
                "UPDATE transactions SET deleted_ts = ? WHERE id = ? AND user_id = ? AND deleted_ts IS NULL",
                (int(datetime.now(self.tz).timestamp()), transaction_id, user_id),
            )
            return cur.rowcount == 1

    def undo_last(self, user_id: int) -> sqlite3.Row | None:
        with self._connection() as conn:
            row = conn.execute(
                """SELECT * FROM transactions WHERE user_id = ? AND deleted_ts IS NULL
                   ORDER BY id DESC LIMIT 1""",
                (user_id,),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE transactions SET deleted_ts = ? WHERE id = ?",
                    (int(datetime.now(self.tz).timestamp()), row["id"]),
                )
            return row

    def edit(self, user_id: int, transaction_id: int, entry: Entry) -> bool:
        old = self.get(user_id, transaction_id)
        if not old:
            return False
        operation_ts = old["operation_ts"]
        if entry.yesterday:
            now = datetime.now(self.tz)
            operation_ts = int(datetime.combine(now.date() - timedelta(days=1), time(now.hour, now.minute, now.second), self.tz).timestamp())
        with self._connection() as conn:
            cur = conn.execute(
                """UPDATE transactions SET amount_minor = ?, comment = ?, operation_ts = ?
                   WHERE id = ? AND user_id = ? AND deleted_ts IS NULL""",
                (entry.amount_minor, entry.comment, operation_ts, transaction_id, user_id),
            )
            return cur.rowcount == 1

    def period_totals(self, user_id: int, start: int | None, end: int | None) -> Totals:
        conditions = ["user_id = ?", "deleted_ts IS NULL"]
        values: list[int] = [user_id]
        if start is not None:
            conditions.append("operation_ts >= ?")
            values.append(start)
        if end is not None:
            conditions.append("operation_ts < ?")
            values.append(end)
        where = " AND ".join(conditions)
        with self._connection() as conn:
            row = conn.execute(
                f"""SELECT COALESCE(SUM(CASE WHEN amount_minor > 0 THEN amount_minor ELSE 0 END), 0) AS income,
                           COALESCE(SUM(CASE WHEN amount_minor < 0 THEN -amount_minor ELSE 0 END), 0) AS expenses,
                           COUNT(*) AS count FROM transactions WHERE {where}""",
                values,
            ).fetchone()
        return Totals(row["income"], row["expenses"], row["count"])

    def period_totals_many(
        self, user_ids: tuple[int, ...], start: int | None, end: int | None
    ) -> dict[int, Totals]:
        """Admin read-only report for an explicit list of allowed Telegram IDs."""
        if not user_ids:
            return {}
        placeholders = ", ".join("?" for _ in user_ids)
        conditions = [f"user_id IN ({placeholders})", "deleted_ts IS NULL"]
        params: list[int] = list(user_ids)
        if start is not None:
            conditions.append("operation_ts >= ?")
            params.append(start)
        if end is not None:
            conditions.append("operation_ts < ?")
            params.append(end)
        query = f"""SELECT user_id,
                   COALESCE(SUM(CASE WHEN amount_minor > 0 THEN amount_minor ELSE 0 END), 0) AS income,
                   COALESCE(SUM(CASE WHEN amount_minor < 0 THEN -amount_minor ELSE 0 END), 0) AS expenses,
                   COUNT(*) AS count
                   FROM transactions WHERE {' AND '.join(conditions)} GROUP BY user_id"""
        result = {user_id: Totals(0, 0, 0) for user_id in user_ids}
        with self._connection() as conn:
            for row in conn.execute(query, params):
                result[row["user_id"]] = Totals(row["income"], row["expenses"], row["count"])
        return result

    def recent(self, user_id: int, limit: int = 10) -> list[sqlite3.Row]:
        with self._connection() as conn:
            return conn.execute(
                """SELECT * FROM transactions WHERE user_id = ? AND deleted_ts IS NULL
                   ORDER BY id DESC LIMIT ?""",
                (user_id, limit),
            ).fetchall()

    def get_last_backup_day(self) -> str | None:
        with self._connection() as conn:
            row = conn.execute("SELECT value FROM backup_state WHERE key = 'last_backup_day'").fetchone()
            return row["value"] if row else None

    def set_last_backup_day(self, day: str) -> None:
        with self._connection() as conn:
            conn.execute("""INSERT INTO backup_state(key, value) VALUES('last_backup_day', ?)
                         ON CONFLICT(key) DO UPDATE SET value = excluded.value""", (day,))

    def export_all(self) -> list[dict]:
        """Export ALL rows, including soft-deleted entries, for complete recovery."""
        with self._connection() as conn:
            return [dict(row) for row in conn.execute(
                """SELECT id, user_id, amount_minor, comment, operation_ts, created_ts,
                          deleted_ts, source_chat_id, source_message_id, source_line
                   FROM transactions ORDER BY id"""
            ).fetchall()]

    def create_sqlite_backup(self, destination: str | Path) -> None:
        """SQLite Online Backup API, safe for a WAL database."""
        with closing(sqlite3.connect(self.path, timeout=10)) as source:
            with closing(sqlite3.connect(str(destination))) as target:
                source.backup(target)

    def restore_all(self, rows: list[dict]) -> None:
        """Replace records atomically after the caller has verified a remote snapshot."""
        columns = (
            "id", "user_id", "amount_minor", "comment", "operation_ts", "created_ts",
            "deleted_ts", "source_chat_id", "source_message_id", "source_line",
        )
        with self._connection() as conn:
            conn.execute("DELETE FROM transactions")
            conn.executemany(
                """INSERT INTO transactions
                   (id, user_id, amount_minor, comment, operation_ts, created_ts,
                    deleted_ts, source_chat_id, source_message_id, source_line)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ([row[c] for c in columns] for row in rows),
            )
            conn.execute("DELETE FROM sqlite_sequence WHERE name = 'transactions'")
            conn.execute("DELETE FROM backup_state WHERE key = 'last_backup_day'")


def period_bounds(period: str, tz: ZoneInfo, now: datetime | None = None) -> tuple[int | None, int | None]:
    now = now or datetime.now(tz)
    date = now.astimezone(tz).date()
    if period == "today":
        start_date, end_date = date, date + timedelta(days=1)
    elif period == "week":
        start_date = date - timedelta(days=date.weekday())
        end_date = start_date + timedelta(days=7)
    elif period == "month":
        start_date = date.replace(day=1)
        end_date = start_date.replace(year=start_date.year + 1, month=1) if start_date.month == 12 else start_date.replace(month=start_date.month + 1)
    elif period == "all":
        return None, None
    else:
        raise ValueError(f"Unknown period: {period}")

    return (
        int(datetime.combine(start_date, time.min, tzinfo=tz).timestamp()),
        int(datetime.combine(end_date, time.min, tzinfo=tz).timestamp()),
    )
