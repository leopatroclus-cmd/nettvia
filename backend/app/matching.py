"""Transaction matching (spec §6c): pair AR obligations to their mirror AP on
the counterparty's ledger (A's AR to B ↔ B's AP to A), across resolved parties.

Tiered:
  1. Tier 1 (exact, auto-confirm) — same normalized invoice_number + amount
     within tolerance + same currency + resolved pair → match_status='confirmed'.
  2. Tier 2 (fuzzy, review) — invoice_number matches but amount beyond tolerance
     (the mismatch case), OR a strong partial (amount within tolerance +
     same due_date, invoice missing/differing) → match_status='proposed'.
  3. Tier 3 (exception) — no confident mirror → left unmatched (one-sided).

This pairs obligations only — no disposition, no netting. The full set is
recomputed each run (idempotent), so it is safe to call after any ingest /
resolution change.
"""
import re

# Tier-1 auto-confirm amount tolerance: a small FIXED minor-unit threshold for
# genuine rounding / FX noise only — NOT a percentage (1% of €5,000 = €50 would
# mask a real disagreement). Anything beyond this drops to Tier-2 (Mismatch).
TIER1_TOLERANCE_MINOR = 100   # e.g. €1.00 / $1.00 / ¥100


def _norm_inv(s):
    return re.sub(r"[^a-z0-9]", "", s.lower()) if s else ""


def match_all(conn):
    # Preserve human-touched matches (match_tier='manual'): a human-confirmed
    # match must survive the full recompute. Only the auto-derived tiers are
    # rebuilt.
    preserved = conn.execute(
        "SELECT ar_obligation_id, ap_obligation_id FROM matches WHERE match_tier = 'manual'"
    ).fetchall()
    conn.execute("DELETE FROM matches WHERE match_tier != 'manual'")

    obls = conn.execute("SELECT * FROM obligations").fetchall()
    ars = [o for o in obls if o["direction"] == "AR"]

    consumed = set()          # AP obligation ids already paired
    match_rows = []           # (ar_id, ap_id, tier, confidence, amount_delta, status)
    in_match = set()
    for p in preserved:       # don't re-pair obligations held by a manual match
        consumed.add(p["ap_obligation_id"])
        in_match.update((p["ar_obligation_id"], p["ap_obligation_id"]))

    for ar in ars:
        if ar["obligation_id"] in in_match:
            continue          # already held by a preserved manual match
        owner, cp = ar["owner_party_id"], ar["counterparty_party_id"]
        if cp is None:
            continue          # can't pair until the counterparty is resolved
        candidates = [
            o for o in obls
            if o["direction"] == "AP"
            and o["owner_party_id"] == cp
            and o["counterparty_party_id"] == owner
            and o["currency"] == ar["currency"]
            and o["obligation_id"] not in consumed
        ]
        chosen = tier = status = conf = delta = None

        # Invoice-number hit (Tier 1 confirm, or Tier 2 amount mismatch).
        inv = _norm_inv(ar["invoice_number"])
        exact = [c for c in candidates if inv and _norm_inv(c["invoice_number"]) == inv]
        if exact:
            c = exact[0]
            delta = abs(ar["amount"] - c["amount"])
            if delta <= TIER1_TOLERANCE_MINOR:
                chosen, tier, status, conf = c, "exact", "confirmed", 1.0
            else:
                chosen, tier, status, conf = c, "fuzzy", "proposed", 0.6   # material → mismatch
        else:
            # Strong partial: amount within tolerance + same due date.
            partial = [
                c for c in candidates
                if abs(ar["amount"] - c["amount"]) <= TIER1_TOLERANCE_MINOR
                and c["due_date"] and c["due_date"] == ar["due_date"]
            ]
            if partial:
                c = partial[0]
                chosen, tier, status, conf = c, "fuzzy", "proposed", 0.7
                delta = abs(ar["amount"] - c["amount"])

        if chosen:
            consumed.add(chosen["obligation_id"])
            in_match.add(ar["obligation_id"])
            in_match.add(chosen["obligation_id"])
            match_rows.append((ar["obligation_id"], chosen["obligation_id"],
                               tier, conf, delta, status))

    conn.executemany(
        "INSERT INTO matches "
        "(ar_obligation_id, ap_obligation_id, match_tier, match_confidence, "
        " amount_delta, match_status) VALUES (?,?,?,?,?,?)",
        match_rows,
    )

    # Reflect outcome on each obligation's ingest_state so the badge can tell
    # "ran, no mirror" (unmatched/one-sided) from "awaiting resolution" (pending).
    for o in obls:
        oid = o["obligation_id"]
        if oid in in_match:
            state = "matched"
        elif o["counterparty_party_id"] is None:
            state = "ingested"          # counterparty unresolved → pending
        else:
            state = "unmatched"         # resolved but no mirror → one-sided
        conn.execute("UPDATE obligations SET ingest_state = ? WHERE obligation_id = ?",
                     (state, oid))
    conn.commit()
    return len(match_rows)
