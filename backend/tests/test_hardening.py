"""Phase 8.5 — pre-real-data hardening: the Part C backlog is fixed."""
from conftest import upload_confirm, obligation_count


def test_inbatch_duplicate_flagged_not_collapsed(client, conn):
    csv = (
        "Doc No,Counterparty,Amount,Ccy,Issued,Due,Type\n"
        "DUP-1,Acme Co,100.00,EUR,2026-06-10,2026-06-28,Purchase\n"
        "DUP-1,Acme Co,200.00,EUR,2026-06-10,2026-06-28,Purchase\n"
    )
    before = obligation_count(conn)
    _, confirm = upload_confirm(client, "dup.csv", csv)
    j = confirm.json()
    # both rows flagged (never last-wins), nothing imported
    assert j["imported"] == 0
    assert j["skipped_breakdown"].get("duplicate_in_batch") == 2
    assert obligation_count(conn) == before


def test_reused_invoice_across_counterparties_imports(client, conn):
    # same invoice number + direction but DIFFERENT counterparties → both legit.
    csv = (
        "Doc No,Counterparty,Amount,Ccy,Issued,Due,Type\n"
        "SHARED-1,Acme Co,100.00,EUR,2026-06-10,2026-06-28,Purchase\n"
        "SHARED-1,Beta Ltd,200.00,EUR,2026-06-10,2026-06-28,Purchase\n"
    )
    _, confirm = upload_confirm(client, "shared.csv", csv)
    assert confirm.json()["imported"] == 2
    n = conn.execute("SELECT COUNT(*) FROM obligations WHERE invoice_number='SHARED-1'").fetchone()[0]
    assert n == 2


def test_zero_amount_line_imports(client, conn):
    csv = (
        "Doc No,Counterparty,Amount,Ccy,Issued,Due,Type\n"
        "Z-1,Acme Co,0.00,EUR,2026-06-10,2026-06-28,Sales\n"
    )
    _, confirm = upload_confirm(client, "zero.csv", csv)
    assert confirm.json()["imported"] == 1     # legitimate 0.00 imports, not dropped
    row = conn.execute("SELECT amount FROM obligations WHERE invoice_number='Z-1'").fetchone()
    assert row["amount"] == 0


def test_unknown_currency_flagged_not_imported(client, conn):
    csv = (
        "Doc No,Counterparty,Amount,Ccy,Issued,Due,Type\n"
        "XC-1,Acme Co,100.00,XYZ,2026-06-10,2026-06-28,Sales\n"
    )
    up, confirm = upload_confirm(client, "xc.csv", csv)
    assert up["attention_breakdown"].get("unknown_currency") == 1
    assert confirm.json()["imported"] == 0     # not silently imported at default exponent
    assert confirm.json()["skipped_breakdown"].get("unknown_currency") == 1
    assert conn.execute("SELECT COUNT(*) FROM obligations WHERE currency='XYZ'").fetchone()[0] == 0
