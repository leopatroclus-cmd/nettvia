"""Phase 3 done-gates: layered entity resolution + alias persistence + on_network."""
import os

from conftest import AP_EXPORT, upload_confirm

_SAMPLE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "sample_data", "netting-test-upload.csv")


def test_tax_id_auto_resolves(client, conn):
    _, confirm = upload_confirm(client, "ap.csv", AP_EXPORT)
    # Skyline carries a matching VAT → deterministic auto-resolve to party 2.
    row = conn.execute(
        "SELECT counterparty_party_id FROM obligations WHERE invoice_number='INV-7001'"
    ).fetchone()
    assert row["counterparty_party_id"] == 2
    # ...and was NOT surfaced for confirmation.
    pending = [p["raw_name"] for p in confirm.json()["pending"]]
    assert "SKYLINE FRT GMBH" not in pending


def test_fuzzy_proposes_with_candidate(client):
    _, confirm = upload_confirm(client, "ap.csv", AP_EXPORT)
    pending = {p["raw_name"]: p for p in confirm.json()["pending"]}
    assert "Pacific Forwarders" in pending
    cand = pending["Pacific Forwarders"]["candidate"]
    assert cand and cand["legal_name"] == "Pacific Forwarders Ltd"      # proposed, needs human OK
    assert "Meridian Cargo SARL" in pending                            # genuinely new


def test_confirmed_alias_auto_resolves_next_time(client, conn):
    upload_confirm(client, "ap.csv", AP_EXPORT)
    client.post("/resolutions/confirm", json={"raw_name": "Pacific Forwarders", "party_id": 3})
    # alias persisted
    alias = conn.execute(
        "SELECT resolved_party_id, match_status FROM counterparty_aliases "
        "WHERE lower(raw_name)='pacific forwarders'").fetchone()
    assert alias["resolved_party_id"] == 3 and alias["match_status"] == "confirmed"
    # re-upload → auto-resolves, no longer pending
    _, confirm2 = upload_confirm(client, "ap.csv", AP_EXPORT)
    assert "Pacific Forwarders" not in [p["raw_name"] for p in confirm2.json()["pending"]]


def test_on_network_derived(client, conn):
    upload_confirm(client, "ap.csv", AP_EXPORT)
    # Skyline uploads its own ledger (owns obligations) → on-network.
    assert conn.execute("SELECT on_network FROM parties WHERE party_id=2").fetchone()[0] == 1
    # "It's new" creates an off-network party.
    res = client.post("/resolutions/confirm", json={"raw_name": "Meridian Cargo SARL"}).json()
    assert res["on_network"] is False


# ── Phase 8.6: resolve against existing parties, not just confirmed aliases ──

def test_name_matching_existing_legal_name_resolves(client, conn):
    """An uploaded counterparty whose name matches an existing party's
    legal_name resolves to that party — even with no alias seeded yet."""
    csv = ("Doc No,Trading Partner,Gross Amount,Ccy,Invoice Date,Maturity,Ledger Type\n"
           "X-1,Skyline Freight GmbH,\"1,000.00\",EUR,2026-06-10,2026-06-30,Purchase\n")
    _, confirm = upload_confirm(client, "exact.csv", csv)
    row = conn.execute(
        "SELECT counterparty_party_id FROM obligations WHERE invoice_number='X-1'"
    ).fetchone()
    assert row["counterparty_party_id"] == 2          # auto-confirmed to Skyline
    assert "Skyline Freight GmbH" not in [p["raw_name"] for p in confirm.json()["pending"]]


def test_sample_upload_resolves_seeded_names_only_new_surfaces(client, conn):
    """Done-gate: re-uploading the sample as Aegean resolves the six seeded
    counterparties to their existing parties, both Pacific variants collapse to
    the single Pacific party, and ONLY 'Baltic Freight Lines' surfaces as new —
    with no duplicate parties created."""
    before = conn.execute("SELECT COUNT(*) FROM parties").fetchone()[0]
    with open(_SAMPLE, "rb") as f:
        data = f.read()
    _, confirm = upload_confirm(client, "netting-test-upload.csv", data)
    pending = {p["raw_name"]: p for p in confirm.json()["pending"]}

    # The six seeded counterparties resolve to their existing parties.
    expect = {"Skyline Freight GmbH": 2, "Helvetia Air AG": 5, "Levant Cargo Co": 4,
              "Nile Logistics": 6, "Adriatic Shipping": 7, "Pacific Forwarders Ltd": 3}
    for name, pid in expect.items():
        owned = conn.execute(
            "SELECT counterparty_party_id FROM obligations "
            "WHERE owner_party_id=1 AND lower(counterparty_raw)=lower(?)", (name,)
        ).fetchall()
        assert owned and all(r["counterparty_party_id"] == pid for r in owned), name

    # "Pacific Forwarders Limited" resolves to the SAME Pacific party (3) — as a
    # proposal with the correct candidate — never a new duplicate.
    plimited = pending.get("Pacific Forwarders Limited")
    assert plimited and plimited["candidate"]["party_id"] == 3

    # Only Baltic is genuinely new (no candidate party).
    new_names = [n for n, p in pending.items() if not p["candidate"]]
    assert new_names == ["Baltic Freight Lines"]

    # No duplicate parties: only Baltic could be created, and only on confirm.
    assert conn.execute("SELECT COUNT(*) FROM parties").fetchone()[0] == before
    assert conn.execute(
        "SELECT COUNT(*) FROM parties WHERE legal_name LIKE 'Pacific%'"
    ).fetchone()[0] == 1
