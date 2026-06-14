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


def test_close_uploads_is_cutoff_only(client, conn):
    client.post("/cycles/1/close-uploads")
    assert conn.execute("SELECT state FROM cycles WHERE cycle_id=1").fetchone()[0] == "reconciling"
    # Cut-off ONLY: nothing settles and no next cycle is opened here (Run netting
    # opens it). Obligations stay put and stay visible.
    assert conn.execute("SELECT COUNT(*) FROM cycles").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM obligations WHERE owner_party_id=1 AND assigned_cycle_id=1"
    ).fetchone()[0] == 9


def test_lock_freezes_only_and_keeps_invoices_visible(client, conn):
    client.post("/cycles/1/close-uploads")
    r = client.post("/cycles/1/lock")
    assert r.status_code == 200 and r.json()["frozen"] == 12   # 6 nettable pairs
    assert conn.execute("SELECT state FROM cycles WHERE cycle_id=1").fetchone()[0] == "locked"
    assert conn.execute("SELECT COUNT(*) FROM cycle_obligations WHERE cycle_id=1").fetchone()[0] == 12
    # Lock is freeze-ONLY: nothing rolls, no next cycle, ALL invoices still
    # assigned to (and visible in) the working cycle, statement intact.
    assert conn.execute("SELECT COUNT(*) FROM cycles").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM obligations WHERE owner_party_id=1 AND assigned_cycle_id=1"
    ).fetchone()[0] == 9
    assert len(client.get("/obligations").json()) == 9


def test_lock_requires_reconciling(client):
    assert client.post("/cycles/1/lock").status_code == 409   # still OPEN


def test_lock_alone_changes_only_cycle_state(client, conn):
    """Phase D.1: Lock must change ONLY cycle state (+ freeze the snapshot) — it
    must not roll, archive, create the next cycle, or change the statement."""
    client.post("/cycles/1/close-uploads")
    view_before = sorted(o["id"] for o in client.get("/obligations").json())
    stmt_before = client.get("/statement/summary").json()["network"]["gross_settled"]
    assign_before = conn.execute(
        "SELECT obligation_id, assigned_cycle_id FROM obligations ORDER BY obligation_id").fetchall()

    client.post("/cycles/1/lock")

    assert conn.execute("SELECT state FROM cycles WHERE cycle_id=1").fetchone()[0] == "locked"
    assert conn.execute("SELECT COUNT(*) FROM cycles").fetchone()[0] == 1          # no next cycle
    assert sorted(o["id"] for o in client.get("/obligations").json()) == view_before  # view intact
    assert client.get("/statement/summary").json()["network"]["gross_settled"] == stmt_before
    # nothing rolled — every assigned_cycle_id unchanged
    assert conn.execute(
        "SELECT obligation_id, assigned_cycle_id FROM obligations ORDER BY obligation_id"
    ).fetchall() == assign_before


def test_run_netting_is_atomic_statement_archive_roll(client, conn):
    """Phase D.1: Run netting performs statement + archive + roll + close in one
    step on the locked cycle."""
    client.post("/cycles/1/close-uploads")
    client.post("/cycles/1/lock")
    nile = _oid(conn, "AEG-2044")     # one-sided → rolls
    sky = _oid(conn, "SKY-4471")      # netted → settled/archived

    client.post("/cycles/1/net")

    # (2) statement written for the worked cycle
    assert conn.execute("SELECT COUNT(*) FROM net_positions WHERE cycle_id=1").fetchone()[0] > 0
    # (5) cycle closed, a new cycle opened
    assert conn.execute("SELECT state FROM cycles WHERE cycle_id=1").fetchone()[0] == "closed"
    open_id = conn.execute("SELECT cycle_id FROM cycles WHERE state='open'").fetchone()[0]
    assert open_id != 1
    # (3) netted obligation archived in the closed cycle, gone from the active view
    assert conn.execute(
        "SELECT assigned_cycle_id FROM obligations WHERE obligation_id=?", (sky,)).fetchone()[0] == 1
    assert sky not in {o["id"] for o in client.get("/obligations").json()}
    # (4) unresolved (Nile) rolled into the new open cycle and stays active
    assert conn.execute(
        "SELECT assigned_cycle_id FROM obligations WHERE obligation_id=?", (nile,)).fetchone()[0] == open_id
    assert nile in {o["id"] for o in client.get("/obligations").json()}


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
    # default policy is 'roll': pending Levant (net-90) stays put through lock and
    # ROLLS only at Run netting (settlement), never frozen/netted.
    lev = _oid(conn, "LEV-1190")
    assert conn.execute("SELECT disposition FROM obligations WHERE obligation_id=?", (lev,)).fetchone()[0] == "pending"
    client.post("/cycles/1/close-uploads")
    client.post("/cycles/1/lock")
    assert conn.execute("SELECT assigned_cycle_id FROM obligations WHERE obligation_id=?", (lev,)).fetchone()[0] == 1   # still here at lock
    assert conn.execute("SELECT COUNT(*) FROM cycle_obligations WHERE cycle_id=1 AND obligation_id=?", (lev,)).fetchone()[0] == 0
    client.post("/cycles/1/net")
    assert conn.execute("SELECT assigned_cycle_id FROM obligations WHERE obligation_id=?", (lev,)).fetchone()[0] != 1   # rolled on settlement


def test_default_on_no_action_auto_accept(client, conn):
    conn.execute("UPDATE networks SET default_on_no_action='auto_accept' WHERE network_id=1")
    conn.commit()
    lev = _oid(conn, "LEV-1190")
    client.post("/cycles/1/close-uploads")
    client.post("/cycles/1/lock")
    # auto_accept concretizes the suggestion at lock (Levant is long-dated → deferred, not pending).
    assert conn.execute("SELECT disposition FROM obligations WHERE obligation_id=?", (lev,)).fetchone()[0] != "pending"
