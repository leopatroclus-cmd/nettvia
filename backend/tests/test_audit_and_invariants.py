"""Audit hash-chain, country-blindness, determinism, and savings recompute."""
import os
import re

from app import audit


def test_audit_chain_verifies(conn):
    assert audit.verify(conn)["ok"] is True


def test_tampered_audit_fails_at_right_id(conn):
    mid = conn.execute("SELECT log_id FROM audit_log ORDER BY log_id LIMIT 1 OFFSET 1").fetchone()[0]
    conn.execute("UPDATE audit_log SET after='TAMPERED' WHERE log_id=?", (mid,))
    conn.commit()
    v = audit.verify(conn)
    assert v["ok"] is False and v["broken_log_id"] == mid


def test_matching_netting_cycles_are_country_blind():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    forbidden = re.compile(r"jurisdiction|iso3|\bcountry\b", re.IGNORECASE)
    for fname in ("matching.py", "cycles.py", "netting.py"):
        src = open(os.path.join(here, "app", fname)).read()
        hits = forbidden.findall(src)
        assert not hits, f"{fname} contains country logic: {hits}"


def test_engine_is_deterministic(conn):
    from app.matching import match_all
    from app import canonical, netting

    def snapshot():
        matches = sorted(tuple(r) for r in conn.execute(
            "SELECT ar_obligation_id, ap_obligation_id, match_status, amount_delta FROM matches"))
        cis = sorted(tuple(r) for r in conn.execute(
            "SELECT ar_obligation_id, biller_id, payer_id, gross_amount_minor FROM canonical_invoices"))
        cyc = conn.execute("SELECT * FROM cycles WHERE cycle_id=1").fetchone()
        nets = sorted((p["party_id"], p["currency"], p["net"])
                      for p in netting.compute(conn, cyc)["positions"])
        return matches, cis, nets

    first = snapshot()
    match_all(conn)
    canonical.mint(conn)
    assert snapshot() == first


def test_reveal_fee_only_when_settlements_drop(client):
    d = client.get("/netting").json()
    st, cpp = d["settlements"], d["savings"]["cost_per_payment"]
    # Honest, never a double-count: fee saved == distinct settlements eliminated ×
    # cost-per-payment. The seed's flows are bilateral, so nothing drops → €0.
    assert st["fee_saved_minor"] == st["eliminated"] * int(round(cpp * 100))
    assert st["eliminated"] == 0 and st["fee_saved_minor"] == 0
