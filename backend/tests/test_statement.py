"""Phase C done-gate: the netting statement's network headline + per-party math.

Builds the EUR ring (5 parties, 8 EUR invoices) on top of the seeded parties,
nets it via /demo/advance, and asserts the summary: Σnet=0, gross→net,
compression, per-party net positions, payments, savings, and FX=0.
"""
from app import canonical, matching

# (ref, biller_id, payer_id, amount_major) — a circular EUR flow that nets down
# to small residuals. Gross 53,100 → net 1,100 (~98% compression).
RING = [
    ("RING-1", 1, 2, 9800),
    ("RING-2", 2, 3, 11400),
    ("RING-3", 3, 5, 8400),
    ("RING-4", 5, 4, 10600),
    ("RING-5", 4, 1, 9900),
    ("RING-6", 3, 5, 1600),
    ("RING-7", 3, 2, 1100),
    ("RING-8", 4, 1, 300),
]


def _build_ring(conn):
    names = {r["party_id"]: r["legal_name"]
             for r in conn.execute("SELECT party_id, legal_name FROM parties")}

    def ins(owner, direction, cp, ref, amt):
        conn.execute(
            "INSERT INTO obligations (owner_party_id, direction, counterparty_raw, "
            "counterparty_party_id, network_id, invoice_number, amount, currency, "
            "issue_date, due_date, status_source, assigned_cycle_id, ingest_state, "
            "disposition, settlement_mode) "
            "VALUES (?,?,?,?,1,?,?, 'EUR', '2026-06-05', '2026-06-25', 'open', 1, "
            "'ingested', 'accepted', 'net')",
            (owner, direction, names[cp], cp, ref, amt * 100))

    for ref, biller, payer, amt in RING:
        ins(biller, "AR", payer, ref, amt)     # biller's receivable
        ins(payer, "AP", biller, ref, amt)     # payer's payable (mirror)
    matching.match_all(conn)
    canonical.mint(conn)
    conn.commit()


def test_eur_ring_statement_math(client, conn):
    client.post("/demo/reset")                 # clean baseline: parties only, fresh cycle 1
    _build_ring(conn)
    adv = client.post("/demo/advance")
    assert adv.status_code == 200

    s = client.get("/statement/summary", params={"cycle_id": 1}).json()
    net = s["network"]

    # Headline: 8 invoices netted across 5 parties.
    assert net["invoices_netted"] == 8
    assert net["parties_in_net"] == 5

    # Gross → net (EUR), single currency.
    gross = {m["currency"]: m["major"] for m in net["gross_settled"]}
    settle = {m["currency"]: m["major"] for m in net["net_to_settle"]}
    assert gross["EUR"] == 53100
    assert settle["EUR"] == 1100                # Σ positive nets = 500 + 600
    assert net["compression_pct"] == 98         # 1 − 1100/53100

    # Per-party net positions sum to zero and hit the targets.
    pos = {}
    for p in s["parties"]:
        eur = [x for x in p["positions"] if x["currency"] == "EUR"]
        pos[p["party_id"]] = eur[0]["net_major"] if eur else 0
    assert pos == {1: -400, 2: 500, 3: -300, 5: 600, 4: -400}
    assert sum(pos.values()) == 0

    # HONEST network figure: 5 net settlements (one per netting party). No
    # inflated per-cycle "fees avoided / payments eliminated" is reported.
    assert net["net_settlements"] == 5
    assert "fees_avoided_minor" not in net and "savings_minor" not in net
    assert "gross_payments" not in net
    assert s["assumptions"]["wire_fee_major"] == 30.0
    assert s["assumptions"]["fx_rate_pct"] == 0.6

    # AT-SCALE projection (separate from this cycle): fees + FX at monthly volume.
    proj = s["projection"]
    assert proj["monthly_volume"] == 1000
    assert proj["monthly_fees_minor"] == 1000 * 3000            # volume × €30
    assert proj["avg_ticket_minor"] == 5310000 // 8             # cycle mean ticket
    assert proj["monthly_fx_minor"] == int(1000 * (5310000 // 8) * 0.006)
    assert proj["monthly_total_minor"] == proj["monthly_fees_minor"] + proj["monthly_fx_minor"]

    # Per-party detail: invoices listed, payments N → 1 (honest at party level),
    # one-fewer-movement saving = (n − 1) × fee.
    by_id = {p["party_id"]: p for p in s["parties"]}
    aegean = by_id[1]
    assert aegean["invoices_netted"] == 3 and len(aegean["invoice_list"]) == 3
    assert aegean["gross_payments"] == 3 and aegean["net_payments"] == 1
    assert aegean["money_saved_major"] == 60          # (3 − 1) × €30
    assert by_id[3]["money_saved_major"] == 90        # Pacific is in 4 invoices
    assert {e["ref"] for e in aegean["invoice_list"]} == {"RING-1", "RING-5", "RING-8"}


def test_summary_assumptions_adjustable(client):
    """Wire fee, FX rate, and monthly volume are adjustable per request and
    drive the at-scale projection."""
    client.post("/demo/advance")               # net the seeded cycle
    s = client.get("/statement/summary",
                   params={"wire_fee": 50, "fx_rate": 0.01, "monthly_volume": 2000}).json()
    assert s["assumptions"]["wire_fee_major"] == 50.0
    assert s["assumptions"]["fx_rate_pct"] == 1.0
    assert s["projection"]["monthly_volume"] == 2000
    assert s["projection"]["monthly_fees_minor"] == 2000 * 5000   # 2000 × €50
