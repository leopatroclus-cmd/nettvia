"""Phase C.1 done-gates: the Accounts view (list + metric cards) is scoped to
the current party's OPEN cycle.

(1) Reset → empty list AND zeroed metric cards.
(2) Advance → netted obligations drop out of the open view (they live in the
    closed cycle's statement); only rolled-forward obligations remain, and the
    cards reflect just those.
"""


def _open_ids(client):
    return {o["id"] for o in client.get("/obligations").json()}


def test_reset_zeroes_accounts_metrics(client):
    client.post("/demo/reset")
    assert client.get("/obligations").json() == []          # empty list
    m = client.get("/accounts/metrics").json()
    assert m["gross_receivable"] == [] and m["gross_payable"] == []
    assert m["pending"] == 0 and m["needs_attention"] == 0   # zeroed cards


def test_advance_drops_netted_keeps_rolled(client, conn):
    # Seeded open cycle: Aegean's accept&net matched invoices are nettable;
    # SKY-4471 (ob 1) is one of them; Nile AEG-2044 (ob 7) is one-sided → rolls.
    before = _open_ids(client)
    assert {1, 7} <= before

    client.post("/demo/advance")

    after = _open_ids(client)
    # The netted invoice left the open view (it's frozen in the now-closed cycle).
    assert 1 not in after
    assert conn.execute(
        "SELECT 1 FROM cycle_obligations WHERE cycle_id=1 AND obligation_id=1").fetchone()
    # The rolled, one-sided Nile obligation remains in the new open cycle.
    assert 7 in after
    open_id = conn.execute("SELECT cycle_id FROM cycles WHERE state='open'").fetchone()[0]
    assert conn.execute(
        "SELECT assigned_cycle_id FROM obligations WHERE obligation_id=7").fetchone()[0] == open_id

    # Cards reflect ONLY the rolled set: every counted obligation is in the open
    # cycle, and no netted (frozen) one is double-counted.
    m = client.get("/accounts/metrics").json()
    open_rows = conn.execute(
        "SELECT direction, amount, currency FROM obligations "
        "WHERE owner_party_id=1 AND assigned_cycle_id=?", (open_id,)).fetchall()
    exp_ar = sum(r["amount"] for r in open_rows if r["direction"] == "AR")
    got_ar = sum(int(x["major"]) * 100 for x in m["gross_receivable"])   # EUR-only here
    assert got_ar == exp_ar
    # Nile AEG-2044 is a receivable awaiting disposition → counts as pending.
    assert m["pending"] >= 1


def _accept_net_pair(conn, ref, biller, payer, amt, matched=True):
    """Insert a two-sided accept&net EUR pair in the open cycle (cycle 1)."""
    from app import canonical, matching
    names = {r["party_id"]: r["legal_name"]
             for r in conn.execute("SELECT party_id, legal_name FROM parties")}

    def ins(owner, direction, cp):
        conn.execute(
            "INSERT INTO obligations (owner_party_id, direction, counterparty_raw, "
            "counterparty_party_id, network_id, invoice_number, amount, currency, "
            "issue_date, due_date, status_source, assigned_cycle_id, ingest_state, "
            "disposition, settlement_mode) VALUES (?,?,?,?,1,?,?, 'EUR', '2026-06-05', "
            "'2026-06-25', 'open', 1, 'ingested', 'accepted', 'net')",
            (owner, direction, names[cp], cp, ref, amt * 100))

    ins(biller, "AR", payer)
    ins(payer, "AP", biller)
    if matched:
        matching.match_all(conn)
        canonical.mint(conn)
    conn.commit()


def test_all_netted_open_view_empty_for_every_party(client, conn):
    """ALL-NETTED case (no rolled items): after advance, every fully-netted
    party's open Accounts view is empty — netted invoices live only in the
    closed cycle's statement, none swept into the new open cycle."""
    client.post("/demo/reset")
    # A small fully-nettable ring: 1→2, 2→3, 3→1 (all matched, accept&net).
    _accept_net_pair(conn, "R-1", 1, 2, 5000)
    _accept_net_pair(conn, "R-2", 2, 3, 4000)
    _accept_net_pair(conn, "R-3", 3, 1, 3000)

    client.post("/demo/advance")

    # Nothing rolled → every party's open view is empty.
    for pid in (1, 2, 3):
        client.post("/demo/session", json={"party_id": pid})
        assert client.get("/obligations").json() == [], f"party {pid} open view not empty"
        m = client.get("/accounts/metrics").json()
        assert m["gross_receivable"] == [] and m["gross_payable"] == []

    # All six obligations stayed frozen in the now-closed cycle 1.
    assert conn.execute("SELECT COUNT(*) FROM cycle_obligations WHERE cycle_id=1").fetchone()[0] == 6
    assert conn.execute(
        "SELECT COUNT(*) FROM obligations WHERE assigned_cycle_id=1").fetchone()[0] == 6


def test_accepted_net_but_unmatched_does_not_roll(client, conn):
    """Accept&net on a pair that isn't a confirmed nettable match must NOT roll
    into the new open cycle (the reported bug) — it stays in the closing cycle."""
    client.post("/demo/reset")
    _accept_net_pair(conn, "U-1", 1, 2, 7000, matched=False)   # accepted net, unmatched

    client.post("/demo/advance")

    assert client.get("/obligations").json() == []             # left the open view
    open_id = conn.execute("SELECT cycle_id FROM cycles WHERE state='open'").fetchone()[0]
    assert conn.execute(
        "SELECT COUNT(*) FROM obligations WHERE assigned_cycle_id=?", (open_id,)).fetchone()[0] == 0
