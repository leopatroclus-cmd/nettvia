"""Regression: startup must not crash after a demo Reset.

Reset keeps networks + parties and clears transactional tables. The seed-if-empty
guard must key off a PRESERVED table (networks), so a post-reset DB is recognised
as already-initialized and is NOT re-seeded (which would collide on networks).
"""
from app import db
from app.db import get_conn

# Mirrors /demo/reset: cleared transactional tables (networks + parties KEPT).
_RESET_TABLES = ("cycle_obligations", "net_positions", "canonical_invoices",
                 "matches", "obligations", "upload_batches", "source_formats",
                 "cycles", "counterparty_aliases", "audit_log")


def test_init_db_after_reset_does_not_reseed_or_raise(db_path):
    conn = get_conn()
    for t in _RESET_TABLES:                      # simulate a demo Reset
        conn.execute(f"DELETE FROM {t}")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM networks").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM obligations").fetchone()[0] == 0
    conn.close()

    db.init_db()                                 # next startup — must be clean

    conn = get_conn()
    # Networks preserved, NOT duplicated; no re-seed of transactional data.
    assert conn.execute("SELECT COUNT(*) FROM networks").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM obligations").fetchone()[0] == 0
    conn.close()


def test_init_db_seeds_a_genuinely_empty_db(db_path):
    conn = get_conn()
    for t in _RESET_TABLES + ("party_networks", "parties", "currencies", "networks"):
        conn.execute(f"DELETE FROM {t}")         # wipe EVERYTHING → truly empty
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM networks").fetchone()[0] == 0
    conn.close()

    db.init_db()                                 # empty DB → seeds once

    conn = get_conn()
    assert conn.execute("SELECT COUNT(*) FROM networks").fetchone()[0] >= 1
    assert conn.execute("SELECT COUNT(*) FROM obligations").fetchone()[0] > 0
    conn.close()


def test_reference_inserts_are_defensive(db_path):
    """The networks/parties/currencies inserts must no-op (OR IGNORE) on an
    already-present row, so a half-seeded DB can never crash startup."""
    from app import refdata
    from app.seed import NETWORK, PARTIES
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO networks (network_id, name, cycle_length_days, cut_off_rule, "
        "settlement_lag_days, term_mode, default_on_no_action, cost_per_payment, "
        "cost_components, upload_cutoff_offset_days, processing_cutoff_offset_days) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", NETWORK)            # networks row already exists
    cur.executemany(
        "INSERT OR IGNORE INTO parties (party_id,legal_name,tax_id,country,city,group_id,on_network,jurisdiction) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [p[:7] + (refdata.iso3(p[3]),) for p in PARTIES])     # parties already exist
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM networks").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM parties").fetchone()[0] == len(PARTIES)
    conn.close()
