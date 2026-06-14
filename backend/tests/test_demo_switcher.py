"""Phase A done-gates: demo party switcher drives attribution.

The 'acting as' party (set via /demo/session) determines who an upload's
obligations are owned by, who a disposition acts as, and whose partners
/parties returns. A matched pair can be accepted as both sides to go nettable.
"""
from conftest import upload_confirm

# Skyline's own ledger: an invoice against Aegean that mirrors a seeded pair.
SKYLINE_LEDGER = (
    "Doc No,Counterparty,Amount,Ccy,Issued,Due,Type\n"
    "SKY-9001,Aegean Air Cargo S.A.,\"4,000.00\",EUR,2026-06-10,2026-06-30,Sales\n"
)


def test_session_lists_parties_and_switches(client):
    s = client.get("/demo/session").json()
    assert s["current_party_id"] == 1                       # defaults to Aegean
    assert {p["party_id"] for p in s["parties"]} >= {1, 2, 3}
    res = client.post("/demo/session", json={"party_id": 2})
    assert res.status_code == 200 and res.json()["current_party_id"] == 2
    assert client.get("/demo/session").json()["current_party_id"] == 2
    # Unknown party is rejected.
    assert client.post("/demo/session", json={"party_id": 999}).status_code == 404


def test_upload_attributes_to_current_party(client, conn):
    client.post("/demo/session", json={"party_id": 2})       # act as Skyline
    upload_confirm(client, "skyline.csv", SKYLINE_LEDGER)
    owner = conn.execute(
        "SELECT owner_party_id FROM obligations WHERE invoice_number='SKY-9001'"
    ).fetchone()["owner_party_id"]
    assert owner == 2                                        # owned by Skyline, not Aegean
    # GET /parties now returns Skyline's partners (and excludes Skyline itself).
    ids = {p["party_id"] for p in client.get("/parties").json()}
    assert 2 not in ids and 1 in ids


def test_disposition_acts_as_current_party(client, conn):
    # Aegean's obligation 1 (SKY-4471) is matched; acting as Skyline must NOT be
    # able to dispose of Aegean's row...
    client.post("/demo/session", json={"party_id": 2})
    blocked = client.post("/dispositions", json={"ids": [1], "disp": "dispute"})
    assert blocked.status_code == 404                       # not Skyline's obligation

    # ...but acting as Aegean, the same call succeeds and is attributed to Aegean.
    client.post("/demo/session", json={"party_id": 1})
    ok = client.post("/dispositions", json={"ids": [1], "disp": "dispute"})
    assert ok.status_code == 200
    entry = conn.execute(
        "SELECT actor FROM audit_log WHERE entity_ref='obligation:1' "
        "AND action='disposition_set' ORDER BY log_id DESC LIMIT 1"
    ).fetchone()
    assert entry["actor"] == "party:1"


def test_both_sides_accept_makes_pair_nettable(client, conn):
    """The same matched invoice accepted as Skyline and then as Aegean goes
    nettable — the core two-sided demo flow."""
    # Seeded pair: Aegean AP SKY-4471 (ob 1) <-> Skyline AR SKY-4471 (ob 10).
    # Reset both sides to pending first so we drive them through the switcher.
    conn.execute("UPDATE obligations SET disposition='pending', settlement_mode=NULL "
                 "WHERE obligation_id IN (1, 10)")
    conn.commit()

    client.post("/demo/session", json={"party_id": 2})       # Skyline accepts its side
    assert client.post("/dispositions", json={"ids": [10], "disp": "net"}).status_code == 200

    client.post("/demo/session", json={"party_id": 1})       # Aegean accepts its side
    assert client.post("/dispositions", json={"ids": [1], "disp": "net"}).status_code == 200

    # Aegean now sees the row as nettable (both sides accept & net + matched).
    row = next(o for o in client.get("/obligations").json() if o["id"] == 1)
    assert row["nettable"] is True
