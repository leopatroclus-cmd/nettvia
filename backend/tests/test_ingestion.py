"""Phase 2 done-gates: ingestion + idempotency, schema mapping, currency exponent."""
from conftest import AP_EXPORT, upload_confirm, obligation_count


def test_ingest_creates_canonical_obligations(client, conn):
    before = obligation_count(conn)
    up, confirm = upload_confirm(client, "ap.csv", AP_EXPORT)
    assert confirm.status_code == 200
    assert confirm.json()["imported"] == 3
    assert obligation_count(conn) == before + 3
    # values normalized: Skyline AP €5,400 -> 540000 minor, direction AP (Purchase)
    row = conn.execute(
        "SELECT amount, currency, direction FROM obligations WHERE invoice_number='INV-7001'"
    ).fetchone()
    assert (row["amount"], row["currency"], row["direction"]) == (540000, "EUR", "AP")


def test_reupload_is_idempotent(client, conn):
    upload_confirm(client, "ap.csv", AP_EXPORT)
    after_first = obligation_count(conn)
    upload_confirm(client, "ap.csv", AP_EXPORT)   # same file again
    assert obligation_count(conn) == after_first   # no duplicates


def test_schema_mapping_deterministic_fallback(client):
    # No ANTHROPIC_API_KEY in tests → heuristic mapping must still resolve headers.
    up = client.post("/upload", files={"file": ("ap.csv", AP_EXPORT, "text/csv")}).json()
    assert up["mapping_method"] == "heuristic"
    m = {row["src"]: row["dst"] for row in up["mapping"]}
    assert m["Doc No"] == "invoice_number"
    assert m["Counterparty"] == "counterparty"
    assert m["Amount"] == "amount"
    assert m["Ccy"] == "currency"
    assert m["Due"] == "due_date"
    assert m["Type"] == "direction"
    assert m["Counterparty VAT"] == "counterparty_tax_id"   # not grabbed as amount/etc.


def test_currency_exponent_roundtrip(client, conn):
    # JPY (0-decimal) and BHD (3-decimal) must round-trip via the exponent, not /100.
    csv = (
        "Doc No,Counterparty,Amount,Ccy,Issued,Due,Type\n"
        "JPY-1,Tokyo Air KK,10000,JPY,2026-06-10,2026-06-28,Purchase\n"
        "BHD-1,Manama Cargo,1.234,BHD,2026-06-10,2026-06-28,Purchase\n"
        "EUR-1,Decimal GmbH,1840.50,EUR,2026-06-10,2026-06-28,Purchase\n"
    )
    upload_confirm(client, "multi.csv", csv)
    # stored minor units
    jpy = conn.execute("SELECT amount, currency FROM obligations WHERE invoice_number='JPY-1'").fetchone()
    bhd = conn.execute("SELECT amount FROM obligations WHERE invoice_number='BHD-1'").fetchone()
    assert jpy["amount"] == 10000      # exp 0 → 10000, NOT 1_000_000
    assert bhd["amount"] == 1234       # exp 3 → 1.234 * 1000

    # displayed major value via the API
    amts = {(r["c"], r["amt"]) for r in client.get("/obligations").json()}
    assert ("JPY", 10000) in amts
    assert ("BHD", 1.234) in amts
    assert ("EUR", 1840.5) in amts
