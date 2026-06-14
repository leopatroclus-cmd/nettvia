"""Phase 6 done-gates: staged deadlines, state machine, snapshot, default_on_no_action."""
from app import cycles


def _oid(conn, inv, owner=1):
    return conn.execute(
        "SELECT obligation_id FROM obligations WHERE invoice_number=? AND owner_party_id=?",
        (inv, owner)).fetchone()[0]


def test_staged_deadlines_from_config(conn):
    c = conn.execute("SELECT * FROM cycles WHERE cycle_id=1").fetchone()
    # opens 2026-06-01; offsets 25 / 29; settlement_lag 3.
    assert c["opens_at"] == "2026-06-01"
    assert c["upload_cutoff_at"] == "2026-06-26"
    assert c["processing_cutoff_at"] == "2026-06-30"
    assert c["settlement_date"] == "2026-07-03"
    assert c["state"] == "open"


def test_close_uploads_to_reconciling_opens_next(client, conn):
    client.post("/cycles/1/close-uploads")
    assert conn.execute("SELECT state FROM cycles WHERE cycle_id=1").fetchone()[0] == "reconciling"
    # a new OPEN cycle exists for late uploads
    assert conn.execute("SELECT COUNT(*) FROM cycles WHERE state='open'").fetchone()[0] == 1


def test_lock_freezes_immutable_snapshot(client, conn):
    client.post("/cycles/1/close-uploads")
    r = client.post("/cycles/1/lock")
    assert r.status_code == 200 and r.json()["frozen"] == 12   # 6 nettable pairs
    assert conn.execute("SELECT state FROM cycles WHERE cycle_id=1").fetchone()[0] == "locked"
    assert conn.execute("SELECT COUNT(*) FROM cycle_obligations WHERE cycle_id=1").fetchone()[0] == 12


def test_lock_requires_reconciling(client):
    assert client.post("/cycles/1/lock").status_code == 409   # still OPEN


def test_post_lock_edits_rejected(client, conn):
    client.post("/cycles/1/close-uploads")
    client.post("/cycles/1/lock")
    frozen = conn.execute(
        "SELECT obligation_id FROM cycle_obligations WHERE cycle_id=1 "
        "AND obligation_id IN (SELECT obligation_id FROM obligations WHERE owner_party_id=1) LIMIT 1"
    ).fetchone()[0]
    r = client.post("/dispositions", json={"ids": [frozen], "disp": "dispute"})
    assert r.status_code == 409


def test_default_on_no_action_roll(client, conn):
    # default policy is 'roll': pending Levant (net-90) must roll to the next cycle, not net.
    lev = _oid(conn, "LEV-1190")
    assert conn.execute("SELECT disposition FROM obligations WHERE obligation_id=?", (lev,)).fetchone()[0] == "pending"
    client.post("/cycles/1/close-uploads")
    client.post("/cycles/1/lock")
    assert conn.execute("SELECT assigned_cycle_id FROM obligations WHERE obligation_id=?", (lev,)).fetchone()[0] != 1
    assert conn.execute("SELECT COUNT(*) FROM cycle_obligations WHERE cycle_id=1 AND obligation_id=?", (lev,)).fetchone()[0] == 0


def test_default_on_no_action_auto_accept(client, conn):
    conn.execute("UPDATE networks SET default_on_no_action='auto_accept' WHERE network_id=1")
    conn.commit()
    lev = _oid(conn, "LEV-1190")
    client.post("/cycles/1/close-uploads")
    client.post("/cycles/1/lock")
    # auto_accept concretizes the suggestion (Levant is long-dated → deferred, not pending).
    assert conn.execute("SELECT disposition FROM obligations WHERE obligation_id=?", (lev,)).fetchone()[0] != "pending"
