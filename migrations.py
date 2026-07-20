"""Majhne, zaporedne migracije SQLite sheme.

Stare namestitve so pred uvedbo te datoteke stolpce dodajale neposredno v
``init_db``. Nove spremembe se od različice 1 naprej beležijo v tabeli
``schema_migrations``, zato se vsaka izvede natanko enkrat.
"""


LATEST_SCHEMA_VERSION = 1


def _column_names(db, table):
    return {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}


def _migration_1(db):
    if "must_change_password" not in _column_names(db, "users"):
        db.execute(
            "ALTER TABLE users ADD COLUMN must_change_password "
            "INTEGER NOT NULL DEFAULT 0"
        )
    db.execute(
        """CREATE INDEX IF NOT EXISTS idx_participants_callsign
           ON participants(callsign COLLATE NOCASE, checkin_at DESC)"""
    )


MIGRATIONS = {
    1: _migration_1,
}


def run_migrations(db, applied_at):
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                   version INTEGER PRIMARY KEY,
                   applied_at TEXT NOT NULL
               )"""
        )
        applied = {
            row["version"]
            for row in db.execute("SELECT version FROM schema_migrations").fetchall()
        }
        for version in sorted(MIGRATIONS):
            if version in applied:
                continue
            MIGRATIONS[version](db)
            db.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, applied_at),
            )
        db.commit()
    except Exception:
        db.rollback()
        raise


def schema_version(db):
    try:
        row = db.execute(
            "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
        ).fetchone()
    except Exception:
        return 0
    return row["version"]
