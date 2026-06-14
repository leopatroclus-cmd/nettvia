"""Netting MVP API — Phase 0 (the frontend↔backend seam).

Serves the unchanged prototype as static files and exposes GET /obligations,
projecting canonical obligation rows into the exact field shape the
prototype's render() already consumes: {cp, on, dir, amt, c, iss, due,
match, sugg, note}. The contract is matched to the prototype, not the reverse.
"""
import json
import os
from collections import Counter
from datetime import date, datetime
from typing import Optional

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import (audit, canonical, cycles, ingest, mapping, matching, netting,
               refdata, resolve)
from .db import get_conn, init_db

_SYM = {"EUR": "€", "USD": "$", "CHF": "CHF ", "GBP": "£"}

# Default signed-in party (Aegean Air Cargo S.A.) when nothing is selected.
CURRENT_PARTY_ID = 1

# DEMO ONLY — the "acting as" party for the live demo, a single process-wide
# selection set from the UI party switcher (see /demo/session). Fine for a
# single-presenter demo; it is NOT auth and gates nothing. When real auth lands,
# replace current_party_id() below with the authenticated principal and drop
# this dict and the /demo/session routes.
_demo_session = {"current_party_id": CURRENT_PARTY_ID}


def current_party_id(party_id: Optional[int] = None) -> int:
    """The party a request acts as. An explicit ?party_id= overrides; otherwise
    the demo 'acting as' selection is used. The real auth phase replaces the
    body with the principal resolved from the auth token."""
    return party_id if party_id is not None else _demo_session["current_party_id"]

FRONTEND_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "frontend"))

app = FastAPI(title="Netting MVP API", version="0.1.0")

# Single-origin: FastAPI serves index.html and the API from the same origin, so
# no CORS is needed (and no wildcard origin in production).


@app.on_event("startup")
def _startup():
    init_db()   # seeds only when empty; never resets an existing DB


def _fmt_date(iso):
    """ISO yyyy-mm-dd -> '02 Jun', matching the prototype's date strings."""
    if not iso:
        return ""
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%d %b")


def _amount_units(conn, minor, currency):
    """Stored minor units -> display value, using the currency's exponent (not /100)."""
    return refdata.to_major(minor, refdata.exponent(conn, currency))


def _mismatch_note(conn, row):
    """For a mismatch without a stored note, surface the counterparty's amount
    so the amount_delta is visible on the row."""
    m = cycles.match_for(conn, row["obligation_id"])
    if not m or not m["amount_delta"]:
        return None
    other_id = m["ap_obligation_id"] if m["ar_obligation_id"] == row["obligation_id"] else m["ar_obligation_id"]
    other = conn.execute(
        "SELECT amount, currency FROM obligations WHERE obligation_id = ?", (other_id,)
    ).fetchone()
    if not other:
        return None
    sym = _SYM.get(other["currency"], other["currency"] + " ")
    return f"their books: {sym}{_amount_units(conn, other['amount'], other['currency']):,}"


def _project(row, conn):
    """Canonical obligation -> the prototype's row contract."""
    cp = conn.execute(
        "SELECT legal_name, on_network FROM parties WHERE party_id = ?",
        (row["counterparty_party_id"],),
    ).fetchone()
    horizon = cycles.horizon_for(conn, row)
    match = cycles.match_state(conn, row)
    disp = cycles.effective_code(row, match, horizon)
    cp_net, nettable = cycles.pair_state(conn, row, match, disp, horizon)
    return {
        "id": row["obligation_id"],
        "cp": cp["legal_name"] if cp else row["counterparty_raw"],
        "on": bool(cp["on_network"]) if cp else False,
        "dir": row["direction"],
        "amt": _amount_units(conn, row["amount"], row["currency"]),
        "c": row["currency"],
        "iss": _fmt_date(row["issue_date"]),
        "due": _fmt_date(row["due_date"]),
        "match": match,
        "sugg": cycles.suggested(row, match, horizon),
        "disp": disp,
        "cpNet": cp_net,
        "nettable": nettable,
        "note": row["match_note"] or (_mismatch_note(conn, row) if match == "mismatch" else None),
    }


def _open_cycle_obligations(conn, party_id, network_id):
    """The current party's obligations in the ACTIVE (earliest non-closed) cycle
    — the one being worked through its lifecycle (open → reconciling → locked).
    Obligations stay here while the cycle is processed and leave only when it
    CLOSES at Run netting: netted/settle-direct rows settle into the now-closed
    cycle, while rolled rows have already moved into the next (now-active) cycle.
    Using the working cycle (not the freshly-opened next one) keeps the view from
    emptying prematurely at Close uploads."""
    ac = _active_cycle(conn, network_id)
    if ac is None:
        return []
    return conn.execute(
        "SELECT * FROM obligations WHERE owner_party_id = ? AND assigned_cycle_id = ? "
        "ORDER BY obligation_id", (party_id, ac["cycle_id"])).fetchall()


@app.get("/obligations")
def get_obligations(party_id: int = Depends(current_party_id), network_id: int = 1):
    """The signed-in party's obligations in the OPEN cycle, in the prototype's
    shape. Scoped to the open cycle so netted invoices drop out after advance and
    rolled-forward ones appear here."""
    conn = get_conn()
    try:
        return [_project(r, conn) for r in _open_cycle_obligations(conn, party_id, network_id)]
    finally:
        conn.close()


@app.get("/accounts/metrics")
def accounts_metrics(party_id: int = Depends(current_party_id), network_id: int = 1):
    """Accounts metric cards, COMPUTED from the same open-cycle set (never static):
    gross AR / AP per currency, pending (awaiting disposition), needs-attention
    (mismatches + disputes). An empty cycle yields all zeros."""
    conn = get_conn()
    try:
        rows = _open_cycle_obligations(conn, party_id, network_id)
        ar, ap, pending, attention = {}, {}, 0, 0
        for r in rows:
            bucket = ar if r["direction"] == "AR" else ap
            bucket[r["currency"]] = bucket.get(r["currency"], 0) + r["amount"]
            if r["disposition"] == "pending":
                pending += 1
            if cycles.match_state(conn, r) == "mismatch" or r["disposition"] == "disputed":
                attention += 1
        return {
            "gross_receivable": _money_list(conn, ar),
            "gross_payable": _money_list(conn, ap),
            "pending": pending,
            "needs_attention": attention,
        }
    finally:
        conn.close()


@app.post("/dispositions")
def set_dispositions(payload: dict = Body(...), party_id: int = Depends(current_party_id)):
    """Persist a disposition (accept & net / settle direct / defer / dispute) for
    one or more of the signed-in party's obligations. Bulk via `ids`.

    Server-enforced gate (spec §8): accept & net is only allowed on a
    confirmed-match obligation."""
    ids = payload.get("ids")
    if ids is None and payload.get("id") is not None:
        ids = [payload["id"]]
    code = payload.get("disp")
    if not ids or code not in cycles.CODE_TO_CANONICAL:
        raise HTTPException(400, "Provide ids and a disp of net|direct|defer|dispute.")
    disposition, mode = cycles.CODE_TO_CANONICAL[code]

    conn = get_conn()
    try:
        rows = {}
        for oid in ids:
            row = conn.execute(
                "SELECT * FROM obligations WHERE obligation_id = ? AND owner_party_id = ?",
                (oid, party_id),
            ).fetchone()
            if row is None:
                raise HTTPException(404, f"Obligation {oid} not found for this party.")
            if cycles.is_locked(conn, row["assigned_cycle_id"]):
                raise HTTPException(409, f"Obligation {oid} is in a locked cycle.")
            if code == "net" and cycles.match_state(conn, row) != "matched":
                raise HTTPException(
                    400, f"Accept & net requires a confirmed match (obligation {oid}).")
            rows[oid] = row
        for oid, row in rows.items():
            conn.execute(
                "UPDATE obligations SET disposition = ?, settlement_mode = ? "
                "WHERE obligation_id = ?",
                (disposition, mode, oid),
            )
            audit.append(
                conn, actor=f"party:{party_id}", action="disposition_set",
                entity_ref=f"obligation:{oid}",
                before={"disposition": row["disposition"], "settlement_mode": row["settlement_mode"]},
                after={"disposition": disposition, "settlement_mode": mode},
            )
        conn.commit()
        return {"updated": len(rows)}
    finally:
        conn.close()


def _cached_mapping(conn, party_id, signature):
    row = conn.execute(
        "SELECT column_mapping FROM source_formats "
        "WHERE party_id = ? AND source_label = ?",
        (party_id, signature),
    ).fetchone()
    return json.loads(row["column_mapping"]) if row else None


def _evaluate_batch(conn, rows, mapping_dict):
    """Per-row (obligation, issues). A row with any issue is flagged for the
    user (needs attention) and NOT imported — never silently collapsed/coerced.
    Issues: missing required field, unknown currency, in-batch duplicate."""
    known = {r["code"] for r in conn.execute("SELECT code FROM currencies")}
    dup_idx = set(ingest.inbatch_duplicate_rows(rows, mapping_dict))
    out = []
    for i, raw in enumerate(rows):
        ob, missing = ingest.normalize_row(raw, mapping_dict)
        issues = []
        if missing:
            issues.append("missing_fields")
        if ob["currency"] and ob["currency"] not in known:
            issues.append("unknown_currency")
        if i in dup_idx:
            issues.append("duplicate_in_batch")
        out.append((raw, ob, issues))
    return out


# ---------------------------------------------------------------------------
# Entity resolution (Phase 3): resolve counterparty_raw -> a canonical party.
# ---------------------------------------------------------------------------

def _derive_on_network(conn, party_id):
    """on-network = the party also uploads a ledger, i.e. owns ≥1 obligation."""
    owns = conn.execute(
        "SELECT 1 FROM obligations WHERE owner_party_id = ? LIMIT 1", (party_id,)
    ).fetchone()
    conn.execute("UPDATE parties SET on_network = ? WHERE party_id = ?",
                 (1 if owns else 0, party_id))


def _upsert_alias(conn, raw_name, resolved_party_id, status, confidence, signals):
    existing = conn.execute(
        "SELECT alias_id FROM counterparty_aliases WHERE lower(raw_name) = lower(?)",
        (raw_name,),
    ).fetchone()
    sig = json.dumps(signals or {})
    if existing:
        conn.execute(
            "UPDATE counterparty_aliases SET resolved_party_id=?, signals=?, "
            "match_confidence=?, match_status=? WHERE alias_id=?",
            (resolved_party_id, sig, confidence, status, existing["alias_id"]),
        )
    else:
        conn.execute(
            "INSERT INTO counterparty_aliases "
            "(raw_name, resolved_party_id, signals, match_confidence, match_status) "
            "VALUES (?,?,?,?,?)",
            (raw_name, resolved_party_id, sig, confidence, status),
        )


def _apply_resolution(conn, raw_name, party_id, owner_party_id, confidence, signals):
    """Confirm raw_name -> party_id: resolve all of that name's obligations,
    store the confirmed alias, and re-derive the party's on_network flag."""
    conn.execute(
        "UPDATE obligations SET counterparty_party_id = ? "
        "WHERE lower(counterparty_raw) = lower(?) AND owner_party_id = ? "
        "AND counterparty_party_id IS NULL",
        (party_id, raw_name, owner_party_id),
    )
    _upsert_alias(conn, raw_name, party_id, "confirmed", confidence, signals)
    _derive_on_network(conn, party_id)


def _signals_by_name(rows, mapping_dict):
    """Merge each counterparty's signals across the batch's rows."""
    out = {}
    for raw in rows:
        ob, _ = ingest.normalize_row(raw, mapping_dict)
        name = ob["counterparty_raw"]
        if not name:
            continue
        sig = ingest.extract_signals(raw, mapping_dict)
        cur = out.setdefault(name, {"tax_id": None, "country": None, "iban": None})
        for k, v in sig.items():
            if v and not cur[k]:
                cur[k] = v
    return out


def _pending_list(conn, party_id):
    """Counterparties still needing a human decision, for the Upload panel."""
    aliases = conn.execute(
        "SELECT * FROM counterparty_aliases WHERE match_status IN ('proposed','unresolved') "
        "ORDER BY alias_id"
    ).fetchall()
    out = []
    for a in aliases:
        cnt = conn.execute(
            "SELECT COUNT(*) AS n FROM obligations "
            "WHERE lower(counterparty_raw) = lower(?) AND owner_party_id = ? "
            "AND counterparty_party_id IS NULL",
            (a["raw_name"], party_id),
        ).fetchone()["n"]
        if not cnt:
            continue
        candidate = None
        if a["resolved_party_id"]:
            p = conn.execute(
                "SELECT party_id, legal_name, country FROM parties WHERE party_id = ?",
                (a["resolved_party_id"],),
            ).fetchone()
            if p:
                sig = json.loads(a["signals"] or "{}")
                reason = "same country" if sig.get("country") else (
                    f"{int((a['match_confidence'] or 0) * 100)}% name match")
                candidate = {"party_id": p["party_id"], "legal_name": p["legal_name"],
                             "reason": reason}
        out.append({
            "raw_name": a["raw_name"],
            "status": a["match_status"],
            "obligation_count": cnt,
            "candidate": candidate,
        })
    return out


def _resolve_batch(conn, party_id, rows, mapping_dict):
    """Run layered resolution over the batch's distinct counterparties."""
    signals = _signals_by_name(rows, mapping_dict)
    names = [r["counterparty_raw"] for r in conn.execute(
        "SELECT DISTINCT counterparty_raw FROM obligations "
        "WHERE owner_party_id = ? AND counterparty_party_id IS NULL "
        "AND counterparty_raw IS NOT NULL", (party_id,))]
    resolved = 0
    batch_decisions = {}   # normalized name -> party already resolved this batch
    for name in names:
        sig = signals.get(name, {})
        res = resolve.resolve(conn, name, sig, batch_decisions)
        if res["party_id"]:
            batch_decisions[resolve.normalize_name(name)] = res["party_id"]
        if res["status"] == "confirmed":
            _apply_resolution(conn, name, res["party_id"], party_id, res["confidence"], sig)
            resolved += 1
        else:
            cand = res["party_id"] if res["status"] == "proposed" else None
            _upsert_alias(conn, name, cand, res["status"], res["confidence"], sig)
    return resolved


@app.post("/upload")
async def upload(file: UploadFile = File(...), party_id: Optional[int] = Form(None)):
    """Parse a CSV/XLSX, propose (or recall) a schema mapping, stage the batch.

    The batch (and the obligations it later creates) is OWNED BY the current
    party — an explicit party_id form field overrides, else the demo selection.
    Returns the proposed mapping + summary counts for the Upload screen. No
    obligations are created until /upload/{batch_id}/confirm.
    """
    party_id = current_party_id(party_id)
    content = await file.read()
    try:
        headers, rows = ingest.parse_upload(file.filename, content)
    except Exception as e:
        raise HTTPException(400, f"Could not parse file: {e}")
    if not rows:
        raise HTTPException(400, "File has no data rows.")

    signature = ingest.header_signature(headers)
    conn = get_conn()
    try:
        cached = _cached_mapping(conn, party_id, signature)
        if cached is not None:
            mapping_dict, method = cached, "cached"
        else:
            mapping_dict, method = mapping.propose_mapping(headers, rows[:8])

        evaluated = _evaluate_batch(conn, rows, mapping_dict)
        breakdown = Counter(reason for _, _, iss in evaluated for reason in iss)
        need_attention = sum(1 for _, _, iss in evaluated if iss)
        cur = conn.execute(
            "INSERT INTO upload_batches "
            "(party_id,network_id,source_label,header_signature,status,row_count,"
            " raw_rows,proposed_mapping,created_at) "
            "VALUES (?,1,?,?,'parsed',?,?,?,?)",
            (party_id, file.filename, signature, len(rows),
             json.dumps(rows), json.dumps(mapping_dict),
             datetime.utcnow().isoformat()),
        )
        conn.commit()
        return {
            "batch_id": cur.lastrowid,
            "source_label": file.filename,
            "headers": headers,
            "sample_rows": rows[:8],
            "mapping": [{"src": c, "dst": f} for c, f in mapping_dict.items()],
            "cached": method == "cached",
            "mapping_method": method,
            "rows_read": len(rows),
            "auto_processed": len(rows) - need_attention,
            "need_attention": need_attention,
            "attention_breakdown": dict(breakdown),
        }
    finally:
        conn.close()


@app.post("/upload/{batch_id}/confirm")
def confirm_upload(batch_id: int, payload: dict = Body(default=None)):
    """Cache the (confirmed) mapping and normalize the staged rows into
    canonical obligations tagged to the uploading party + NAP."""
    conn = get_conn()
    try:
        batch = conn.execute(
            "SELECT * FROM upload_batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if batch is None:
            raise HTTPException(404, "Unknown upload batch.")
        if batch["status"] == "imported":
            raise HTTPException(409, "This batch was already imported.")

        mapping_dict = (payload or {}).get("mapping") or json.loads(batch["proposed_mapping"])
        party_id = batch["party_id"]
        signature = batch["header_signature"]

        # Cache the format mapping so the same headers auto-apply next time.
        if _cached_mapping(conn, party_id, signature) is None:
            conn.execute(
                "INSERT INTO source_formats (party_id, source_label, column_mapping) "
                "VALUES (?,?,?)",
                (party_id, signature, json.dumps(mapping_dict)),
            )

        # New obligations enter the network's currently OPEN cycle. After
        # close-uploads this is the next cycle, so late uploads route there.
        open_cycle = cycles.ensure_open_cycle(conn, 1)["cycle_id"]

        rows = json.loads(batch["raw_rows"])
        evaluated = _evaluate_batch(conn, rows, mapping_dict)
        imported = 0
        skipped = 0
        skipped_breakdown = Counter()
        for raw, obligation, issues in evaluated:
            if issues:                       # flagged → needs attention, not imported
                skipped += 1
                skipped_breakdown.update(issues)
                continue
            # Major -> minor units via the currency's exponent (correct for JPY etc.).
            exp = refdata.exponent(conn, obligation["currency"])
            amount_minor = refdata.to_minor(obligation["amount"], exp)
            vat_minor = refdata.to_minor(obligation["vat_amount"], exp)
            # UPSERT on the natural key (owner + invoice_number + direction +
            # counterparty): re-uploading updates the row instead of duplicating it.
            # An existing counterparty_party_id (a prior resolution) and disposition
            # are preserved. In-batch dups are flagged above, never collapsed here.
            conn.execute(
                "INSERT INTO obligations "
                "(owner_party_id,direction,counterparty_raw,counterparty_party_id,"
                " network_id,invoice_number,amount,currency,issue_date,due_date,"
                " status_source,po_reference,upload_batch_id,assigned_cycle_id,ingest_state,raw_row,"
                " vat_treatment,vat_rate,vat_amount_minor) "
                "VALUES (?,?,?,NULL,1,?,?,?,?,?,?,?,?,?,'ingested',?,?,?,?) "
                "ON CONFLICT(owner_party_id, invoice_number, direction, counterparty_raw) DO UPDATE SET "
                " amount=excluded.amount, "
                " currency=excluded.currency, issue_date=excluded.issue_date, "
                " due_date=excluded.due_date, status_source=excluded.status_source, "
                " po_reference=excluded.po_reference, upload_batch_id=excluded.upload_batch_id, "
                " raw_row=excluded.raw_row, ingest_state='ingested', "
                " vat_treatment=excluded.vat_treatment, vat_rate=excluded.vat_rate, "
                " vat_amount_minor=excluded.vat_amount_minor",
                (party_id, obligation["direction"], obligation["counterparty_raw"],
                 obligation["invoice_number"], amount_minor, obligation["currency"],
                 obligation["issue_date"], obligation["due_date"],
                 obligation["status_source"], obligation["po_reference"],
                 batch_id, open_cycle, json.dumps(raw),
                 obligation["vat_treatment"], obligation["vat_rate"], vat_minor),
            )
            imported += 1

        # Entity resolution: deterministic/known aliases auto-resolve; the rest
        # surface in the Upload panel for confirmation.
        resolved = _resolve_batch(conn, party_id, rows, mapping_dict)
        pending = _pending_list(conn, party_id)
        # Transaction matching over the (now resolved) ledger, then mint canonical
        # invoices from confirmed matches. Uploaded obligations stay PENDING and
        # follow default_on_no_action at lock (spec §9).
        matching.match_all(conn)
        canonical.mint(conn)

        conn.execute(
            "UPDATE upload_batches SET status='imported' WHERE batch_id = ?", (batch_id,)
        )
        conn.commit()
        return {"imported": imported, "skipped": skipped, "rows_read": len(rows),
                "party_id": party_id, "resolved": resolved, "pending": pending,
                "skipped_breakdown": dict(skipped_breakdown)}
    finally:
        conn.close()


@app.get("/resolutions")
def list_resolutions(party_id: int = Depends(current_party_id)):
    """Counterparties still awaiting a resolution decision."""
    conn = get_conn()
    try:
        return _pending_list(conn, party_id)
    finally:
        conn.close()


@app.post("/resolutions/confirm")
def confirm_resolution(payload: dict = Body(...), party_id: int = Depends(current_party_id)):
    """Confirm a counterparty: resolve to an existing party_id, or create a new
    (off-network) party when it's genuinely new. Writes the alias and resolves
    all of that counterparty's obligations."""
    raw_name = (payload or {}).get("raw_name")
    if not raw_name:
        raise HTTPException(400, "raw_name is required.")
    conn = get_conn()
    try:
        alias = conn.execute(
            "SELECT signals FROM counterparty_aliases WHERE lower(raw_name) = lower(?)",
            (raw_name,),
        ).fetchone()
        signals = json.loads(alias["signals"]) if alias and alias["signals"] else {}

        target = payload.get("party_id")
        if target is None:  # "It's new" — create an off-network party.
            cur = conn.execute(
                "INSERT INTO parties (legal_name, tax_id, country, on_network) "
                "VALUES (?,?,?,0)",
                (payload.get("legal_name") or raw_name,
                 signals.get("tax_id"), signals.get("country")),
            )
            target = cur.lastrowid

        _apply_resolution(conn, raw_name, target, party_id, 1.0, signals)
        audit.append(
            conn, actor=f"party:{party_id}", action="entity_resolved",
            entity_ref=f"counterparty:{raw_name}",
            after={"resolved_to": target})
        matching.match_all(conn)   # a new resolution may enable a match
        canonical.mint(conn)       # which may mint/remove a canonical invoice
        on_net = conn.execute(
            "SELECT on_network FROM parties WHERE party_id = ?", (target,)
        ).fetchone()["on_network"]
        conn.commit()
        return {"resolved_to": target, "on_network": bool(on_net),
                "pending": _pending_list(conn, party_id)}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Partners (Phase 8.7): tax_id is the primary identifier; partner info editable.
# ---------------------------------------------------------------------------

_PARTY_EDITABLE = ("legal_name", "tax_id", "country", "group_id")


def _party_basis(conn, owner_id, partner_id, on_network):
    """Off-network partners are reconciled from our books only ('Claimed'); for
    on-network ones, a disputed disposition shows 'Disputed', otherwise 'Agreed'."""
    if not on_network:
        return "Claimed"
    disputed = conn.execute(
        "SELECT 1 FROM obligations WHERE owner_party_id = ? AND counterparty_party_id = ? "
        "AND disposition = 'disputed' LIMIT 1", (owner_id, partner_id)).fetchone()
    return "Disputed" if disputed else "Agreed"


def _party_view(conn, owner_id, p):
    """One partner row for the Partners view, with per-currency balance from the
    signed-in party's ledger (AR positive, AP negative). on_network is derived."""
    obs = conn.execute(
        "SELECT direction, amount, currency FROM obligations "
        "WHERE owner_party_id = ? AND counterparty_party_id = ?",
        (owner_id, p["party_id"])).fetchall()
    bal = {}
    for o in obs:
        sign = 1 if o["direction"] == "AR" else -1
        bal[o["currency"]] = bal.get(o["currency"], 0) + sign * o["amount"]
    balance = [{"currency": c, "net_major": refdata.to_major(v, refdata.exponent(conn, c))}
               for c, v in bal.items()]
    return {
        "party_id": p["party_id"], "legal_name": p["legal_name"],
        "tax_id": p["tax_id"], "country": p["country"], "city": p["city"],
        "group_id": p["group_id"], "on_network": bool(p["on_network"]),
        "invoices": len(obs), "balance": balance,
        "basis": _party_basis(conn, owner_id, p["party_id"], p["on_network"]),
    }


@app.get("/parties")
def list_parties(party_id: int = Depends(current_party_id)):
    """The signed-in party's trading partners, identity-first (tax_id primary).
    Excludes the signed-in party itself and the network operator (admin)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM parties WHERE party_id != ? AND party_id NOT IN "
            "(SELECT party_id FROM party_networks WHERE role = 'admin') "
            "ORDER BY legal_name", (party_id,)).fetchall()
        return [_party_view(conn, party_id, p) for p in rows]
    finally:
        conn.close()


@app.patch("/parties/{party_id}")
def edit_party(party_id: int, payload: dict = Body(...),
               actor_party_id: int = Depends(current_party_id)):
    """Edit a partner's identity: legal_name, tax_id, country, group_id.

    on_network is DERIVED from ledger ownership — not editable here. tax_id is
    the primary identifier, so a value already owned by another party is blocked
    (application-layer uniqueness; intentionally no DB UNIQUE constraint). Every
    edit is recorded in the hash-chained audit log (identity is trust-critical).
    """
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM parties WHERE party_id = ?", (party_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Unknown party.")

        updates = {}
        for f in _PARTY_EDITABLE:
            if f not in payload:
                continue
            v = payload[f]
            if isinstance(v, str):
                v = v.strip()
            if f == "legal_name":
                if not v:
                    raise HTTPException(400, "legal_name cannot be empty.")
            elif f == "tax_id":
                v = v or None                       # blank clears the identifier
            elif f == "country":
                v = (v or None)
                if v:
                    v = v.upper()
            elif f == "group_id":
                v = int(v) if v not in (None, "") else None
            updates[f] = v
        if not updates:
            raise HTTPException(400, "No editable fields provided.")

        # tax_id uniqueness (primary identifier) — enforced in app code, not the
        # DB. Compare normalized so "DE 811907980" and "DE811907980" collide.
        if updates.get("tax_id"):
            norm = resolve.normalize_taxid(updates["tax_id"])
            for other in conn.execute(
                "SELECT party_id, legal_name, tax_id FROM parties WHERE party_id != ?",
                (party_id,)):
                if other["tax_id"] and resolve.normalize_taxid(other["tax_id"]) == norm:
                    raise HTTPException(
                        409, f"tax_id is already used by {other['legal_name']} "
                             f"(party {other['party_id']}). Each party's tax_id must be unique.")

        before = {f: row[f] for f in _PARTY_EDITABLE}
        sets = ", ".join(f"{f} = ?" for f in updates)
        conn.execute(f"UPDATE parties SET {sets} WHERE party_id = ?",
                     (*updates.values(), party_id))
        # jurisdiction (Delos seam) stays derived from country.
        if "country" in updates:
            conn.execute("UPDATE parties SET jurisdiction = ? WHERE party_id = ?",
                         (refdata.iso3(updates["country"]), party_id))

        after = {**before, **updates}
        audit.append(conn, actor=f"party:{actor_party_id}", action="party.edit",
                     entity_ref=f"party:{party_id}", before=before, after=after)
        conn.commit()
        updated = conn.execute("SELECT * FROM parties WHERE party_id = ?",
                               (party_id,)).fetchone()
        return _party_view(conn, actor_party_id, updated)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Demo party switcher (Phase A) — DEMO ONLY, not auth. Lets the presenter act
# as any party in a live demo. The real auth phase removes these two routes and
# resolves the principal from the auth token instead (see current_party_id()).
# ---------------------------------------------------------------------------

@app.get("/demo/session")
def get_demo_session():
    """Current 'acting as' party + the full party list for the UI switcher."""
    conn = get_conn()
    try:
        parties = conn.execute(
            "SELECT party_id, legal_name, country FROM parties ORDER BY party_id"
        ).fetchall()
        return {
            "current_party_id": _demo_session["current_party_id"],
            "parties": [{"party_id": p["party_id"], "legal_name": p["legal_name"],
                         "country": p["country"]} for p in parties],
        }
    finally:
        conn.close()


@app.post("/demo/session")
def set_demo_session(payload: dict = Body(...)):
    """Switch the 'acting as' party for the session. DEMO ONLY — no auth, gates
    nothing; it only changes which party subsequent requests are attributed to."""
    pid = (payload or {}).get("party_id")
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT party_id, legal_name FROM parties WHERE party_id = ?", (pid,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Unknown party.")
        _demo_session["current_party_id"] = row["party_id"]
        return {"current_party_id": row["party_id"], "legal_name": row["legal_name"]}
    finally:
        conn.close()


_PHASE = {"open": "Open", "reconciling": "Reconciling", "locked": "Locked",
          "netted": "Netted", "closed": "Closed"}
_PROGRESS = {"open": 0.3, "reconciling": 0.62, "locked": 0.85, "netted": 0.95, "closed": 1.0}


def _project_cycle(conn, c):
    frozen = conn.execute(
        "SELECT COUNT(*) AS n FROM cycle_obligations WHERE cycle_id = ?", (c["cycle_id"],)
    ).fetchone()["n"]
    return {
        "cycle_id": c["cycle_id"],
        "sequence_no": c["sequence_no"],
        "label": (c["opens_at"] or "")[:7],   # e.g. "2026-06"
        "state": c["state"],
        "phase": _PHASE.get(c["state"], c["state"]),
        "progress": _PROGRESS.get(c["state"], 0.0),
        "frozen_count": frozen,
        "milestones": [
            {"label": "Opened", "date": _fmt_date(c["opens_at"])},
            {"label": "Upload cut-off", "date": _fmt_date(c["upload_cutoff_at"])},
            {"label": "Processing cut-off", "date": _fmt_date(c["processing_cutoff_at"])},
            {"label": "Settle", "date": _fmt_date(c["settlement_date"])},
        ],
    }


@app.get("/cycles")
def list_cycles(network_id: int = 1):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM cycles WHERE network_id = ? ORDER BY sequence_no", (network_id,)
        ).fetchall()
        return [_project_cycle(conn, c) for c in rows]
    finally:
        conn.close()


@app.get("/cycles/current")
def current_cycle(network_id: int = 1):
    """The cycle the Netting screen tracks: the earliest not-yet-closed cycle
    (so it shows one cycle through Open → Reconciling → Locked)."""
    conn = get_conn()
    try:
        c = conn.execute(
            "SELECT * FROM cycles WHERE network_id = ? AND state != 'closed' "
            "ORDER BY sequence_no LIMIT 1", (network_id,)
        ).fetchone()
        if c is None:
            raise HTTPException(404, "No active cycle.")
        return _project_cycle(conn, c)
    finally:
        conn.close()


@app.post("/cycles/{cycle_id}/close-uploads")
def close_uploads(cycle_id: int):
    """OPEN → RECONCILING; uploads close and route to the next open cycle."""
    conn = get_conn()
    try:
        cycles.close_uploads(conn, cycle_id)
        return _project_cycle(conn, conn.execute(
            "SELECT * FROM cycles WHERE cycle_id = ?", (cycle_id,)).fetchone())
    except ValueError as e:
        raise HTTPException(409, str(e))
    finally:
        conn.close()


@app.post("/cycles/{cycle_id}/lock")
def lock_cycle(cycle_id: int):
    """RECONCILING → LOCKED; freeze the nettable snapshot, open the next cycle."""
    conn = get_conn()
    try:
        _require_audit_ok(conn)   # guard before advancing state
        frozen = cycles.lock(conn, cycle_id)
        canonical.mint(conn)   # rolled obligations may move cycle; keep invoices current
        out = _project_cycle(conn, conn.execute(
            "SELECT * FROM cycles WHERE cycle_id = ?", (cycle_id,)).fetchone())
        out["frozen"] = frozen
        return out
    except ValueError as e:
        raise HTTPException(409, str(e))
    finally:
        conn.close()


@app.get("/audit/verify")
def audit_verify():
    """Recompute the audit hash-chain end-to-end (tamper-evident)."""
    conn = get_conn()
    try:
        return audit.verify(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Netting (Phase 7): regenerable projection over canonical invoices.
# ---------------------------------------------------------------------------

def _require_audit_ok(conn):
    """Runtime guard: refuse to advance state / emit a statement on a broken chain."""
    v = audit.verify(conn)
    if not v["ok"]:
        raise HTTPException(409, f"Audit chain broken at log_id {v['broken_log_id']} — blocked.")


def _days_to(iso):
    if not iso:
        return None
    return max(0, (date.fromisoformat(iso) - date.today()).days)


def _active_cycle(conn, network_id, cycle_id=None):
    if cycle_id:
        return conn.execute("SELECT * FROM cycles WHERE cycle_id = ?", (cycle_id,)).fetchone()
    return conn.execute(
        "SELECT * FROM cycles WHERE network_id = ? AND state != 'closed' "
        "ORDER BY sequence_no LIMIT 1", (network_id,)).fetchone()


def _netting_response(conn, cycle, party_id):
    result = netting.compute(conn, cycle)
    gross, net = netting.compression(result["invoices"], result["positions"])
    net_gross, net_net = sum(gross.values()), sum(net.values())

    party_positions = []
    for s in result["positions"]:
        if s["party_id"] != party_id:
            continue
        exp = refdata.exponent(conn, s["currency"])
        party_positions.append({
            "currency": s["currency"],
            "net_minor": s["net"],
            "net_major": refdata.to_major(s["net"], exp),
            "direction": "receive" if s["net"] > 0 else ("pay" if s["net"] < 0 else "flat"),
            "gross_receivable_major": refdata.to_major(s["receivable"], exp),
            "gross_payable_major": refdata.to_major(s["payable"], exp),
            "reconstruction_key": s["reconstruction_key"],
        })

    counterparties = set()
    for ci in result["invoices"]:
        if ci["biller_id"] == party_id:
            counterparties.add(ci["payer_id"])
        elif ci["payer_id"] == party_id:
            counterparties.add(ci["biller_id"])

    cfg = conn.execute(
        "SELECT cost_per_payment, cycle_length_days FROM networks WHERE network_id = ?",
        (cycle["network_id"],)).fetchone()
    p_gross, p_net = gross.get(party_id, 0), net.get(party_id, 0)
    return {
        "cycle_id": cycle["cycle_id"], "label": (cycle["opens_at"] or "")[:7],
        "state": cycle["state"], "provisional": result["provisional"],
        "settlement_date": cycle["settlement_date"],
        "settlement_date_display": _fmt_date(cycle["settlement_date"]),
        "days_to_settle": _days_to(cycle["settlement_date"]),
        "party": {
            "positions": party_positions,
            "gross": p_gross, "net": p_net, "eliminated": p_gross - p_net,
            "pct": round(100 * (p_gross - p_net) / p_gross) if p_gross else 0,
            "counterparties": len(counterparties),
        },
        "network": {
            "gross": net_gross, "net": net_net, "eliminated": net_gross - net_net,
            "pct": round(100 * (net_gross - net_net) / net_gross) if net_gross else 0,
        },
        "savings": {
            "cost_per_payment": cfg["cost_per_payment"] or 0,
            "cycles_per_year": round(365 / (cfg["cycle_length_days"] or 30)),
        },
        "sum_by_currency": result["sum_by_currency"],
    }


@app.get("/netting")
def get_netting(party_id: int = Depends(current_party_id), network_id: int = 1):
    """Netting result (regenerable) for the active cycle — provisional until lock."""
    conn = get_conn()
    try:
        _require_audit_ok(conn)
        cycle = _active_cycle(conn, network_id)
        if cycle is None:
            raise HTTPException(404, "No active cycle.")
        return _netting_response(conn, cycle, party_id)
    except netting.NettingInvariantError as e:
        raise HTTPException(409, f"Netting invariant failed — blocked: {e}")
    finally:
        conn.close()


def _net_and_persist(conn, cycle):
    """LOCKED → NETTED: compute net positions over the frozen snapshot, persist
    them as a (regenerable) projection, advance the cycle. Caller owns the txn.
    Raises netting.NettingInvariantError if the Σnet=0 / reconstruction guard trips."""
    cycle_id = cycle["cycle_id"]
    result = netting.compute(conn, cycle)         # guards Σnet=0 + reconstruction
    gross, net = netting.compression(result["invoices"], result["positions"])
    cpp = conn.execute(
        "SELECT cost_per_payment FROM networks WHERE network_id = ?",
        (cycle["network_id"],)).fetchone()["cost_per_payment"] or 0
    conn.execute("DELETE FROM net_positions WHERE cycle_id = ?", (cycle_id,))
    for s in result["positions"]:
        g, n = gross.get(s["party_id"], 0), net.get(s["party_id"], 0)
        conn.execute(
            "INSERT INTO net_positions (cycle_id, party_id, currency, gross_payable, "
            "gross_receivable, net_amount, gross_payment_count, net_payment_count, "
            "estimated_savings) VALUES (?,?,?,?,?,?,?,?,?)",
            (cycle_id, s["party_id"], s["currency"], s["payable"], s["receivable"],
             s["net"], g, n, int(round((g - n) * cpp * 100))))
    conn.execute("UPDATE cycles SET state = 'netted' WHERE cycle_id = ?", (cycle_id,))
    audit.append(conn, actor="operator", action="cycle_netted",
                 entity_ref=f"cycle:{cycle_id}",
                 after={"positions": len(result["positions"]),
                        "sum_by_currency": result["sum_by_currency"]})
    return result


@app.post("/cycles/{cycle_id}/net")
def run_netting(cycle_id: int, party_id: int = Depends(current_party_id)):
    """LOCKED → NETTED → CLOSED: compute net positions over the snapshot, persist
    them as the (regenerable) statement, then CLOSE the cycle and open the next.
    Closing is what settles the netted/settle-direct obligations out of the active
    Accounts view (they live on in the now-closed cycle's statement / history)."""
    conn = get_conn()
    try:
        cycle = conn.execute("SELECT * FROM cycles WHERE cycle_id = ?", (cycle_id,)).fetchone()
        if cycle is None:
            raise HTTPException(404, "Unknown cycle.")
        if cycle["state"] != "locked":
            raise HTTPException(409, f"Netting requires a LOCKED cycle (was '{cycle['state']}').")
        _require_audit_ok(conn)            # guard before advancing state
        try:
            _net_and_persist(conn, cycle)  # → netted (uncommitted)
            cycles.close(conn, cycle_id)   # → closed + opens the next cycle (commits)
        except netting.NettingInvariantError as e:
            raise HTTPException(409, f"Netting invariant failed — cycle not netted: {e}")
        return _netting_response(conn,
                                 conn.execute("SELECT * FROM cycles WHERE cycle_id = ?",
                                              (cycle_id,)).fetchone(), party_id)
    finally:
        conn.close()


@app.get("/statement")
def statement(party_id: int = Depends(current_party_id), cycle_id: int = None, network_id: int = 1):
    """Per-party per-cycle statement, including the reconstruction key."""
    conn = get_conn()
    try:
        _require_audit_ok(conn)
        cycle = _active_cycle(conn, network_id, cycle_id)
        if cycle is None:
            raise HTTPException(404, "No cycle.")
        try:
            r = _netting_response(conn, cycle, party_id)
        except netting.NettingInvariantError as e:
            raise HTTPException(409, f"Netting invariant failed — statement blocked: {e}")
        return {
            "party_id": party_id, "cycle_id": r["cycle_id"], "label": r["label"],
            "state": r["state"], "provisional": r["provisional"],
            "settlement_date": r["settlement_date"],
            "invoice_count": r["party"]["gross"],
            "positions": r["party"]["positions"],   # net + gross AR/AP + reconstruction_key per currency
            "compression": {k: r["party"][k] for k in ("gross", "net", "eliminated", "pct")},
            "savings": r["savings"],
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Statement summary (Phase C): the presentation-grade cycle reveal — network
# headline + per-party breakdown, computed at render from net_positions +
# obligations + canonical_invoices. Savings show their assumptions, never exact.
# ---------------------------------------------------------------------------

# Adjustable assumptions, displayed on the statement so the numbers are defensible.
WIRE_FEE_MAJOR_DEFAULT = 30.0    # € per international payment avoided (per-party figure)
FX_RATE_DEFAULT = 0.006          # 0.6% spread on cross-currency netted value (per-party)

# At-scale projection — grounded BOTTOM-UP in member-interview figures, NOT
# extrapolated from the demo cycle. Per member/month: fee saving + FX saving.
FEE_SAVING_MIN_DEFAULT = 100.0   # € / member / month (payment fees)
FEE_SAVING_MAX_DEFAULT = 250.0
FX_SAVING_MIN_DEFAULT = 300.0    # € / member / month (FX spread)
FX_SAVING_MAX_DEFAULT = 600.0
ACTIVE_MEMBERS_DEFAULT = 50      # configurable network member count


def _money_list(conn, by_ccy):
    out = []
    for c in sorted(by_ccy):
        exp = refdata.exponent(conn, c)
        out.append({"currency": c, "minor": by_ccy[c],
                    "major": refdata.to_major(by_ccy[c], exp)})
    return out


def _statement_summary(conn, cycle, wire_fee_minor, fx_rate, projection_cfg):
    """Network headline + per-party breakdown for a cycle's netting set."""
    result = netting.compute(conn, cycle)           # guards Σnet=0 + reconstruction
    invoices, positions = result["invoices"], result["positions"]
    names = {r["party_id"]: r for r in
             conn.execute("SELECT party_id, legal_name, tax_id FROM parties")}
    refs = {}   # canonical_invoice_id -> source invoice_number (from the AR row)
    for ci in invoices:
        row = conn.execute("SELECT invoice_number FROM obligations WHERE obligation_id = ?",
                           (ci["ar_obligation_id"],)).fetchone()
        refs[ci["canonical_invoice_id"]] = row["invoice_number"] if row else None

    # Network value aggregates (per currency).
    gross_by_ccy, net_by_ccy = {}, {}
    for ci in invoices:
        gross_by_ccy[ci["currency"]] = gross_by_ccy.get(ci["currency"], 0) + ci["gross_amount_minor"]
    for s in positions:
        if s["net"] > 0:
            net_by_ccy[s["currency"]] = net_by_ccy.get(s["currency"], 0) + s["net"]

    # Primary currency = largest gross; anything else is "cross-currency" value
    # (used per-party for the FX leg).
    primary = max(gross_by_ccy, key=gross_by_ccy.get) if gross_by_ccy else None

    total_gross = sum(gross_by_ccy.values())        # minor, summed (exact for 1 ccy)
    total_net = sum(net_by_ccy.values())
    compression_pct = round(100 * (1 - total_net / total_gross)) if total_gross else 0

    # HONEST network settlement count: one net settlement per party with a
    # non-zero net position. We deliberately do NOT report a per-cycle "fees
    # avoided / payments eliminated" figure: summing both ledger sides of each
    # invoice double-counts, and for a ring settled through the centre the
    # distinct money-transfers don't actually drop — the per-cycle benefit is
    # VALUE compression (the gross→net hero), not transfer count.
    _, n_counts = netting.compression(invoices, positions)
    net_settlements = sum(n_counts.values())

    # AT-SCALE projection (NOT this cycle's actuals) — built BOTTOM-UP from
    # member-interview figures, never extrapolated from this cycle. Per member/
    # month = a fee-saving range + an FX-saving range; network = per-member ×
    # active members. Presented as a range, not a false-precise single figure.
    cfg = projection_cfg
    members = cfg["active_members"]
    pm_min = cfg["fee_min"] + cfg["fx_min"]      # € per member / month, low
    pm_max = cfg["fee_max"] + cfg["fx_max"]      # € per member / month, high

    # Per-party: invoices (with refs), gross AR/AP, net positions, payments, savings.
    by_party = {}   # party_id -> list of (role, ci)
    for ci in invoices:
        by_party.setdefault(ci["biller_id"], []).append(("AR", ci))
        by_party.setdefault(ci["payer_id"], []).append(("AP", ci))
    pos_by_party = {}
    for s in positions:
        pos_by_party.setdefault(s["party_id"], []).append(s)

    parties_out = []
    for pid in sorted(set(by_party) | set(pos_by_party)):
        entries = by_party.get(pid, [])
        invoice_list, gross_ar, gross_ap, party_cross = [], {}, {}, 0
        for role, ci in entries:
            exp = refdata.exponent(conn, ci["currency"])
            other = ci["payer_id"] if role == "AR" else ci["biller_id"]
            invoice_list.append({
                "ref": refs.get(ci["canonical_invoice_id"]),
                "counterparty": names[other]["legal_name"] if other in names else None,
                "direction": role, "currency": ci["currency"],
                "amount_major": refdata.to_major(ci["gross_amount_minor"], exp),
            })
            bucket = gross_ar if role == "AR" else gross_ap
            bucket[ci["currency"]] = bucket.get(ci["currency"], 0) + ci["gross_amount_minor"]
            if ci["currency"] != primary:
                party_cross += ci["gross_amount_minor"]

        positions_out = []
        for s in pos_by_party.get(pid, []):
            exp = refdata.exponent(conn, s["currency"])
            positions_out.append({
                "currency": s["currency"], "net_minor": s["net"],
                "net_major": refdata.to_major(s["net"], exp),
                "direction": "receive" if s["net"] > 0 else ("pay" if s["net"] < 0 else "flat"),
            })

        obl_count = len(entries)                    # their nettable obligation count
        fee_saved = max(0, obl_count - 1) * wire_fee_minor
        party_fx = int(round(party_cross * fx_rate))
        parties_out.append({
            "party_id": pid,
            "legal_name": names[pid]["legal_name"] if pid in names else None,
            "tax_id": names[pid]["tax_id"] if pid in names else None,
            "invoices_netted": obl_count,
            "invoice_list": invoice_list,
            "gross_ar": _money_list(conn, gross_ar),
            "gross_ap": _money_list(conn, gross_ap),
            "positions": positions_out,
            "gross_payments": obl_count, "net_payments": 1 if obl_count else 0,
            "money_saved_minor": fee_saved + party_fx,
            "money_saved_major": refdata.to_major(fee_saved + party_fx, 2),
        })

    closed = conn.execute(
        "SELECT timestamp FROM audit_log WHERE entity_ref = ? "
        "AND action IN ('cycle_netted','cycle_close') ORDER BY log_id DESC LIMIT 1",
        (f"cycle:{cycle['cycle_id']}",)).fetchone()

    return {
        "cycle_id": cycle["cycle_id"], "label": (cycle["opens_at"] or "")[:7],
        "sequence_no": cycle["sequence_no"], "state": cycle["state"],
        "provisional": result["provisional"],
        "closed_at": closed["timestamp"] if closed else None,
        "settlement_date": cycle["settlement_date"],
        "assumptions": {
            "wire_fee_minor": wire_fee_minor,
            "wire_fee_major": refdata.to_major(wire_fee_minor, 2),
            "fx_rate": fx_rate, "fx_rate_pct": round(fx_rate * 100, 3),
        },
        "network": {
            "parties_in_net": len(parties_out),
            "invoices_netted": len(invoices),
            "gross_settled": _money_list(conn, gross_by_ccy),
            "net_to_settle": _money_list(conn, net_by_ccy),
            "compression_pct": compression_pct,
            "net_settlements": net_settlements,   # honest: one net settlement per netting party
        },
        # Clearly separate from this cycle — a bottom-up projection from member
        # figures, presented as a range. The cycle's own FX stays €0 (single ccy).
        "projection": {
            "basis": "member-interview figures",
            "active_members": members,
            "per_member": {
                "fee_min_major": cfg["fee_min"], "fee_max_major": cfg["fee_max"],
                "fx_min_major": cfg["fx_min"], "fx_max_major": cfg["fx_max"],
                "total_min_major": pm_min, "total_max_major": pm_max,
            },
            "monthly_min_major": pm_min * members,
            "monthly_max_major": pm_max * members,
            "annual_min_major": pm_min * members * 12,
            "annual_max_major": pm_max * members * 12,
        },
        "parties": parties_out,
    }


@app.get("/statement/summary")
def statement_summary(cycle_id: int = None, network_id: int = 1,
                      wire_fee: float = WIRE_FEE_MAJOR_DEFAULT,
                      fx_rate: float = FX_RATE_DEFAULT,
                      fee_saving_min: float = FEE_SAVING_MIN_DEFAULT,
                      fee_saving_max: float = FEE_SAVING_MAX_DEFAULT,
                      fx_saving_min: float = FX_SAVING_MIN_DEFAULT,
                      fx_saving_max: float = FX_SAVING_MAX_DEFAULT,
                      active_members: int = ACTIVE_MEMBERS_DEFAULT):
    """Presentation-grade cycle statement: value-compression headline + per-party
    detail + a clearly-separated, bottom-up at-scale projection. The projection
    is built from per-member fee/FX saving ranges × active members (all
    adjustable, echoed back); nothing is presented as exact."""
    conn = get_conn()
    try:
        _require_audit_ok(conn)
        cycle = _active_cycle(conn, network_id, cycle_id)
        if cycle is None:
            raise HTTPException(404, "No cycle.")
        projection_cfg = {
            "fee_min": fee_saving_min, "fee_max": fee_saving_max,
            "fx_min": fx_saving_min, "fx_max": fx_saving_max,
            "active_members": active_members,
        }
        try:
            return _statement_summary(conn, cycle, int(round(wire_fee * 100)),
                                      fx_rate, projection_cfg)
        except netting.NettingInvariantError as e:
            raise HTTPException(409, f"Netting invariant failed — statement blocked: {e}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Demo controls (Phase B) — DEMO ONLY. Browser-driven cycle advance + reset so a
# live demo needs no CLI. These force the EXISTING state machine / row ops on
# demand; they are never normal user actions and must stay behind the Demo area.
# ---------------------------------------------------------------------------

# Transactional tables cleared on reset, in FK-safe order (children first).
# Reference/identity data (networks, parties, party_networks, currencies) is kept.
_RESET_TABLES = ("cycle_obligations", "net_positions", "canonical_invoices",
                 "matches", "obligations", "upload_batches", "source_formats",
                 "cycles", "counterparty_aliases", "audit_log")


@app.post("/demo/advance")
def demo_advance(network_id: int = 1):
    """Drive the active cycle all the way forward NOW (no waiting for a cut-off):
    OPEN→RECONCILING→LOCKED (freeze nettable set; roll deferred/unmatched into the
    next cycle)→NETTED (write net_positions + statement)→CLOSED, leaving the next
    cycle OPEN. DEMO ONLY."""
    conn = get_conn()
    try:
        c = conn.execute(
            "SELECT * FROM cycles WHERE network_id = ? AND state != 'closed' "
            "ORDER BY sequence_no LIMIT 1", (network_id,)).fetchone()
        if c is None:
            c = cycles.ensure_open_cycle(conn, network_id)
            conn.commit()
        cycle_id = c["cycle_id"]
        _require_audit_ok(conn)            # guard before advancing state
        state = c["state"]
        try:
            if state == "open":
                cycles.close_uploads(conn, cycle_id)
                state = "reconciling"
            if state == "reconciling":
                cycles.lock(conn, cycle_id)
                canonical.mint(conn)       # rolled obligations may move cycle
                state = "locked"
            if state == "locked":
                locked = conn.execute("SELECT * FROM cycles WHERE cycle_id = ?",
                                      (cycle_id,)).fetchone()
                _net_and_persist(conn, locked)
                conn.commit()
                state = "netted"
            if state == "netted":
                cycles.close(conn, cycle_id)
                state = "closed"
        except netting.NettingInvariantError as e:
            raise HTTPException(409, f"Netting invariant failed — advance blocked: {e}")
        except ValueError as e:
            raise HTTPException(409, str(e))
        nxt = cycles.ensure_open_cycle(conn, network_id)
        conn.commit()
        return {
            "advanced_cycle_id": cycle_id,
            "advanced": _project_cycle(conn, conn.execute(
                "SELECT * FROM cycles WHERE cycle_id = ?", (cycle_id,)).fetchone()),
            "next_cycle": _project_cycle(conn, nxt),
        }
    finally:
        conn.close()


@app.post("/demo/reset")
def demo_reset(network_id: int = 1):
    """Clear ALL transactional data and reopen a fresh cycle 1, keeping parties
    and their identities/tax_ids intact. Returns the app to a clean baseline.
    DEMO ONLY — destructive; never a normal user action."""
    conn = get_conn()
    try:
        for table in _RESET_TABLES:
            conn.execute(f"DELETE FROM {table}")
        # Tables use plain INTEGER PRIMARY KEY (rowid), so once emptied the next
        # insert restarts ids at 1 — no sqlite_sequence reset needed.
        _demo_session["current_party_id"] = CURRENT_PARTY_ID
        fresh = cycles.ensure_open_cycle(conn, network_id)
        audit.append(conn, actor="operator", action="demo_reset",
                     entity_ref=f"network:{network_id}",
                     after={"open_cycle_id": fresh["cycle_id"]})
        conn.commit()
        return {"ok": True, "open_cycle": _project_cycle(conn, fresh),
                "parties_kept": conn.execute("SELECT COUNT(*) FROM parties").fetchone()[0]}
    finally:
        conn.close()


# Serve the prototype. index.html at /, assets alongside it.
@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="frontend")
