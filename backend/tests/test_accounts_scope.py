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
