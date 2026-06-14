"""Phase 6.5 done-gates: canonical-invoice minting (confirmed only, biller/payer, idempotent)."""
from app import canonical


def test_minted_only_on_confirmed_matches(conn):
    n_canonical = conn.execute("SELECT COUNT(*) FROM canonical_invoices").fetchone()[0]
    n_confirmed = conn.execute("SELECT COUNT(*) FROM matches WHERE match_status='confirmed'").fetchone()[0]
    assert n_canonical == n_confirmed == 7


def test_biller_payer_from_owner_direction(conn):
    # AEG-2025 is Aegean's AR to Skyline → biller = Aegean (1), payer = Skyline (2).
    ar = conn.execute("SELECT obligation_id FROM obligations WHERE invoice_number='AEG-2025' AND direction='AR'").fetchone()[0]
    ci = conn.execute("SELECT * FROM canonical_invoices WHERE ar_obligation_id=?", (ar,)).fetchone()
    assert ci["biller_id"] == 1 and ci["payer_id"] == 2
    assert ci["biller_jurisdiction"] == "GRC" and ci["payer_jurisdiction"] == "DEU"
    # biller's (AR-side) figure is the canonical gross
    assert ci["gross_amount_minor"] == 1225000


def test_mismatch_and_one_sided_excluded(conn):
    # Pacific mismatch (PAC-7782) and Nile one-sided (AEG-2044) have no canonical invoice.
    for inv in ("PAC-7782", "AEG-2044"):
        oid = conn.execute("SELECT obligation_id FROM obligations WHERE invoice_number=? AND owner_party_id=1", (inv,)).fetchone()[0]
        n = conn.execute(
            "SELECT COUNT(*) FROM canonical_invoices WHERE ar_obligation_id=? OR ap_obligation_id=?",
            (oid, oid)).fetchone()[0]
        assert n == 0


def test_mint_is_idempotent(conn):
    before = sorted(tuple(r) for r in conn.execute(
        "SELECT canonical_invoice_id, ar_obligation_id, gross_amount_minor FROM canonical_invoices"))
    canonical.mint(conn)
    canonical.mint(conn)
    after = sorted(tuple(r) for r in conn.execute(
        "SELECT canonical_invoice_id, ar_obligation_id, gross_amount_minor FROM canonical_invoices"))
    assert before == after
