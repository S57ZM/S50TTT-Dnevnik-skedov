import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path


DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", "/app/data/skedi.db"))
BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/app/backups"))
BACKUP_RETENTION = max(1, int(os.environ.get("BACKUP_RETENTION", "30")))
BACKUP_INTERVAL_SECONDS = max(
    300, int(os.environ.get("BACKUP_INTERVAL_SECONDS", "86400"))
)
BACKUP_SUFFIX = ".sqlite3"


def verify_database(path):
    path = Path(path)
    if not path.is_file():
        return False
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            return connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    except sqlite3.Error:
        return False


def list_backups():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backups = []
    for path in BACKUP_DIR.glob(f"*{BACKUP_SUFFIX}"):
        if not path.is_file():
            continue
        stat = path.stat()
        backups.append(
            {
                "name": path.name,
                "size": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            }
        )
    return sorted(backups, key=lambda item: item["modified_at"], reverse=True)


def backup_path(name):
    if not name or Path(name).name != name or not name.endswith(BACKUP_SUFFIX):
        raise ValueError("Neveljavno ime varnostne kopije.")
    path = (BACKUP_DIR / name).resolve()
    if path.parent != BACKUP_DIR.resolve():
        raise ValueError("Neveljavna pot varnostne kopije.")
    return path


def prune_backups(retention=BACKUP_RETENTION):
    for item in list_backups()[retention:]:
        backup_path(item["name"]).unlink(missing_ok=True)


def create_backup(kind="auto"):
    if kind not in {"auto", "manual", "pre-restore", "pre-import"}:
        raise ValueError("Neveljavna vrsta varnostne kopije.")
    if not DATABASE_PATH.is_file():
        raise FileNotFoundError(f"Podatkovna baza ne obstaja: {DATABASE_PATH}")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    destination = BACKUP_DIR / f"{kind}-skedi-{timestamp}{BACKUP_SUFFIX}"
    temporary = destination.with_suffix(".tmp")
    try:
        with sqlite3.connect(DATABASE_PATH) as source, sqlite3.connect(temporary) as target:
            source.backup(target)
        if not verify_database(temporary):
            raise RuntimeError("Preverjanje izdelane varnostne kopije ni uspelo.")
        os.replace(temporary, destination)
        if kind != "pre-restore":
            prune_backups()
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def restore_backup(name, confirmed=False):
    if not confirmed:
        raise RuntimeError("Obnovo moraš potrditi z --confirm.")
    source_path = backup_path(name)
    if not verify_database(source_path):
        raise RuntimeError("Izbrana datoteka ni veljavna SQLite baza.")

    if DATABASE_PATH.is_file():
        create_backup("pre-restore")
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = DATABASE_PATH.with_suffix(".restore-tmp")
    try:
        with sqlite3.connect(source_path) as source, sqlite3.connect(temporary) as target:
            source.backup(target)
        if not verify_database(temporary):
            raise RuntimeError("Preverjanje obnovljene baze ni uspelo.")
        os.replace(temporary, DATABASE_PATH)
        for suffix in ("-wal", "-shm"):
            Path(f"{DATABASE_PATH}{suffix}").unlink(missing_ok=True)
    finally:
        temporary.unlink(missing_ok=True)


def run_scheduler():
    while True:
        try:
            path = create_backup("auto")
            print(f"Ustvarjena dnevna varnostna kopija: {path.name}", flush=True)
            time.sleep(BACKUP_INTERVAL_SECONDS)
        except FileNotFoundError as error:
            print(f"Baza še ni pripravljena: {error}", file=sys.stderr, flush=True)
            time.sleep(60)
        except Exception as error:
            print(f"Napaka pri varnostnem kopiranju: {error}", file=sys.stderr, flush=True)
            time.sleep(300)


def main():
    parser = argparse.ArgumentParser(description="Varnostne kopije S50TTT skedov")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("create")
    subparsers.add_parser("schedule")
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("name")
    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("name")
    restore_parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()

    if args.command == "create":
        print(create_backup("manual"))
    elif args.command == "schedule":
        run_scheduler()
    elif args.command == "verify":
        path = backup_path(args.name)
        print("OK" if verify_database(path) else "NAPAKA")
        if not verify_database(path):
            raise SystemExit(1)
    elif args.command == "restore":
        restore_backup(args.name, args.confirm)
        print("Obnova je uspešno zaključena.")


if __name__ == "__main__":
    main()
