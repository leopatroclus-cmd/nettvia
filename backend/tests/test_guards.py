"""Part B — runtime guards block on a deliberately-broken invariant and refuse
to advance state / emit a statement (never emit a wrong result)."""
import pytest

from app import netting


def _lock(client):
    client.post("/cycles/1/close-uploads")
    assert client.post("/cycles/1/lock").status_code == 200


def test_canonical_vs_sources_guard_blocks(client, conn):
    """Data-breakable: a canonical invoice that diverges from its source
    obligations beyond Tier-1 tolerance must block netting + state advance."""
    _lock(client)
    ci = conn.execute(
        "SELECT canonical_invoice_id, gross_amount_minor FROM canonical_invoices "
        "WHERE ar_obligation_id IN (SELECT obligation_id FROM cycle_obligations WHERE cycle_id=1) LIMIT 1"
    ).fetchone()
    # diverge the canonical gross from its AR/AP sources by > tolerance
    conn.execute("UPDATE canonical_invoices SET gross_amount_minor=? WHERE canonical_invoice_id=?",
                 (ci["gross_amount_minor"] + 100000, ci["canonical_invoice_id"]))
    conn.commit()

    assert client.get("/netting").status_code == 409
    assert client.post("/cycles/1/net").status_code == 409
    assert conn.execute("SELECT state FROM cycles WHERE cycle_id=1").fetchone()[0] == "locked"


def test_audit_guard_blocks_netting_statement_and_state(client, conn):
    _lock(client)
    # Tamper the hash-chained audit log.
    mid = conn.execute("SELECT log_id FROM audit_log ORDER BY log_id LIMIT 1").fetchone()[0]
    conn.execute("UPDATE audit_log SET after='TAMPERED' WHERE log_id=?", (mid,))
    conn.commit()

    assert client.get("/netting").status_code == 409       # refuses to produce
    assert client.get("/statement").status_code == 409      # refuses to emit a statement
    assert client.post("/cycles/1/net").status_code == 409  # refuses to advance state
    # state did NOT advance, no net_positions written
    assert conn.execute("SELECT state FROM cycles WHERE cycle_id=1").fetchone()[0] == "locked"
    assert conn.execute("SELECT COUNT(*) FROM net_positions WHERE cycle_id=1").fetchone()[0] == 0


# Σnet=0 and reconstruction hold by construction over canonical invoices (one
# figure serves the biller + and payer −), so they can only break via a code
# regression — exercise the guard logic directly with deliberately-broken input.

def _ci(cid, amt):
    return {"canonical_invoice_id": cid, "gross_amount_minor": amt}


def test_sum_net_guard_raises():
    positions = [
        {"party_id": 1, "currency": "EUR", "net": 100,
         "reconstruction_key": {"receivable_invoice_ids": [1], "payable_invoice_ids": []}},
        {"party_id": 2, "currency": "EUR", "net": -50,    # Σ = 50 ≠ 0
         "reconstruction_key": {"receivable_invoice_ids": [], "payable_invoice_ids": [1]}},
    ]
    with pytest.raises(netting.NettingInvariantError, match="Σnet"):
        netting.check_invariants(positions, [_ci(1, 100)])


def test_reconstruction_guard_raises():
    # Σ = 0, but party 1's key sums to 100 while its net says 90 → must block.
    positions = [
        {"party_id": 1, "currency": "EUR", "net": 90,
         "reconstruction_key": {"receivable_invoice_ids": [1], "payable_invoice_ids": []}},
        {"party_id": 2, "currency": "EUR", "net": -90,
         "reconstruction_key": {"receivable_invoice_ids": [], "payable_invoice_ids": [1]}},
    ]
    with pytest.raises(netting.NettingInvariantError, match="reconstruction"):
        netting.check_invariants(positions, [_ci(1, 100)])
