"""Phase 7 done-gates: Σnet=0, regenerable, reconstruction keys reconcile."""
from app import netting


def _cycle(conn, cid=1):
    return conn.execute("SELECT * FROM cycles WHERE cycle_id=?", (cid,)).fetchone()


def test_sum_net_zero_per_currency(conn):
    result = netting.compute(conn, _cycle(conn))
    assert result["sum_by_currency"] == {"EUR": 0, "USD": 0, "CHF": 0}


def test_recompute_identical(conn):
    a = netting.compute(conn, _cycle(conn))
    b = netting.compute(conn, _cycle(conn))
    key = lambda r: sorted((p["party_id"], p["currency"], p["net"]) for p in r["positions"])
    assert key(a) == key(b)


def test_reconstruction_keys_reconcile(conn):
    result = netting.compute(conn, _cycle(conn))
    amt = {ci["canonical_invoice_id"]: ci["gross_amount_minor"] for ci in result["invoices"]}
    recv_all, pay_all = [], []
    for p in result["positions"]:
        rk = p["reconstruction_key"]
        recon = (sum(amt[i] for i in rk["receivable_invoice_ids"])
                 - sum(amt[i] for i in rk["payable_invoice_ids"]))
        assert recon == p["net"]                       # key sums to the net figure
        recv_all += rk["receivable_invoice_ids"]
        pay_all += rk["payable_invoice_ids"]
    ids = sorted(amt)
    assert sorted(recv_all) == ids                     # every invoice once on the receivable side
    assert sorted(pay_all) == ids                      # ...and once on the payable side


def test_known_party_positions(client):
    d = client.get("/netting").json()
    pos = {p["currency"]: p["net_major"] for p in d["party"]["positions"]}
    assert pos == {"EUR": -17050, "USD": 9800, "CHF": 6400}
    assert d["party"]["gross"] == 6 and d["party"]["net"] == 3        # compression 6→3
    assert d["network"]["eliminated"] == 5


def test_netting_over_locked_snapshot_matches_provisional(client, conn):
    provisional = {p["currency"]: p["net_minor"] for p in client.get("/netting").json()["party"]["positions"]}
    client.post("/cycles/1/close-uploads")
    client.post("/cycles/1/lock")
    final = {p["currency"]: p["net_minor"] for p in client.post("/cycles/1/net").json()["party"]["positions"]}
    assert provisional == final
    assert conn.execute("SELECT state FROM cycles WHERE cycle_id=1").fetchone()[0] == "netted"
