"""Phase 4 done-gates: tiered matching, recompute reproducibility, manual-match survival."""
from app.matching import match_all


def _match_for(conn, inv):
    return conn.execute(
        "SELECT m.* FROM matches m JOIN obligations o ON o.obligation_id IN "
        "(m.ar_obligation_id, m.ap_obligation_id) WHERE o.invoice_number = ? LIMIT 1", (inv,)
    ).fetchone()


def test_tiers_skyline_pacific_nile(conn):
    # Tier 1 — Skyline pair confirmed.
    assert _match_for(conn, "SKY-4471")["match_status"] == "confirmed"
    # Tier 2 — Pacific €4,100 vs €3,900 → proposed (mismatch) with a delta.
    pac = _match_for(conn, "PAC-7782")
    assert pac["match_status"] == "proposed" and pac["amount_delta"] == 20000
    # Tier 3 — Nile one-sided (off-network) → no match row.
    assert _match_for(conn, "AEG-2044") is None


def test_recompute_reproduces_matches(conn):
    before = sorted(tuple(r) for r in conn.execute(
        "SELECT ar_obligation_id, ap_obligation_id, match_status FROM matches"))
    match_all(conn)
    after = sorted(tuple(r) for r in conn.execute(
        "SELECT ar_obligation_id, ap_obligation_id, match_status FROM matches"))
    assert before == after


def test_human_confirmed_match_survives_recompute(conn):
    m = conn.execute(
        "SELECT match_id, ar_obligation_id, ap_obligation_id FROM matches "
        "WHERE match_status='confirmed' LIMIT 1").fetchone()
    conn.execute("UPDATE matches SET match_tier='manual' WHERE match_id=?", (m["match_id"],))
    conn.commit()
    match_all(conn)   # full recompute
    survived = conn.execute(
        "SELECT COUNT(*) FROM matches WHERE match_tier='manual' "
        "AND ar_obligation_id=? AND ap_obligation_id=?",
        (m["ar_obligation_id"], m["ap_obligation_id"])).fetchone()[0]
    assert survived == 1


def test_tight_tolerance_surfaces_material_diff(conn):
    from app.matching import TIER1_TOLERANCE_MINOR
    assert TIER1_TOLERANCE_MINOR <= 1000          # genuine-rounding, not 1%
    assert 20000 > TIER1_TOLERANCE_MINOR          # Pacific's €200 is well beyond → Tier-2
