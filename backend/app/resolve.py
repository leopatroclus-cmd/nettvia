"""Entity resolution (spec §6b): counterparty_raw -> a canonical party.

Layered, AI-proposes / human-confirms:
  1. Deterministic — exact tax_id (VAT/registration) match -> auto-confirm.
  2. Fuzzy — normalized-name similarity (rapidfuzz) + a corroborating signal
     (country) above threshold -> propose.
  3. LLM — ambiguous clusters only, claude-sonnet-4-6 proposes likely-same-party.

Confirmed resolutions are stored as aliases by the caller and never re-asked.
This module resolves WHO the counterparty is — it does NOT match invoices.
"""
import json
import os
import re

from rapidfuzz import fuzz

NAME_PROPOSE = 88     # name ratio to propose a fuzzy match (with corroboration)
NAME_LLM_LOW = 70     # ambiguous band [LLM_LOW, PROPOSE): hand to the LLM
LLM_MODEL = "claude-sonnet-4-6"

# Legal-form suffixes stripped before name comparison.
_SUFFIXES = {
    "gmbh", "ltd", "limited", "sarl", "sa", "sas", "ag", "co", "company",
    "inc", "llc", "plc", "pte", "bv", "oy", "ou", "as", "asa", "srl", "spa",
    "kg", "nv", "ab", "pty", "corp", "kft", "gbr",
}


def normalize_name(name):
    if not name:
        return ""
    s = re.sub(r"[^a-z0-9\s]", " ", name.lower())
    return " ".join(t for t in s.split() if t and t not in _SUFFIXES)


def normalize_taxid(t):
    return re.sub(r"[^a-z0-9]", "", t.lower()) if t else ""


def _parties(conn):
    return conn.execute(
        "SELECT party_id, legal_name, tax_id, country FROM parties"
    ).fetchall()


def confirmed_alias(conn, raw_name):
    """Party a known/confirmed alias points to, or None. Never re-ask these."""
    row = conn.execute(
        "SELECT resolved_party_id FROM counterparty_aliases "
        "WHERE lower(raw_name) = lower(?) AND match_status = 'confirmed' "
        "AND resolved_party_id IS NOT NULL",
        (raw_name,),
    ).fetchone()
    return row["resolved_party_id"] if row else None


def resolve(conn, raw_name, signals):
    """Return {status, party_id, tier, confidence}.

    status: 'confirmed' (set party_id now) | 'proposed' (candidate party_id,
    needs human OK) | 'unresolved' (no confident candidate).
    """
    pid = confirmed_alias(conn, raw_name)
    if pid:
        return {"status": "confirmed", "party_id": pid, "tier": "alias", "confidence": 1.0}

    parties = _parties(conn)

    # 1. Deterministic: exact tax_id.
    tax = normalize_taxid(signals.get("tax_id"))
    if tax:
        for p in parties:
            if p["tax_id"] and normalize_taxid(p["tax_id"]) == tax:
                return {"status": "confirmed", "party_id": p["party_id"],
                        "tier": "deterministic", "confidence": 1.0}

    # 2./3. Name similarity → fuzzy (with corroboration) or LLM (ambiguous).
    nn = normalize_name(raw_name)
    if not nn:
        return {"status": "unresolved", "party_id": None, "tier": "none", "confidence": 0.0}

    scored = sorted(
        ((p, fuzz.token_sort_ratio(nn, normalize_name(p["legal_name"])))
         for p in parties if p["legal_name"]),
        key=lambda x: x[1], reverse=True,
    )
    if not scored:
        return {"status": "unresolved", "party_id": None, "tier": "none", "confidence": 0.0}

    best_p, best_score = scored[0]
    country = (signals.get("country") or "").strip().upper()

    if best_score >= NAME_PROPOSE:
        corroborated = bool(country) and best_p["country"] and country == best_p["country"].upper()
        if corroborated:
            return {"status": "proposed", "party_id": best_p["party_id"],
                    "tier": "fuzzy", "confidence": round(best_score / 100, 2)}

    # Ambiguous: strong-ish name but no corroborating signal → ask the LLM.
    if best_score >= NAME_LLM_LOW:
        candidates = [p for p, s in scored if s >= NAME_LLM_LOW][:4]
        chosen = _llm_resolve(raw_name, signals, candidates)
        if chosen:
            return {"status": "proposed", "party_id": chosen,
                    "tier": "llm", "confidence": round(best_score / 100, 2)}

    return {"status": "unresolved", "party_id": None, "tier": "none", "confidence": 0.0}


def _llm_resolve(raw_name, signals, candidates):
    """Ask claude-sonnet-4-6 whether raw_name is one of the candidate parties.
    Returns a party_id or None. No-op when no API key is configured."""
    if not os.environ.get("ANTHROPIC_API_KEY") or not candidates:
        return None
    try:
        import anthropic

        options = [{"party_id": p["party_id"], "legal_name": p["legal_name"],
                    "country": p["country"]} for p in candidates]
        ids = [str(p["party_id"]) for p in candidates]
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
