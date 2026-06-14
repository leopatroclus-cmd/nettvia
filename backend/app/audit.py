"""Hash-chained audit log (Phase 6.5) — tamper-evident, Delos AuditEvent shape.

Each entry's entry_hash = sha256(prev_hash | canonical(entry fields)); prev_hash
links to the prior entry. verify() recomputes the chain end-to-end.
"""
import hashlib
import json
from datetime import datetime

GENESIS = "GENESIS"


def _canonical(actor, action, entity_ref, before_str, after_str, ts):
    return json.dumps([actor, action, entity_ref, before_str, after_str, ts],
                      sort_keys=True, default=str)


def _hash(prev, payload):
    return hashlib.sha256((prev + "|" + payload).encode("utf-8")).hexdigest()


def append(conn, actor, action, entity_ref, before=None, after=None, timestamp=None):
    """Append one hash-chained audit event. Does not commit (caller owns the txn)."""
    ts = timestamp or datetime.utcnow().isoformat()
    before_str = None if before is None else json.dumps(before, sort_keys=True, default=str)
    after_str = None if after is None else json.dumps(after, sort_keys=True, default=str)

    last = conn.execute(
        "SELECT entry_hash FROM audit_log ORDER BY log_id DESC LIMIT 1"
    ).fetchone()
    prev = last["entry_hash"] if last and last["entry_hash"] else GENESIS
    entry_hash = _hash(prev, _canonical(actor, action, entity_ref, before_str, after_str, ts))

    conn.execute(
        "INSERT INTO audit_log "
        "(actor, action, entity_ref, before, after, timestamp, prev_hash, entry_hash) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (actor, action, entity_ref, before_str, after_str, ts, prev, entry_hash),
    )
    return entry_hash


def verify(conn):
    """Recompute the chain. Returns {ok, count, broken_log_id}."""
    rows = conn.execute(
        "SELECT log_id, actor, action, entity_ref, before, after, timestamp, "
        "prev_hash, entry_hash FROM audit_log ORDER BY log_id"
    ).fetchall()
    prev = GENESIS
    for r in rows:
        expect = _hash(prev, _canonical(r["actor"], r["action"], r["entity_ref"],
                                        r["before"], r["after"], r["timestamp"]))
        if r["prev_hash"] != prev or r["entry_hash"] != expect:
            return {"ok": False, "count": len(rows), "broken_log_id": r["log_id"]}
        prev = r["entry_hash"]
    return {"ok": True, "count": len(rows), "broken_log_id": None}
