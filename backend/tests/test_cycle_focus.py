"""Phase D.2: deterministic cycle-in-focus with MORE THAN ONE non-closed cycle.

Single-cycle tests never exercised this — these inject stray/leftover non-closed
cycles (late-upload litter, prod orphans) and assert Accounts / reveal / statement
resolve to the RIGHT cycle, that the runbook lands the reveal on the settled cycle,
and that no second non-closed cycle can be spawned.
"""
from app import cycles

_CSV = ("Invoice,Counterparty,Amount,Currency,Direction,Due\n"
        "L1,Skyline Freight GmbH,100,EUR,Sales,2026-06-25\n")


def test_late_upload_after_cutoff_rejected_no_second_cycle(client, conn):
    client.post("/cycles/1/close-uploads")          # cut-off → reconciling
    up = client.post("/upload", files={"file": ("l.csv", _CSV, "text/csv")}).json()
    r = client.post(f"/upload/{up['batch_id']}/confirm")
    assert r.status_code == 409
    assert "current cycle" in r.json()["detail"].lower()
    # The invariant holds: still exactly ONE non-closed cycle, no stray spawned.
    assert conn.execute("SELECT COUNT(*) FROM cycles WHERE state!='closed'").fetchone()[0] == 1


def test_stale_empty_cycle_does_not_hijack_the_view(client, conn):
    # Working cycle 1 (open, holds the seeded obligations). Inject a stray EMPTY
    # open cycle 2 with a HIGHER sequence — exactly the kind of litter that broke
    # "earliest non-closed".
    cycles.open_cycle(conn, 1, "2026-07-01", 2)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM cycles WHERE state!='closed'").fetchone()[0] == 2

    # Resolution ignores the empty stray and locks onto the cycle with data.
    assert cycles.working_cycle(conn, 1)["cycle_id"] == 1
    assert client.get("/cycles/current").json()["cycle_id"] == 1
    assert len(client.get("/obligations").json()) == 9     # Aegean's cycle-1 ledger, not empty cycle 2


def test_stale_lower_seq_reconciling_cycle_does_not_hijack(client, conn):
    # The opposite litter: a stale EMPTY lower-sequence non-closed cycle alongside
    # the real working cycle (higher sequence, with data). Must not be picked.
    conn.execute("UPDATE cycles SET state='reconciling' WHERE cycle_id=1")  # stale, will be emptied
    work = cycles.open_cycle(conn, 1, "2026-07-01", 2)                      # real working cycle
    conn.execute("UPDATE obligations SET assigned_cycle_id=? WHERE owner_party_id=1",
                 (work["cycle_id"],))                                       # move Aegean's data here
    conn.commit()
    assert cycles.working_cycle(conn, 1)["cycle_id"] == work["cycle_id"]    # latest WITH obligations
    assert client.get("/cycles/current").json()["cycle_id"] == work["cycle_id"]


def test_reconcile_closes_empty_orphans_keeps_working(client, conn):
    cycles.open_cycle(conn, 1, "2026-07-01", 2)      # empty stray
    cycles.open_cycle(conn, 1, "2026-08-01", 3)      # empty stray
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM cycles WHERE state!='closed'").fetchone()[0] == 3

    closed = cycles.reconcile_orphans(conn, 1)
    assert closed == 2                                # both empty strays closed
    assert conn.execute("SELECT COUNT(*) FROM cycles WHERE state!='closed'").fetchone()[0] == 1
    assert cycles.working_cycle(conn, 1)["cycle_id"] == 1   # the one holding obligations
    assert client.get("/audit/verify").json()["ok"] is True


def test_reconcile_all_empty_keeps_latest(client, conn):
    for t in ("cycle_obligations", "net_positions", "canonical_invoices",
              "matches", "obligations"):
        conn.execute(f"DELETE FROM {t}")             # make cycle 1 empty too
    cycles.open_cycle(conn, 1, "2026-07-01", 2)
    conn.commit()
    closed = cycles.reconcile_orphans(conn, 1)
    assert closed == 1                                # one of two empties closed
    remaining = conn.execute("SELECT cycle_id FROM cycles WHERE state!='closed'").fetchall()
    assert len(remaining) == 1 and remaining[0]["cycle_id"] == 2   # latest kept as working


def test_runbook_reveal_and_statement_land_on_settled_after_net(client, conn):
    client.post("/cycles/1/close-uploads")
    client.post("/cycles/1/lock")
    client.post("/cycles/1/net")

    # Reveal + default statement stay on the JUST-SETTLED closed cycle 1 (real
    # numbers), NOT the freshly-opened empty cycle.
    assert client.get("/netting").json()["party"]["gross"] > 0
    stmt = client.get("/statement/summary").json()
    assert stmt["cycle_id"] == 1 and stmt["network"]["gross_settled"]

    # Accounts, separately, shows the NEW open working cycle (rolled obligations).
    cur = client.get("/cycles/current").json()
    assert cur["cycle_id"] != 1 and cur["state"] == "open"
    ids = {o["id"] for o in client.get("/obligations").json()}
    nile = conn.execute(
        "SELECT obligation_id FROM obligations WHERE invoice_number='AEG-2044'").fetchone()[0]
    sky = conn.execute(
        "SELECT obligation_id FROM obligations WHERE invoice_number='SKY-4471' AND owner_party_id=1"
    ).fetchone()[0]
    assert nile in ids and sky not in ids            # Nile rolled (active), SKY netted (settled)
