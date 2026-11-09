"""CLI for Google Sheets snapshots, including safe manual recovery."""
from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from .sheets_backup import BackupError, SheetsBackup
from .storage import Storage


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="plusminuxbot: Google Sheets резервные копии")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="Список доступных снимков")
    sub.add_parser("backup", help="Создать резервную копию немедленно")
    restore = sub.add_parser("restore", help="Восстановить SQLite из проверенного снимка")
    restore.add_argument("--sheet", help="Название листа backup_... (без параметра — последний)")
    restore.add_argument("--confirm", action="store_true", help="Разрешить замену текущих операций")
    args = parser.parse_args()
    sheet_id = os.getenv("GOOGLE_SHEET_ID", "").strip()
    if not sheet_id:
        parser.error("Сначала заполни GOOGLE_SHEET_ID в .env")
    tz = ZoneInfo(os.getenv("BOT_TIMEZONE", "UTC"))
    backup = SheetsBackup(
        sheet_id, os.getenv("GOOGLE_CREDENTIALS_FILE", "secrets/service-account.json"),
        tz, int(os.getenv("BACKUP_RETENTION_DAYS", "30")),
    )
    storage = Storage(os.getenv("DB_PATH", "data/finance.sqlite3"), tz)
    try:
        if args.command == "list":
            names = backup.list_snapshots()
            print("\n".join(names) if names else "Резервных копий пока нет")
            return
        if args.command == "backup":
            storage.initialize()
            result = backup.export(storage)
            print(f"Готово: {result.title} — {result.count} операций\n{result.url}")
            return
        if not args.confirm:
            parser.error("Для восстановления требуется --confirm. Останови основной бот перед запуском.")
        names = backup.list_snapshots()
        title = args.sheet or (names[0] if names else None)
        if not title:
            raise BackupError("Нет доступных резервных копий")
        rows = backup.read_verified_snapshot(title)
        storage.initialize()
        # Always preserve an independent local database copy before destructive restore.
        dbpath = Path(storage.path)
        emergency = dbpath.with_name(
            f"pre_restore_{datetime.now(tz):%Y%m%d_%H%M%S}_{uuid4().hex[:6]}.sqlite3"
        )
        storage.create_sqlite_backup(emergency)
        storage.restore_all(rows)
        print(f"Восстановлено {len(rows)} операций из {title}")
        print(f"Предыдущая база сохранена: {emergency}")
    except (BackupError, OSError, ValueError) as exc:
        parser.exit(1, f"Ошибка: {exc}\n")


if __name__ == "__main__":
    main()
