"""Multilateral netting (Phase 7) — computed over CANONICAL INVOICES.

Net positions are a REGENERABLE projection of a cycle's frozen snapshot (or, for
an open/reconciling cycle, a provisional view of the current nettable set). They
are recomputed from the canonical invoices each time, never treated as the source
of truth.

Per party per currency (Delos-style):
    receivable = Σ canonical where biller = party
    payable    = Σ canonical where payer  = party
    net        = receivable − payable
Σ net = 0 per currency by construction — canonical uses ONE figure (the biller's
agreed amount) as the biller's + and the payer's −.
"""
from . import cycles


class NettingInvariantError(Exception):
    """A core netting invariant failed — block, never emit a wrong result."""


def _netting_invoices(conn, cycle):
    """Canonical invoices in this cycle's netting set, and whether provisional."""
    cid = cycle["cycle_id"]
    if cycle["state"] in ("locked", "netted", "closed"):
        rows = conn.execute(
            "SELECT * FROM canonical_invoices WHERE ar_obligation_id IN "
            "(SELECT obligation_id FROM cycle_obligations WHERE cycle_id = ?)",
            (cid,),
        ).fetchall()
        return rows, False
    # Provisional: confirmed canonical invoices in this cycle where BOTH sides
    # currently choose accept & net.
    horizon = cycle["processing_cutoff_at"]
    out = []
    for ci in conn.execute("SELECT * FROM canonical_invoices WHERE cycle_id = ?", (cid,)):
        ar = conn.execute("SELECT * FROM obligations WHERE obligation_id = ?",
                          (ci["ar_obligation_id"],)).fetchone()
        ap = conn.execute("SELECT * FROM obligations WHERE obligation_id = ?",
                          (ci["ap_obligation_id"],)).fetchone()
        if (ar and ap
                and cycles.effective_code(ar, "matched", horizon) == "net"
                and cycles.effective_code(ap, "matched", horizon) == "net"):
            out.append(ci)
    return out, True


def _verify_against_sources(conn, invoices):
    """Data-breakable guard: each canonical invoice must reconcile to BOTH source
    obligations (AR and AP) within the Tier-1 tolerance, in the same currency.
    Validates the net (canonical) against the two-sided gross."""
    from .matching import TIER1_TOLERANCE_MINOR
    for ci in invoices:
        ar = conn.execute("SELECT amount, currency FROM obligations WHERE obligation_id = ?",
                          (ci["ar_obligation_id"],)).fetchone()
        ap = conn.execute("SELECT amount, currency FROM obligations WHERE obligation_id = ?",
                          (ci["ap_obligation_id"],)).fetchone()
        cid, gross = ci["canonical_invoice_id"], ci["gross_amount_minor"]
        if ar is None or ap is None:
            raise NettingInvariantError(f"canonical invoice {cid} is missing a source obligation")
        if ci["currency"] != ar["currency"] or ci["currency"] != ap["currency"]:
            raise NettingInvariantError(f"canonical invoice {cid} currency disagrees with its sources")
        if abs(ar["amount"] - gross) > TIER1_TOLERANCE_MINOR:
            raise NettingInvariantError(f"canonical invoice {cid} diverges from its AR source")
        if abs(ap["amount"] - gross) > TIER1_TOLERANCE_MINOR:
            raise NettingInvariantError(f"canonical invoice {cid} diverges from its AP source")


def compute(conn, cycle):
    """Return the netting result for a cycle (regenerable from canonical invoices)."""
    invoices, provisional = _netting_invoices(conn, cycle)
    _verify_against_sources(conn, invoices)   # GUARD — net vs two-sided gross

    pos = {}   # (party_id, currency) -> position

    def slot(party_id, currency):
        key = (party_id, currency)
        if key not in pos:
            pos[key] = {"party_id": party_id, "currency": currency,
                        "receivable": 0, "payable": 0,
                        "receivable_invoice_ids": [], "payable_invoice_ids": []}
        return pos[key]

    for ci in invoices:
        cur, amt, cid = ci["currency"], ci["gross_amount_minor"], ci["canonical_invoice_id"]
        b = slot(ci["biller_id"], cur)
        b["receivable"] += amt
        b["receivable_invoice_ids"].append(cid)
        p = slot(ci["payer_id"], cur)
        p["payable"] += amt
        p["payable_invoice_ids"].append(cid)

    positions = []
    for s in pos.values():
        s["net"] = s["receivable"] - s["payable"]
        s["reconstruction_key"] = {
            "receivable_invoice_ids": s["receivable_invoice_ids"],
            "payable_invoice_ids": s["payable_invoice_ids"],
        }
        positions.append(s)

    sum_by_currency = check_invariants(positions, invoices)
    return {"invoices": invoices, "positions": positions,
            "provisional": provisional, "sum_by_currency": sum_by_currency}


def check_invariants(positions, invoices):
    """Blocking guards (Phase 8): raise NettingInvariantError on a broken core
    invariant. Returns Σnet per currency when all hold."""
    # GUARD 1 — Σ net = 0 per currency.
    sum_by_currency = {}
    for s in positions:
        sum_by_currency[s["currency"]] = sum_by_currency.get(s["currency"], 0) + s["net"]
    bad = {c: t for c, t in sum_by_currency.items() if t != 0}
    if bad:
        raise NettingInvariantError(f"Σnet != 0 per currency: {bad}")

    # GUARD 2 — reconstruction keys reconcile: every invoice appears exactly once
    # per side, and each key sums back to its net figure.
    amt = {ci["canonical_invoice_id"]: ci["gross_amount_minor"] for ci in invoices}
    recv_seen, pay_seen = [], []
    for s in positions:
        recv = s["reconstruction_key"]["receivable_invoice_ids"]
        pay = s["reconstruction_key"]["payable_invoice_ids"]
        if sum(amt[i] for i in recv) - sum(amt[i] for i in pay) != s["net"]:
            raise NettingInvariantError(
                f"reconstruction key does not reconcile for party {s['party_id']} {s['currency']}")
        recv_seen += recv
        pay_seen += pay
    inv_ids = sorted(amt)
    if sorted(recv_seen) != inv_ids or sorted(pay_seen) != inv_ids:
        raise NettingInvariantError("reconstruction keys do not cover every invoice once per side")
    return sum_by_currency


def compression(invoices, positions):
    """Per-party gross (invoices a party is in) and net (non-zero positions)."""
    gross, net = {}, {}
    for ci in invoices:
        gross[ci["biller_id"]] = gross.get(ci["biller_id"], 0) + 1
        gross[ci["payer_id"]] = gross.get(ci["payer_id"], 0) + 1
    for s in positions:
        if s["net"] != 0:
            net[s["party_id"]] = net.get(s["party_id"], 0) + 1
    return gross, net


def settlement_counts(invoices, positions):
    """Honest DISTINCT-settlement reduction from multilateral netting — never a
    double-count, so the reveal can't contradict the statement.

    before = distinct (party-pair, currency) with a non-zero net BILATERAL balance
             (the realistic baseline: treasuries already net bilaterally with each
             counterparty, so each such pair is one settlement);
    after  = non-zero multilateral net positions (each settles once with the pool).

    On a closed ring before == after (every party already settles once, in and out
    cancel) → 0 eliminated. Multilateral netting only drops distinct settlements
    when debt CYCLES collapse (bilateral/hub flows with many legs per pair)."""
    bal = {}
    for ci in invoices:
        b, p = ci["biller_id"], ci["payer_id"]
        lo, hi = (b, p) if b < p else (p, b)
        sign = 1 if b == lo else -1
        key = (lo, hi, ci["currency"])
        bal[key] = bal.get(key, 0) + sign * ci["gross_amount_minor"]
    before = sum(1 for v in bal.values() if v != 0)
    after = sum(1 for s in positions if s["net"] != 0)
    return before, after
