"""Seed the canonical DB with the prototype's messy mock set (+ mirror rows).

The signed-in party is Aegean Air Cargo. Its 9 obligations reproduce the
prototype's `rows` array exactly. Each matched/mismatched pair also gets the
counterparty's *mirror* obligation, so the DB is a realistic two-sided ledger
even though Aegean's Accounts view only shows Aegean-owned rows.
"""

# network_id, name, cycle_length_days, cut_off_rule, settlement_lag_days,
# term_mode, default_on_no_action, cost_per_payment, cost_components,
# upload_cutoff_offset_days, processing_cutoff_offset_days
NETWORK = (1, "NAP", 30, "last_business_day", 3, "respect_due_date", "roll",
           35.0, None, 25, 29)

CYCLE_OPENS_AT = "2026-06-01"   # the open cycle (2026-06)

# party_id, legal_name, tax_id, country, city, group_id, on_network
# Network role within NAP (party_networks.role): the operator is admin, the
# trading forwarders are participants.
PARTIES = [
    (1, "Aegean Air Cargo S.A.", "EL094019245", "GR", "Athens", None, 1, "participant"),
    (2, "Skyline Freight GmbH", "DE811907980", "DE", "Frankfurt", None, 1, "participant"),
    (3, "Pacific Forwarders Ltd", "SG198912345R", "SG", "Singapore", None, 1, "participant"),
    (4, "Levant Cargo Co", "LB10203040", "LB", "Beirut", None, 1, "participant"),
    (5, "Helvetia Air AG", "CHE116281942", "CH", "Zürich", None, 1, "participant"),
    (6, "Nile Logistics", "EG200318877", "EG", "Cairo", None, 0, "participant"),  # off-network
    (7, "Adriatic Shipping", "HR12345678901", "HR", "Rijeka", None, 1, "participant"),
    (8, "NAP Network Operator", None, "GR", "Athens", None, 0, "admin"),  # runs the network
]

# Obligations. Columns:
# id, owner, dir, cp_raw, cp_id, inv, amount, ccy, issue, due,
# status_source, ingest_state, suggested_disposition, match_note
OBLIGATIONS = [
    # --- Aegean's ledger: the 9 prototype rows, in order ---
    (1, 1, "AP", "Skyline Freight GmbH", 2, "SKY-4471", 18400, "EUR", "2026-06-02", "2026-06-24", "open", "matched", "net", None),
    (2, 1, "AR", "Skyline Freight GmbH", 2, "AEG-2025", 12250, "EUR", "2026-06-04", "2026-06-24", "open", "matched", "net", None),
    (3, 1, "AR", "Pacific Forwarders Ltd", 3, "AEG-2031", 9800, "USD", "2026-06-03", "2026-06-26", "open", "matched", "net", None),
    (4, 1, "AP", "Pacific Forwarders Ltd", 3, "PAC-7782", 4100, "USD", "2026-06-05", "2026-06-26", "open", "matched", "dispute", "their books: $3,900"),
    (5, 1, "AR", "Helvetia Air AG", 5, "AEG-2040", 6400, "CHF", "2026-06-06", "2026-06-25", "open", "matched", "net", None),
    (6, 1, "AP", "Levant Cargo Co", 4, "LEV-1190", 7650, "EUR", "2026-06-01", "2026-08-27", "open", "matched", "defer", "net-90"),
    (7, 1, "AR", "Nile Logistics", 6, "AEG-2044", 5200, "EUR", "2026-05-28", "2026-06-21", "open", "unmatched", "direct", "off-network"),
    (8, 1, "AP", "Adriatic Shipping", 7, "ADR-6651", 3300, "EUR", "2026-05-30", "2026-06-23", "open", "matched", "dispute", "service in dispute"),
    (9, 1, "AP", "Skyline Freight GmbH", 2, "SKY-4503", 7600, "EUR", "2026-06-07", "2026-06-28", "open", "matched", "net", None),

    # --- counterparty mirror rows (the "few more"): real two-sided ledger ---
    (10, 2, "AR", "Aegean Air Cargo S.A.", 1, "SKY-4471", 18400, "EUR", "2026-06-02", "2026-06-24", "open", "matched", "net", None),
    (11, 2, "AP", "Aegean Air Cargo S.A.", 1, "AEG-2025", 12250, "EUR", "2026-06-04", "2026-06-24", "open", "matched", "net", None),
    (12, 3, "AP", "Aegean Air Cargo S.A.", 1, "AEG-2031", 9800, "USD", "2026-06-03", "2026-06-26", "open", "matched", "net", None),
    (13, 3, "AR", "Aegean Air Cargo S.A.", 1, "PAC-7782", 3900, "USD", "2026-06-05", "2026-06-26", "open", "matched", "net", "amount mismatch vs counterparty"),
    (14, 5, "AP", "Aegean Air Cargo S.A.", 1, "AEG-2040", 6400, "CHF", "2026-06-06", "2026-06-25", "open", "matched", "net", None),
    (15, 4, "AR", "Aegean Air Cargo S.A.", 1, "LEV-1190", 7650, "EUR", "2026-06-01", "2026-08-27", "open", "matched", "defer", "net-90"),
    (16, 7, "AR", "Aegean Air Cargo S.A.", 1, "ADR-6651", 3300, "EUR", "2026-05-30", "2026-06-23", "open", "matched", "net", None),
    (17, 2, "AR", "Aegean Air Cargo S.A.", 1, "SKY-4503", 7600, "EUR", "2026-06-07", "2026-06-28", "open", "matched", "net", None),
]

# NOTE: matches are no longer hardcoded — the matcher (app/matching.py) computes
# them from these obligations after seeding. obligation 7 (Nile, off-network) has
# no mirror, so it stays one-sided; obligation 4 (Pacific AP €4,100) pairs with
# obligation 13 (Pacific AR €3,900) as a Tier-2 amount mismatch.


def seed(conn):
    from . import cycles, refdata
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO networks (network_id, name, cycle_length_days, cut_off_rule, "
        "settlement_lag_days, term_mode, default_on_no_action, cost_per_payment, "
        "cost_components, upload_cutoff_offset_days, processing_cutoff_offset_days) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", NETWORK)
    cur.executemany("INSERT INTO currencies (code, minor_unit_exponent) VALUES (?,?)",
                    refdata.CURRENCIES)
    # jurisdiction (ISO-3) derived from each party's country (Delos seam).
    cur.executemany(
        "INSERT INTO parties (party_id,legal_name,tax_id,country,city,group_id,on_network,jurisdiction) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [p[:7] + (refdata.iso3(p[3]),) for p in PARTIES])
    cur.executemany(
        "INSERT INTO party_networks (party_id,network_id,role) VALUES (?,1,?)",
        [(p[0], p[7]) for p in PARTIES])
    # Money is stored in minor units (cents): multiply the human figures here.
    cur.executemany(
        "INSERT INTO obligations "
        "(obligation_id,owner_party_id,direction,counterparty_raw,counterparty_party_id,"
        " network_id,invoice_number,amount,currency,issue_date,due_date,status_source,"
        " ingest_state,suggested_disposition,match_note) "
        "VALUES (?,?,?,?,?,1,?,?,?,?,?,?,?,?,?)",
        [(o[0], o[1], o[2], o[3], o[4], o[5], o[6] * 100, o[7], o[8], o[9],
          o[10], o[11], o[12], o[13]) for o in OBLIGATIONS])
    # Demo: the counterparty mirrors (owned by the other parties) represent
    # participating counterparties who have already chosen accept & net — so a
    # pair goes nettable as soon as the signed-in party (1) accepts its side.
    # The per-side nettable logic stays real; this only seeds the other side.
    cur.execute(
        "UPDATE obligations SET disposition='accepted', settlement_mode='net' "
        "WHERE owner_party_id != 1")
    # EXPLICIT accept&net decisions for the signed-in party (1) on its matched,
    # in-cycle invoices — so the demo cycle stays populated. Everything else
    # (Levant net-90, Pacific mismatch, Nile one-sided) stays genuinely PENDING
    # and follows default_on_no_action at lock.
    cur.execute(
        "UPDATE obligations SET disposition='accepted', settlement_mode='net' "
        "WHERE owner_party_id = 1 AND invoice_number IN "
        "('SKY-4471','AEG-2025','AEG-2031','AEG-2040','ADR-6651','SKY-4503')")
    # Open the first cycle (2026-06) and place every seeded obligation in it.
    cycles.open_cycle(conn, 1, CYCLE_OPENS_AT, 1)
    cur.execute("UPDATE obligations SET assigned_cycle_id = 1")
    conn.commit()
