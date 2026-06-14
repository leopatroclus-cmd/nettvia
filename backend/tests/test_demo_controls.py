"""Phase B done-gates: browser-driven cycle advance + demo reset.

advance: from any active state, lock → net (write net_positions + statement) →
close, leaving the next cycle open; deferred/unmatched obligations roll forward.
reset: clear all transactional data, reopen a fresh cycle, keep parties intact.
"""


def _state(conn, cycle_id):
    return conn.execute("SELECT state FROM cycles WHERE cycle_id=?", (cycle_id,)).fetchone()[0]


def test_advance_nets_and_rolls_unmatched(client, conn):
    # Cycle 1 is open with the seeded nettable set + one-sided / deferred rows.
    # Obligation 7 (Nile, off-network) is unmatched → must roll forward.
    assert _state(conn, 1) == "open"
    rolled_before = conn.execute(
        "SELECT assigned_cycle_id FROM obligations WHERE obligation_id=7").fetchone()[0]
    assert rolled_before == 1

    res = client.post("/demo/advance")
    assert res.status_code == 200
    body = res.json()
    assert body["advanced_cycle_id"] == 1

    # Cycle 1 is fully advanced to closed; a later cycle is now open.
    assert _state(conn, 1) == "closed"
    open_cycles = conn.execute("SELECT cycle_id FROM cycles WHERE state='open'").fetchall()
    assert len(open_cycles) == 1
    next_id = open_cycles[0]["cycle_id"]
    assert next_id != 1

    # Netting wrote net_positions for the closed cycle (Σnet=0 over the snapshot).
    npos = conn.execute("SELECT net_amount FROM net_positions WHERE cycle_id=1").fetchall()
    assert npos, "advance must persist net_positions"
    assert sum(r["net_amount"] for r in npos) == 0

    # The frozen snapshot exists, and a statement is retrievable for the cycle.
    assert conn.execute(
        "SELECT COUNT(*) FROM cycle_obligations WHERE cycle_id=1").fetchone()[0] > 0
    stmt = client.get("/statement", params={"cycle_id": 1})
    assert stmt.status_code == 200

    # The unmatched obligation rolled into the newly opened cycle and is visible.
    rolled_after = conn.execute(
        "SELECT assigned_cycle_id FROM obligations WHERE obligation_id=7").fetchone()[0]
    assert rolled_after == next_id


def test_reset_preserves_parties_clears_transactions(client, conn):
    # Build some state first (advance creates net_positions, cycle_obligations…).
    client.post("/demo/advance")
    parties_before = conn.execute("SELECT party_id, legal_name, tax_id FROM parties "
                                  "ORDER BY party_id").fetchall()
    assert parties_before, "precondition: parties exist"

    res = client.post("/demo/reset")
    assert res.status_code == 200

    # Every transactional table is empty.
    for table in ("obligations", "matches", "cycle_obligations", "net_positions",
                  "counterparty_aliases", "canonical_invoices"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table

    # Exactly one open cycle, and it's a fresh sequence-1 cycle.
    cyc = conn.execute("SELECT sequence_no, state FROM cycles").fetchall()
    assert len(cyc) == 1 and cyc[0]["state"] == "open" and cyc[0]["sequence_no"] == 1

    # Parties (identities + tax_ids) are untouched.
    parties_after = conn.execute("SELECT party_id, legal_name, tax_id FROM parties "
                                 "ORDER BY party_id").fetchall()
    assert [tuple(p) for p in parties_after] == [tuple(p) for p in parties_before]

    # Clean baseline is usable: obligations empty, audit chain still verifies.
    assert client.get("/obligations").json() == []
    assert client.get("/audit/verify").json()["ok"] is True
