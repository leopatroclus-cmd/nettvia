"""Entity resolution (spec §6b): counterparty_raw -> a canonical party.

Layered, AI-proposes / human-confirms. The candidate set is ALL existing
parties (their legal_name/tax_id/country) UNION every confirmed alias — so a
raw name resolves to an existing party even when no alias exists yet.

  1. Deterministic — exact tax_id (VAT/registration) match -> auto-confirm.
  2. Deterministic name — strict normalized name (legal-form suffix KEPT) equal
     to exactly one existing party -> auto-confirm.
  3. Fuzzy — suffix-stripped name similarity (rapidfuzz) above threshold ->
     PROPOSE that specific existing party (corroborating country only raises
     confidence; it is no longer required to surface a candidate).
  4. LLM — genuinely ambiguous band only, claude-sonnet-4-6 proposes the same
     party. Logs when it falls back (no API key / declined).

Confirmed resolutions are stored as aliases by the caller and never re-asked.
This module resolves WHO the counterparty is — it does NOT match invoices.
"""
import json
import os
import re

from rapidfuzz import fuzz

NAME_PROPOSE = 88     # suffix-stripped name ratio to propose a fuzzy match
NAME_LLM_LOW = 70     # ambiguous band [LLM_LOW, PROPOSE): hand to the LLM
LLM_MODEL = "claude-sonnet-4-6"

# Legal-form suffixes stripped before the *fuzzy* name comparison. The strict
# (auto-confirm) comparison keeps them, so "Pacific Forwarders" stays a proposal
# while an exact "Pacific Forwarders Ltd" auto-confirms.
_SUFFIXES = {
    "gmbh", "ltd", "limited", "sarl", "sa", "sas", "ag", "co", "company",
    "inc", "incorporated", "llc", "llp", "plc", "pte", "bv", "oy", "oyj", "ou",
    "as", "asa", "srl", "spa", "kg", "nv", "ab", "pty", "corp", "corporation",
    "kft", "gbr", "sl", "kk", "aps", "sro", "doo", "dd",
}


def _clean(name):
    """Lowercase, drop punctuation, collapse whitespace. Suffixes preserved."""
    if not name:
        return ""
    return " ".join(re.sub(r"[^a-z0-9\s]", " ", name.lower()).split())


def strict_name(name):
    """Strict normalized name (legal suffix KEPT) — basis for auto-confirm."""
    return _clean(name)


def normalize_name(name):
    """Suffix-stripped normalized name — basis for fuzzy comparison."""
    return " ".join(t for t in _clean(name).split() if t not in _SUFFIXES)


def normalize_taxid(t):
    return re.sub(r"[^a-z0-9]", "", t.lower()) if t else ""


def _candidates(conn):
    """All existing parties UNION confirmed aliases, as resolution candidates.

    Each entry is {party_id, name, tax_id, country}. Confirmed aliases let a new
    variant fuzzy-match a name a human has already linked to a party, even if it
    differs from the party's legal_name.
    """
    out = []
    for p in conn.execute(
        "SELECT party_id, legal_name, tax_id, country FROM parties"
    ):
        if p["legal_name"]:
            out.append({"party_id": p["party_id"], "name": p["legal_name"],
                        "tax_id": p["tax_id"], "country": p["country"]})
    for a in conn.execute(
        "SELECT a.raw_name, a.resolved_party_id, p.tax_id, p.country "
        "FROM counterparty_aliases a JOIN parties p ON p.party_id = a.resolved_party_id "
        "WHERE a.match_status = 'confirmed' AND a.resolved_party_id IS NOT NULL"
    ):
        out.append({"party_id": a["resolved_party_id"], "name": a["raw_name"],
                    "tax_id": a["tax_id"], "country": a["country"]})
    return out


def confirmed_alias(conn, raw_name):
    """Party a known/confirmed alias points to, or None. Never re-ask these."""
    row = conn.execute(
        "SELECT resolved_party_id FROM counterparty_aliases "
        "WHERE lower(raw_name) = lower(?) AND match_status = 'confirmed' "
        "AND resolved_party_id IS NOT NULL",
        (raw_name,),
    ).fetchone()
    return row["resolved_party_id"] if row else None


def resolve(conn, raw_name, signals, batch_decisions=None):
    """Return {status, party_id, tier, confidence}.

    status: 'confirmed' (set party_id now) | 'proposed' (candidate party_id,
    needs human OK) | 'unresolved' (no confident candidate).

    batch_decisions: optional {normalized_name: party_id} of parties already
    resolved/proposed earlier in the SAME upload, so two raw names that
    normalize to one entity resolve consistently (in-batch dedupe).
    """
    pid = confirmed_alias(conn, raw_name)
    if pid:
        return {"status": "confirmed", "party_id": pid, "tier": "alias", "confidence": 1.0}

    candidates = _candidates(conn)

    # 1. Deterministic: exact tax_id (VAT / registration number).
    tax = normalize_taxid(signals.get("tax_id"))
    if tax:
        for c in candidates:
            if c["tax_id"] and normalize_taxid(c["tax_id"]) == tax:
                return {"status": "confirmed", "party_id": c["party_id"],
                        "tier": "deterministic", "confidence": 1.0}

    # 2. Deterministic name: strict normalized name equals exactly one party.
    sn = strict_name(raw_name)
    if sn:
        strict_hits = {c["party_id"] for c in candidates if strict_name(c["name"]) == sn}
        if len(strict_hits) == 1:
            return {"status": "confirmed", "party_id": next(iter(strict_hits)),
                    "tier": "deterministic_name", "confidence": 1.0}

    # 3./4. Suffix-stripped similarity → fuzzy proposal or LLM (ambiguous).
    nn = normalize_name(raw_name)
    if not nn:
        return _unresolved()

    scored = sorted(
        ((c, fuzz.token_sort_ratio(nn, normalize_name(c["name"])))
         for c in candidates if c["name"]),
        key=lambda x: x[1], reverse=True,
    )
    if not scored:
        return _unresolved()

    best_c, best_score = scored[0]
    country = (signals.get("country") or "").strip().upper()

    if best_score >= NAME_PROPOSE:
        corroborated = bool(country) and best_c["country"] and country == best_c["country"].upper()
        # Propose the specific existing party regardless of corroboration —
        # corroboration only raises the displayed confidence.
        conf = min(0.99, round(best_score / 100 + (0.05 if corroborated else 0), 2))
        return {"status": "proposed", "party_id": best_c["party_id"],
                "tier": "fuzzy", "confidence": conf}

    # Ambiguous: strong-ish name but below the propose bar → ask the LLM.
    if best_score >= NAME_LLM_LOW:
        cands = [c for c, s in scored if s >= NAME_LLM_LOW][:4]
        chosen = _llm_resolve(raw_name, signals, cands)
        if chosen:
            return {"status": "proposed", "party_id": chosen,
                    "tier": "llm", "confidence": round(best_score / 100, 2)}

    # In-batch dedupe: a sibling row this upload already pointed this normalized
    # name at a party — keep the two consistent rather than splitting the entity.
    if batch_decisions and nn in batch_decisions:
        return {"status": "proposed", "party_id": batch_decisions[nn],
                "tier": "in_batch", "confidence": 0.75}

    return _unresolved()


def _unresolved():
    return {"status": "unresolved", "party_id": None, "tier": "none", "confidence": 0.0}


def _llm_resolve(raw_name, signals, candidates):
    """Ask claude-sonnet-4-6 whether raw_name is one of the candidate parties.
    Returns a party_id or None. Logs (and no-ops) when no API key is configured
    so an ambiguous name silently going unresolved is visible in the logs."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(f"entity-resolution: no ANTHROPIC_API_KEY — LLM disambiguation "
              f"skipped for {raw_name!r}; leaving unresolved.")
        return None
    if not candidates:
        return None
    try:
        import anthropic

        options = [{"party_id": c["party_id"], "legal_name": c["name"],
                    "country": c["country"]} for c in candidates]
        ids = [str(c["party_id"]) for c in candidates]
        schema = {
            "type": "object",
            "properties": {"party_id": {"type": "string", "enum": ids + ["none"]}},
            "required": ["party_id"],
            "additionalProperties": False,
        }
        prompt = (
            "You resolve a counterparty name on an invoice ledger to a known "
            "legal entity. Decide if the raw counterparty is the SAME entity as "
            "one of the candidates (ignore legal-form suffixes and abbreviations; "
            "use the signals). If none is clearly the same, answer 'none'.\n\n"
            f"Raw counterparty: {json.dumps(raw_name)}\n"
            f"Signals: {json.dumps(signals)}\n"
            f"Candidates: {json.dumps(options, indent=2)}\n"
        )
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=LLM_MODEL,
            max_tokens=256,
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": prompt}],
        )
        text = next(b.text for b in resp.content if b.type == "text")
        choice = json.loads(text).get("party_id")
        return int(choice) if choice and choice != "none" else None
    except Exception as e:  # pragma: no cover - network/SDK variance
        print("entity-resolution: LLM path failed:", e)
        return None
