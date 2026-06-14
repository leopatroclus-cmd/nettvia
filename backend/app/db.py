"""SQLite connection + first-run initialization.

Production-safe: app startup NEVER resets an existing database. The demo data is
seeded ONLY when the DB has no obligations yet (empty/new) — a redeploy or
restart preserves whatever is already stored. DB_PATH comes from the environment
so prod can point it at a mounted volume (e.g. DB_PATH=/data/netting.db).
"""
import os
import sqlite3

BASE_DIR = os.path.dirname(__file__)
DB_PATH = os.environ.get("DB_PATH") or os.path.join(BASE_DIR, "netting.db")
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _seed_fresh(conn):
    from .seed import seed
    from .matching import match_all
    from . import canonical
    seed(conn)
    match_all(conn)        # matches are computed, not seeded
    canonical.mint(conn)   # canonical invoices from confirmed matches


def init_db(reset=False):
    """Ensure the schema exists and seed demo data ONLY when the DB is empty.

    Safe to call on every startup: an existing, populated DB is never reset.
    `reset=True` is for dev/tests ONLY — it drops the file first, then reseeds.
    """
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if reset and os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    conn = get_conn()
    with open(SCHEMA_PATH) as f:
        conn.executescript(f.read())   # all CREATE ... IF NOT EXISTS — idempotent
    conn.commit()

    # "Already initialized?" must key off a table the demo RESET PRESERVES
    # (networks/parties), NOT a transactional table it clears (obligations).
    # Otherwise a post-reset DB looks "empty" and re-seeding collides on the
    # surviving networks row. Seed only a genuinely fresh DB (no networks).
    initialized = conn.execute("SELECT COUNT(*) FROM networks").fetchone()[0] > 0
    if not initialized:
        _seed_fresh(conn)              # first run / empty DB only — never overwrites data
    conn.close()


def reset_db():
    """Dev/tests ONLY: wipe and reseed. Never called from app startup."""
    init_db(reset=True)


if __name__ == "__main__":
    import sys
    if "--reset" in sys.argv:
        reset_db()
        print("Reset + seeded", DB_PATH)
    else:
        init_db()
        print("Ensured schema; seeded only if empty:", DB_PATH)
