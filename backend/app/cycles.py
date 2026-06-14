"""Cycle engine (spec §9) — staged two-stage timeline + state machine.

State machine:
  OPEN (uploading) → RECONCILING (uploads closed; matching/disposing) →
  LOCKED (immutable snapshot) → (NETTED, Phase 7) → CLOSED.

Deadlines are computed from opens_at + per-network offsets:
  upload_cutoff_at     = opens_at + upload_cutoff_offset_days
  processing_cutoff_at = opens_at + processing_cutoff_offset_days
  settlement_date      = processing_cutoff_at + settlement_lag_days

Also home to the disposition/suggestion/match-state helpers, since the
suggestion horizon comes from the live cycle (not a constant).
"""
from datetime import datetime, timedelta

from . import audit

# Prototype disposition codes <-> canonical (disposition, settlement_mode).
CODE_TO_CANONICAL = {
    "net": ("accepted", "net"),
    "direct": ("accepted", "direct"),
    "defer": ("deferred", None),
    "dispute": ("disputed", None),
}


def disp_code(disposition, settlement_mode):
    """Canonical -> prototype code, or None while pending."""
    if disposition == "accepted":
        return "direct" if settlement_mode == "direct" else "net"
    if disposition == "deferred":
        return "defer"
    if disposition == "disputed":
        return "dispute"
    return None


# --- match state ------------------------------------------------------------

def match_for(conn, oid):
    return conn.execute(
        "SELECT * FROM matches WHERE ar_obligation_id = ? OR ap_obligation_id = ? LIMIT 1",
        (oid, oid),
    ).fetchone()


def match_state(conn, row):
    """confirmed→matched, proposed→mismatch, none→one-sided (matcher ran) or pending."""
    m = match_for(conn, row["obligation_id"])
    if m:
        return "matched" if m["match_status"] == "confirmed" else "mismatch"
    return "pending" if row["ingest_state"] == "ingested" else "unmatched"


# --- suggested / effective disposition (horizon from the live cycle) --------

def is_long_dated(due_iso, horizon_iso):
    """Due beyond this cycle's processing cut-off → settles in a later cycle."""
    if not due_iso or not horizon_iso:
        return False
    return (datetime.strptime(due_iso, "%Y-%m-%d").date()
            > datetime.strptime(horizon_iso, "%Y-%m-%d").date())


def suggested(row, match, horizon_iso):
    """Suggested disposition from match state + due date vs the cycle horizon."""
    if match == "mismatch":
        return "dispute"
    if match in ("unmatched", "pending"):
        return "direct"
    return "defer" if is_long_dated(row["due_date"], horizon_iso) else "net"


def effective_code(row, match, horizon_iso):
    """The persisted choice, or — while pending — the computed suggestion
    (upload implies acceptance of your own side, spec §7)."""
    return disp_code(row["disposition"], row["settlement_mode"]) or suggested(row, match, horizon_iso)


def horizon_for(conn, row):
    """Processing cut-off of the obligation's cycle (falls back to the open one)."""
    cid = row["assigned_cycle_id"]
    c = None
    if cid:
        c = conn.execute(
            "SELECT processing_cutoff_at FROM cycles WHERE cycle_id = ?", (cid,)
        ).fetchone()
    if c is None:
        c = current_open_cycle(conn, row["network_id"])
    return c["processing_cutoff_at"] if c else None


def pair_state(conn, row, match, my_code, horizon_iso):
    """Derived nettable state (spec §8): (cp_net, nettable)."""
    if match != "matched":
        return None, False
    m = match_for(conn, row["obligation_id"])
    if not m or m["match_status"] != "confirmed":
        return None, False
    other_id = (m["ap_obligation_id"] if m["ar_obligation_id"] == row["obligation_id"]
                else m["ar_obligation_id"])
    other = conn.execute(
        "SELECT * FROM obligations WHERE obligation_id = ?", (other_id,)
    ).fetchone()
    cp_net = other is not None and effective_code(other, "matched", horizon_iso) == "net"
    return cp_net, (my_code == "net" and cp_net)


# --- cycle deadlines + lifecycle -------------------------------------------

def _add_days(iso, n):
    return (datetime.strptime(iso, "%Y-%m-%d").date() + timedelta(days=n or 0)).isoformat()


def open_cycle(conn, network_id, opens_at, sequence_no):
    """Create an OPEN cycle, computing its staged deadlines from network config."""
    net = conn.execute(
        "SELECT cycle_length_days, settlement_lag_days, upload_cutoff_offset_days, "
        "processing_cutoff_offset_days FROM networks WHERE network_id = ?",
        (network_id,),
    ).fetchone()
    upload_cut = _add_days(opens_at, net["upload_cutoff_offset_days"])
    proc_cut = _add_days(opens_at, net["processing_cutoff_offset_days"])
    settle = _add_days(proc_cut, net["settlement_lag_days"])
    cur = conn.execute(
        "INSERT INTO cycles (network_id, sequence_no, opens_at, upload_cutoff_at, "
        "processing_cutoff_at, settlement_date, state) VALUES (?,?,?,?,?,?, 'open')",
        (network_id, sequence_no, opens_at, upload_cut, proc_cut, settle),
    )
    return conn.execute(
        "SELECT * FROM cycles WHERE cycle_id = ?", (cur.lastrowid,)
    ).fetchone()


def current_open_cycle(conn, network_id):
    return conn.execute(
        "SELECT * FROM cycles WHERE network_id = ? AND state = 'open' "
        "ORDER BY sequence_no DESC LIMIT 1",
        (network_id,),
    ).fetchone()


def ensure_open_cycle(conn, network_id):
    """Guarantee a network always has exactly one OPEN cycle for new uploads."""
    c = current_open_cycle(conn, network_id)
    if c:
        return c
    last = conn.execute(
        "SELECT c.*, n.cycle_length_days FROM cycles c JOIN networks n "
        "ON n.network_id = c.network_id WHERE c.network_id = ? "
        "ORDER BY c.sequence_no DESC LIMIT 1",
        (network_id,),
    ).fetchone()
    if last:
        opens_at = _add_days(last["opens_at"], last["cycle_length_days"])
        seq = last["sequence_no"] + 1
    else:
        opens_at, seq = "2026-06-01", 1
    return open_cycle(conn, network_id, opens_at, seq)


def close_uploads(conn, cycle_id):
    """OPEN → RECONCILING. Late uploads route to the next open cycle."""
    c = conn.execute("SELECT * FROM cycles WHERE cycle_id = ?", (cycle_id,)).fetchone()
    if c is None:
        raise ValueError("Unknown cycle.")
    if c["state"] != "open":
        raise ValueError(f"close-uploads requires an OPEN cycle (was '{c['state']}').")
    conn.execute("UPDATE cycles SET state = 'reconciling' WHERE cycle_id = ?", (cycle_id,))
    ensure_open_cycle(conn, c["network_id"])   # so new uploads have somewhere to land
    audit.append(conn, actor="operator", action="cycle_close_uploads",
                 entity_ref=f"cycle:{cycle_id}",
                 before={"state": "open"}, after={"state": "reconciling"})
    conn.commit()
    return conn.execute("SELECT * FROM cycles WHERE cycle_id = ?", (cycle_id,)).fetchone()


def lock(conn, cycle_id):
    """RECONCILING → LOCKED. Resolve no-action obligations per default_on_no_action,
    freeze the nettable set into cycle_obligations (immutable), open the next cycle."""
    c = conn.execute("SELECT * FROM cycles WHERE cycle_id = ?", (cycle_id,)).fetchone()
    if c is None:
        raise ValueError("Unknown cycle.")
    if c["state"] != "reconciling":
        raise ValueError(f"lock requires a RECONCILING cycle (was '{c['state']}').")

    nxt = ensure_open_cycle(conn, c["network_id"])
    policy = conn.execute(
        "SELECT default_on_no_action FROM networks WHERE network_id = ?", (c["network_id"],)
    ).fetchone()["default_on_no_action"]
    horizon = c["processing_cutoff_at"]

    obls = conn.execute(
        "SELECT * FROM obligations WHERE assigned_cycle_id = ?", (cycle_id,)
    ).fetchall()
    frozen = 0
    for o in obls:
        match = match_state(conn, o)
        code = disp_code(o["disposition"], o["settlement_mode"])
        if code is None:                       # no action by the processing cut-off
            if policy == "auto_accept":
                code = suggested(o, match, horizon)
                disp, mode = CODE_TO_CANONICAL[code]
                conn.execute(
                    "UPDATE obligations SET disposition = ?, settlement_mode = ? "
                    "WHERE obligation_id = ?", (disp, mode, o["obligation_id"]))
            else:                              # 'roll' → defer to next cycle
                conn.execute(
                    "UPDATE obligations SET assigned_cycle_id = ? WHERE obligation_id = ?",
                    (nxt["cycle_id"], o["obligation_id"]))
                continue

        _, nettable = pair_state(conn, o, match, code, horizon)
        if code == "net" and nettable:         # both sides accept & net, confirmed → freeze
            conn.execute(
                "INSERT INTO cycle_obligations (cycle_id, obligation_id, frozen_amount, "
                "frozen_currency) VALUES (?,?,?,?)",
                (cycle_id, o["obligation_id"], o["amount"], o["currency"]))
            frozen += 1
        elif code in ("defer", "dispute"):     # long-dated / contested → roll forward
            conn.execute(
                "UPDATE obligations SET assigned_cycle_id = ? WHERE obligation_id = ?",
                (nxt["cycle_id"], o["obligation_id"]))
        # 'net' (accepted to net — frozen above when nettable; otherwise the party
        # acted on it this cycle, so it stays in the CLOSING cycle and does NOT
        # roll) and 'direct' (settled outside netting) both remain here. Net is
        # never rolled, so accept & net always clears the open Accounts view.

    conn.execute("UPDATE cycles SET state = 'locked' WHERE cycle_id = ?", (cycle_id,))
    audit.append(conn, actor="operator", action="cycle_lock",
                 entity_ref=f"cycle:{cycle_id}",
                 before={"state": "reconciling"},
                 after={"state": "locked", "frozen_obligations": frozen})
    conn.commit()
    return frozen


def close(conn, cycle_id):
    """NETTED → CLOSED. Settlement is complete (demo: immediate); the next cycle
    is already open (created at lock). Idempotently guarantees a next open cycle."""
    c = conn.execute("SELECT * FROM cycles WHERE cycle_id = ?", (cycle_id,)).fetchone()
    if c is None:
        raise ValueError("Unknown cycle.")
    if c["state"] != "netted":
        raise ValueError(f"close requires a NETTED cycle (was '{c['state']}').")
    ensure_open_cycle(conn, c["network_id"])
    conn.execute("UPDATE cycles SET state = 'closed' WHERE cycle_id = ?", (cycle_id,))
    audit.append(conn, actor="operator", action="cycle_close",
                 entity_ref=f"cycle:{cycle_id}",
                 before={"state": "netted"}, after={"state": "closed"})
    conn.commit()
    return conn.execute("SELECT * FROM cycles WHERE cycle_id = ?", (cycle_id,)).fetchone()


def is_locked(conn, cycle_id):
    if not cycle_id:
        return False
    c = conn.execute("SELECT state FROM cycles WHERE cycle_id = ?", (cycle_id,)).fetchone()
    return c is not None and c["state"] in ("locked", "netted", "closed")
