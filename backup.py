import argparse
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path


DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", "/app/data/skedi.db"))
BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/app/backups"))
BACKUP_RETENTION = max(1, int(os.environ.get("BACKUP_RETENTION", "30")))
BACKUP_AUTO_RETENTION = max(
    1, int(os.environ.get("BACKUP_AUTO_RETENTION", str(BACKUP_RETENTION)))
)
BACKUP_MANUAL_RETENTION = max(
    1, int(os.environ.get("BACKUP_MANUAL_RETENTION", "10"))
)
BACKUP_SAFETY_RETENTION = max(
    1, int(os.environ.get("BACKUP_SAFETY_RETENTION", "10"))
)
BACKUP_INTERVAL_SECONDS = max(
    300, int(os.environ.get("BACKUP_INTERVAL_SECONDS", "86400"))
)
BACKUP_SUFFIX = ".sqlite3"
OFFSITE_BACKUP_ENABLED = os.environ.get("OFFSITE_BACKUP_ENABLED", "0") == "1"
OFFSITE_BACKUP_DIR = (
    Path(os.environ.get("OFFSITE_BACKUP_DIR", "/app/offsite-backups"))
    if OFFSITE_BACKUP_ENABLED
    else None
)
OFFSITE_BACKUP_RETENTION = max(
    1, int(os.environ.get("OFFSITE_BACKUP_RETENTION", "90"))
)


def verify_database(path):
    path = Path(path)
    if not path.is_file():
        return False
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            return connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    except sqlite3.Error:
        return False


def list_backups(directory=None):
    directory = Path(directory or BACKUP_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    backups = []
    for path in directory.glob(f"*{BACKUP_SUFFIX}"):
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
                "modified_timestamp": stat.st_mtime,
            }
        )
    return sorted(
        backups, key=lambda item: item["modified_timestamp"], reverse=True
    )


def backup_path(name):
    if not name or Path(name).name != name or not name.endswith(BACKUP_SUFFIX):
        raise ValueError("Neveljavno ime varnostne kopije.")
    path = (BACKUP_DIR / name).resolve()
    if path.parent != BACKUP_DIR.resolve():
        raise ValueError("Neveljavna pot varnostne kopije.")
    return path


def backup_kind(name):
    for kind in ("pre-restore", "pre-import", "manual", "auto"):
        if name.startswith(f"{kind}-"):
            return kind
    return "other"


def prune_backups():
    groups = {
        "auto": BACKUP_AUTO_RETENTION,
        "manual": BACKUP_MANUAL_RETENTION,
        "safety": BACKUP_SAFETY_RETENTION,
        "other": BACKUP_MANUAL_RETENTION,
    }
    items = list_backups()
    for group, retention in groups.items():
        if group == "safety":
            selected = [
                item
                for item in items
                if backup_kind(item["name"]) in {"pre-import", "pre-restore"}
            ]
        else:
            selected = [
                item for item in items if backup_kind(item["name"]) == group
            ]
        for item in selected[retention:]:
            backup_path(item["name"]).unlink(missing_ok=True)


def prune_offsite_backups():
    if OFFSITE_BACKUP_DIR is None:
        return
    for item in list_backups(OFFSITE_BACKUP_DIR)[OFFSITE_BACKUP_RETENTION:]:
        path = (OFFSITE_BACKUP_DIR / item["name"]).resolve()
        if path.parent == OFFSITE_BACKUP_DIR.resolve():
            path.unlink(missing_ok=True)


def mirror_backup(path):
    """Kopiraj preverjeno kopijo na izbirno drugo priklopljeno lokacijo."""
    if OFFSITE_BACKUP_DIR is None:
        return None
    source = Path(path)
    OFFSITE_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    destination = OFFSITE_BACKUP_DIR / source.name
    temporary = destination.with_suffix(".tmp")
    try:
        shutil.copy2(source, temporary)
        if not verify_database(temporary):
            raise RuntimeError("Preverjanje druge kopije ni uspelo.")
        os.replace(temporary, destination)
        prune_offsite_backups()
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def backup_status(now=None):
    now = now or datetime.now()
    local = list_backups()
    automatic = [item for item in local if backup_kind(item["name"]) == "auto"]
    latest_auto = automatic[0] if automatic else None
    if latest_auto:
        latest_time = datetime.fromtimestamp(latest_auto["modified_timestamp"])
        age_hours = max(0, (now - latest_time).total_seconds() / 3600)
    else:
        age_hours = None
    offsite = list_backups(OFFSITE_BACKUP_DIR) if OFFSITE_BACKUP_DIR else []
    return {
        "local_count": len(local),
        "latest": local[0] if local else None,
        "latest_auto": latest_auto,
        "latest_auto_age_hours": age_hours,
        "automatic_stale": age_hours is None or age_hours > 26,
        "offsite_enabled": OFFSITE_BACKUP_DIR is not None,
        "offsite_count": len(offsite),
        "offsite_latest": offsite[0] if offsite else None,
    }


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
        except FileNotFoundError as error:
            print(f"Baza še ni pripravljena: {error}", file=sys.stderr, flush=True)
            time.sleep(60)
        except Exception as error:
            print(f"Napaka pri varnostnem kopiranju: {error}", file=sys.stderr, flush=True)
            time.sleep(300)
        else:
            message = f"Ustvarjena dnevna varnostna kopija: {path.name}"
            try:
                mirrored = mirror_backup(path)
            except Exception as error:
                print(
                    f"Lokalna kopija je uspela, druga lokacija pa ne: {error}",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                if mirrored:
                    message += f"; druga kopija: {mirrored.name}"
            print(message, flush=True)
            time.sleep(BACKUP_INTERVAL_SECONDS)


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
