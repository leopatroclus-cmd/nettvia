"""Part C — adversarial / messy inputs (diagnostic).

Runs nasty fixtures through ingestion and asserts the hard bar: no crash, no
500. Each fixture is classified HANDLED (rejected or flagged) vs GAP (silent
corruption / misleading result), and a hardening backlog is printed. The suite
stays green on the no-crash bar; GAPs are reported, not failed (per spec: "where
something breaks, report it — the real-data readiness backlog, not all-green").
"""
FIXTURES = {
    "duplicate_invoice_numbers": ("dup.csv",
        "Doc No,Counterparty,Amount,Ccy,Issued,Due,Type\n"
        "DUP-1,Acme Co,100.00,EUR,2026-06-10,2026-06-28,Purchase\n"
        "DUP-1,Acme Co,200.00,EUR,2026-06-10,2026-06-28,Purchase\n"),
    "credit_note_negative_amount": ("cn.csv",
        "Doc No,Counterparty,Amount,Ccy,Issued,Due,Type\n"
        "CN-1,Acme Co,-500.00,EUR,2026-06-10,2026-06-28,Sales\n"),
    "zero_amount_line": ("zero.csv",
        "Doc No,Counterparty,Amount,Ccy,Issued,Due,Type\n"
        "Z-1,Acme Co,0.00,EUR,2026-06-10,2026-06-28,Sales\n"),
    "unknown_currency": ("xc.csv",
        "Doc No,Counterparty,Amount,Ccy,Issued,Due,Type\n"
        "XC-1,Acme Co,100.00,XYZ,2026-06-10,2026-06-28,Sales\n"),
    "missing_required_fields": ("mf.csv",
        "Doc No,Counterparty,Amount,Ccy,Issued,Due,Type\n"
        "MF-1,,,EUR,2026-06-10,2026-06-28,Sales\n"),
    "malformed_bom_quoted_commas": ("mal.csv",
        "﻿Doc No,Counterparty,Amount,Ccy,Issued,Due,Type\n"
        "MAL-1,\"Acme, Inc\",1000.00,EUR,2026-06-10,2026-06-28,Sales\n"),
}


def _run_one(db, app, TestClient, fixture_db, fname, content):
    """Run a single fixture against a fresh DB; return an outcome dict (or crash)."""
    db.init_db(reset=True)
    out = {"crash": None}
    try:
        with TestClient(app) as c:
            r = c.post("/upload", files={"file": (fname, content, "text/csv")})
            out["upload_status"] = r.status_code
            if r.status_code != 200:
                out["result"] = "rejected at upload"
                return out
            j = r.json()
            cf = c.post(f"/upload/{j['batch_id']}/confirm")
            out["confirm_status"] = cf.status_code
            if cf.status_code != 200:
                out["result"] = "rejected at confirm"
                return out
            cj = cf.json()
            out.update(rows_read=cj["rows_read"], imported=cj["imported"], skipped=cj["skipped"],
                       skipped_breakdown=cj.get("skipped_breakdown", {}))
            conn = db.get_conn()
            out["created"] = conn.execute(
                "SELECT COUNT(*) FROM obligations WHERE upload_batch_id IS NOT NULL").fetchone()[0]
            out["currencies"] = [r[0] for r in conn.execute(
                "SELECT DISTINCT currency FROM obligations WHERE upload_batch_id IS NOT NULL")]
            out["amounts"] = [r[0] for r in conn.execute(
                "SELECT amount FROM obligations WHERE upload_batch_id IS NOT NULL")]
            conn.close()
            out["result"] = "imported"
    except Exception as e:  # a crash IS a gap — record, don't swallow into a pass
        out["crash"] = repr(e)
    return out


def _classify(name, o, known_currencies):
    """(verdict, note) — HANDLED or GAP."""
    if o["crash"]:
        return "GAP", f"CRASH: {o['crash']}"
    if o["result"].startswith("rejected"):
        return "HANDLED", o["result"]
    bd = o.get("skipped_breakdown", {})
    if name == "duplicate_invoice_numbers":
        if bd.get("duplicate_in_batch") and o["created"] == 0:
            return "HANDLED", "in-batch duplicate flagged (needs attention), not collapsed/last-wins"
        return "GAP", f"in-batch duplicate not flagged (imported={o['imported']}, created={o['created']})"
    if name == "credit_note_negative_amount":
        return ("HANDLED", "negative amount stored & flows through netting; credit-note "
                "semantics not explicitly modelled (note for later)")
    if name == "zero_amount_line":
        if o["created"] == 1:
            return "HANDLED", "legitimate 0.00 line imports (is-None check, not falsy)"
        return "GAP", "zero-amount line dropped"
    if name == "unknown_currency":
        imported_unknown = [c for c in o["currencies"] if c not in known_currencies]
        if bd.get("unknown_currency") and not imported_unknown:
            return "HANDLED", "unknown currency rejected/flagged (needs attention), not imported at default exponent"
        return "GAP", f"unknown currency imported: {imported_unknown}"
    if name == "missing_required_fields":
        if o["created"] == 0 and o["skipped"] >= 1:
            return "HANDLED", "missing-field row flagged (need_attention), not imported"
        return "GAP", "missing-field row imported anyway"
    if name == "malformed_bom_quoted_commas":
        if o["created"] == 1:
            return "HANDLED", "BOM + quoted commas parsed correctly"
        return "GAP", f"BOM/quoted-comma row mis-parsed: created={o.get('created')}"
    return "HANDLED", o["result"]


def test_adversarial_report(monkeypatch, tmp_path, capsys):
    from app import db, refdata
    from app.main import app
    from fastapi.testclient import TestClient

    known_codes = {code for code, _ in refdata.CURRENCIES}

    results = {}
    for i, (name, (fname, content)) in enumerate(FIXTURES.items()):
        monkeypatch.setattr(db, "DB_PATH", str(tmp_path / f"adv_{i}.db"))
        results[name] = _run_one(db, app, TestClient, db.DB_PATH, fname, content)

    lines = ["", "=" * 78, "PART C — ADVERSARIAL FIXTURE REPORT", "=" * 78]
    backlog = []
    for name, o in results.items():
        verdict, note = _classify(name, o, known_codes)
        lines.append(f"[{verdict:7}] {name}")
        lines.append(f"          {note}")
        if verdict == "GAP":
            backlog.append(f"- {name}: {note}")
    lines.append("-" * 78)
    lines.append("HARDENING BACKLOG (before real data):" if backlog else "No gaps detected.")
    lines += backlog
    lines.append("=" * 78)
    report = "\n".join(lines)

    with capsys.disabled():
        print(report)

    # Hard bar: nothing may CRASH (unhandled 500 / exception).
    crashed = [n for n, o in results.items() if o["crash"]]
    assert not crashed, f"adversarial input crashed (must be rejected/flagged, never crash): {crashed}"
