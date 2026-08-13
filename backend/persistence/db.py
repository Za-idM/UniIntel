"""SQLite connection + schema bootstrap for UniIntel.

DEPLOYMENT WARNING: DB_PATH (config.DB_PATH, overridable via the DB_PATH env
var) is a plain SQLite file. Railway and Render's default filesystem is
ephemeral -- it is wiped on every redeploy and is NOT shared across replicas.
Any jobs/products/corrections written to it will be lost the next time the
service redeploys or restarts, silently. For anything beyond a demo:
  - Railway: attach a Volume (Settings -> Volumes) and set DB_PATH to a path
    under its mount (e.g. /data/uniintel.db) so the file survives restarts.
  - Render: use a Render Disk the same way, or switch to a hosted Postgres
    (Render/Railway both offer one-click Postgres) if multi-replica or
    stronger durability guarantees are needed -- SQLite has no story for
    concurrent writers across replicas either.
This module does not attempt that migration -- it only makes the path
configurable so a volume/Postgres swap is a config change, not a code change,
whichever direction gets picked."""
import sqlite3
from pathlib import Path

from config import DB_PATH as _DB_PATH_STR

DB_PATH = Path(_DB_PATH_STR)
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Columns added after the initial schema -- CREATE TABLE IF NOT EXISTS won't
# retrofit these onto a database file created before the column existed, so
# each one gets a guarded ALTER TABLE (SQLite has no "ADD COLUMN IF NOT
# EXISTS", hence the try/except on the duplicate-column error).
_MIGRATIONS = [
    "ALTER TABLE jobs ADD COLUMN processed_rows INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN error TEXT",
]


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection()
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
        for migration in _MIGRATIONS:
            try:
                conn.execute(migration)
                conn.commit()
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e):
                    raise
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Initialized SQLite schema at {DB_PATH}")
