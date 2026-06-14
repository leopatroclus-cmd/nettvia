"""Phase 5 done-gates: disposition gate, nettable derivation, durability."""
from app.matching import match_all


def _oid(conn, inv, owner=1):
    return conn.execute(
        "SELECT obligation_id FROM obligations WHERE invoice_number=? AND owner_party_id=?",
        (inv, owner)).fetchone()[0]


def test_accept_net_blocked_on_unmatched(client, conn):
    nile = _oid(conn, "AEG-2044")          # one-sided
    r = client.post("/dispositions", json={"ids": [nile], "disp": "net"})
    assert r.status_code == 400


def test_nettable_needs_both_sides(client, conn):
    # Skyline AP €18,400 is matched; both sides default to accept&net → nettable.
    sky = _oid(conn, "SKY-4471")
    row = next(r for r in client.get("/obligations").json() if r["id"] == sky)
    assert row["match"] == "matched" and row["nettable"] is True
    # Dispute our side → no longer nettable.
    client.post("/dispositions", json={"ids": [sky], "disp": "dispute"})
    row = next(r for r in client.get("/obligations").json() if r["id"] == sky)
    assert row["nettable"] is False


def test_disposition_survives_recompute(client, conn):
    sky = _oid(conn, "SKY-4471")
    client.post("/dispositions", json={"ids": [sky], "disp": "dispute"})
    match_all(conn)   # recompute must not touch dispositions
    d = conn.execute("SELECT disposition FROM obligations WHERE obligation_id=?", (sky,)).fetchone()
    assert d["disposition"] == "disputed"
