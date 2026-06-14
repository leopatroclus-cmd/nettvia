"""Canonical-invoice minting (Phase 6.5, Delos-shaped).

When a match is CONFIRMED, mint/upsert one canonical_invoice from the matched
pair — the clean single-record OUTPUT that Phase-7 net/statement will reference.
Mismatches (proposed) and one-sided rows do NOT mint. Idempotent: re-confirm /
re-match updates the same record (anchored on ar_obligation_id); a pair that is
no longer a confirmed match has its canonical invoice removed.

Kept OUT of matching.py so the matching/netting engine stays jurisdiction-blind:
this reads jurisdictions to RECORD them, never to DECIDE a match.
"""
from datetime import datetime

from . import audit


def _juris(conn, party_id):
    if party_id is None:
        return None
    r = conn.execute("SELECT jurisdiction FROM parties WHERE party_id = ?", (party_id,)).fetchone()
    return r["jurisdiction"] if r else None


def mint(conn):
    """Reconcile canonical_invoices against the current confirmed matches."""
    confirmed = conn.execute(
        "SELECT ar_obligation_id, ap_obligation_id FROM matches WHERE match_status = 'confirmed'"
    ).fetchall()
    keep = []
    for m in confirmed:
        ar = conn.execute(
            "SELECT * FROM obligations WHERE obligation_id = ?", (m["ar_obligation_id"],)
        ).fetchone()
        if ar is None or ar["counterparty_party_id"] is None:
            continue
        # AR side: owner is the biller, counterparty is the payer.
        biller_id, payer_id = ar["owner_party_id"], ar["counterparty_party_id"]
        keep.append(ar["obligation_id"])
        fields = (
            m["ap_obligation_id"], ar["assigned_cycle_id"],
            biller_id, _juris(conn, biller_id),
            payer_id, _juris(conn, payer_id),
            ar["currency"], ar["amount"],
            ar["vat_treatment"], ar["vat_rate"], ar["vat_amount_minor"],
            ar["issue_date"], ar["due_date"], "agreed",
        )
        existing = conn.execute(
            "SELECT canonical_invoice_id FROM canonical_invoices WHERE ar_obligation_id = ?",
            (ar["obligation_id"],),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE canonical_invoices SET ap_obligation_id=?, cycle_id=?, biller_id=?, "
                "biller_jurisdiction=?, payer_id=?, payer_jurisdiction=?, currency=?, "
                "gross_amount_minor=?, vat_treatment=?, vat_rate=?, vat_amount_minor=?, "
                "issue_date=?, due_date=?, status=? WHERE ar_obligation_id=?",
                fields + (ar["obligation_id"],),
            )
        else:
            conn.execute(
                "INSERT INTO canonical_invoices "
                "(ar_obligation_id, ap_obligation_id, cycle_id, biller_id, biller_jurisdiction, "
                " payer_id, payer_jurisdiction, currency, gross_amount_minor, "
                " vat_treatment, vat_rate, vat_amount_minor, issue_date, due_date, status, "
                " service_ref, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, NULL, ?)",
                (ar["obligation_id"],) + fields + (datetime.utcnow().isoformat(),),
            )
            audit.append(
                conn, actor="system", action="match_confirmed",
                entity_ref=f"canonical_invoice:ar_obligation={ar['obligation_id']}",
                after={"biller_id": biller_id, "payer_id": payer_id,
                       "currency": ar["currency"], "gross_amount_minor": ar["amount"]},
            )

    # Drop canonical invoices whose pair is no longer a confirmed match.
    if keep:
        placeholders = ",".join("?" * len(keep))
        conn.execute(
            f"DELETE FROM canonical_invoices WHERE ar_obligation_id NOT IN ({placeholders})",
            keep,
        )
    else:
        conn.execute("DELETE FROM canonical_invoices")
    conn.commit()
