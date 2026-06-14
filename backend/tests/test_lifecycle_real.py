"""Phase D.3: lifecycle tests that drive the REAL app path end-to-end.

Earlier lifecycle tests hand-inserted disposition='accepted'/settlement_mode='net'
— the exact state lock's freeze SELECTed on — so they passed while the live app
(which leaves uploaded rows pending and shows them nettable via the suggested
fallback) froze 0. These exercise POST /upload + confirm and POST /dispositions
through the actual endpoints, then close-uploads + lock, asserting frozen > 0 and
GET /netting gross > 0.
"""
_H = "Invoice,Counterparty,Amount,Currency,Direction,Due\n"


def _upload(client, party_id, rows):
    client.post("/demo/session", json={"party_id": party_id})
    r = client.post("/upload", files={"file": ("l.csv", _H + rows, "text/csv")}).json()
    return client.post(f"/upload/{r['batch_id']}/confirm").json()


def _build_pair(client):
    """A bilateral nettable pair built via real uploads: Aegean AP↔Skyline AR
    (€11,000) and Aegean AR↔Levant AP (€10,600), all matched in the open cycle."""
    client.post("/demo/reset")
    _upload(client, 1, "SKY-A,Skyline Freight GmbH,11000,EUR,Purchase,2026-06-25\n"
                       "LEV-A,Levant Cargo Co,10600,EUR,Sales,2026-06-25\n")
    _upload(client, 2, "SKY-A,Aegean Air Cargo S.A.,11000,EUR,Sales,2026-06-25\n")
    _upload(client, 4, "LEV-A,Aegean Air Cargo S.A.,10600,EUR,Purchase,2026-06-25\n")
    client.post("/demo/session", json={"party_id": 1})


def test_real_accept_net_then_lock_freezes_and_nets(client, conn):
    _build_pair(client)
    # Accept & net through the ACTUAL endpoint, every side.
    for owner in (1, 2, 4):
        client.post("/demo/session", json={"party_id": owner})
        ids = [r[0] for r in conn.execute(
            "SELECT obligation_id FROM obligations WHERE owner_party_id=?", (owner,))]
        for oid in ids:
            assert client.post("/dispositions", json={"ids": [oid], "disp": "net"}).status_code == 200
    client.post("/demo/session", json={"party_id": 1})

    client.post("/cycles/1/close-uploads")
    frozen = client.post("/cycles/1/lock").json()["frozen"]
    assert frozen > 0                                          # 4 obligations (2 pairs)
    assert client.get("/netting").json()["party"]["gross"] > 0
    client.post("/cycles/1/net")
    gross = {m["currency"]: m["major"]
             for m in client.get("/statement/summary", params={"cycle_id": 1}).json()["network"]["gross_settled"]}
    assert gross.get("EUR") == 21600                           # 11,000 + 10,600


def test_real_pending_uploads_are_nettable_and_freeze(client, conn):
    """The EXACT live failure: rows uploaded but NEVER explicitly accepted. The UI
    shows them nettable (suggested fallback) and provisional /netting counts them —
    so lock MUST freeze them too (locked snapshot == provisional view)."""
    _build_pair(client)

    # Uploaded, not accepted: raw disposition is pending...
    assert all(r["disposition"] == "pending" for r in conn.execute(
        "SELECT disposition FROM obligations WHERE owner_party_id=1"))
    # ...yet the app presents them as nettable, and provisional netting counts them.
    obs = client.get("/obligations").json()
    assert obs and all(o["nettable"] and o["disp"] == "net" for o in obs)
    provisional_gross = client.get("/netting").json()["party"]["gross"]
    assert provisional_gross > 0

    client.post("/cycles/1/close-uploads")
    frozen = client.post("/cycles/1/lock").json()["frozen"]
    assert frozen > 0, "lock must freeze what the UI/provisional view show as nettable"
    # Locked snapshot agrees with the provisional view (no drop to 0).
    assert client.get("/netting").json()["party"]["gross"] == provisional_gross
