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

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import (audit, canonical, cycles, ingest, mapping, matching, netting,
               refdata, resolve)
from .db import get_conn, init_db

_SYM = {"EUR": "€", "USD": "$", "CHF": "CHF ", "GBP": "£"}

# The signed-in party in the prototype (Aegean Air Cargo S.A.).
CURRENT_PARTY_ID = 1

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


@app.get("/obligations")
def get_obligations(party_id: int = CURRENT_PARTY_ID):
    """Obligations owned by the signed-in party, in the prototype's shape."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM obligations WHERE owner_party_id = ? "
            "ORDER BY obligation_id",
            (party_id,),
        ).fetchall()
        return [_project(r, conn) for r in rows]
    finally:
        conn.close()


@app.post("/dispositions")
def set_dispositions(payload: dict = Body(...), party_id: int = CURRENT_PARTY_ID):
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
async def upload(file: UploadFile = File(...), party_id: int = Form(CURRENT_PARTY_ID)):
    """Parse a CSV/XLSX, propose (or recall) a schema mapping, stage the batch.

    Returns the proposed mapping + summary counts for the Upload screen. No
    obligations are created until /upload/{batch_id}/confirm.
    """
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
def list_resolutions(party_id: int = CURRENT_PARTY_ID):
    """Counterparties still awaiting a resolution decision."""
    conn = get_conn()
    try:
        return _pending_list(conn, party_id)
    finally:
        conn.close()


@app.post("/resolutions/confirm")
def confirm_resolution(payload: dict = Body(...), party_id: int = CURRENT_PARTY_ID):
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
def get_netting(party_id: int = CURRENT_PARTY_ID, network_id: int = 1):
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


@app.post("/cycles/{cycle_id}/net")
def run_netting(cycle_id: int):
    """LOCKED → NETTED: compute net positions over the snapshot and persist them
    as a (regenerable) projection / statement record."""
    conn = get_conn()
    try:
        cycle = conn.execute("SELECT * FROM cycles WHERE cycle_id = ?", (cycle_id,)).fetchone()
        if cycle is None:
            raise HTTPException(404, "Unknown cycle.")
        if cycle["state"] != "locked":
            raise HTTPException(409, f"Netting requires a LOCKED cycle (was '{cycle['state']}').")
        _require_audit_ok(conn)            # guard before advancing state
        try:
            result = netting.compute(conn, cycle)   # guards Σnet=0 + reconstruction
        except netting.NettingInvariantError as e:
            raise HTTPException(409, f"Netting invariant failed — cycle not netted: {e}")
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
        conn.commit()
        return _netting_response(conn,
                                 conn.execute("SELECT * FROM cycles WHERE cycle_id = ?",
                                              (cycle_id,)).fetchone(), CURRENT_PARTY_ID)
    finally:
        conn.close()


@app.get("/statement")
def statement(party_id: int = CURRENT_PARTY_ID, cycle_id: int = None, network_id: int = 1):
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


# Serve the prototype. index.html at /, assets alongside it.
@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="frontend")
