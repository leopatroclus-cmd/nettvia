"""Phase 3 done-gates: layered entity resolution + alias persistence + on_network."""
from conftest import AP_EXPORT, upload_confirm


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
