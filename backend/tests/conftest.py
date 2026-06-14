"""Test harness — each test runs against a fresh, isolated, seeded SQLite DB.

The dev DB (app/netting.db) is never touched: we monkeypatch db.DB_PATH to a
temp file and seed it (reset=True), so every test starts from the canonical
seed + computed matches + canonical invoices.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # backend/


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    from app import db
    p = str(tmp_path / "test.db")
    monkeypatch.setattr(db, "DB_PATH", p)
    db.init_db(reset=True)        # seed → match → mint canonical invoices
    return p


@pytest.fixture(autouse=True)
def _reset_demo_party():
    """The demo 'acting as' party is process-wide; reset it to Aegean (1) so a
    test that switches parties can't leak into the next one."""
    from app import main
    main._demo_session["current_party_id"] = main.CURRENT_PARTY_ID
    yield


@pytest.fixture
def conn(db_path):
    from app.db import get_conn
    c = get_conn()
    yield c
    c.close()


@pytest.fixture
def client(db_path):
    from app.main import app
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c


# ── fixture CSVs ────────────────────────────────────────────────────────────

# Messy non-canonical headers; Skyline carries a matching VAT (deterministic),
# Pacific a matching country (fuzzy), Meridian is genuinely new.
AP_EXPORT = (
    "Doc No,Counterparty,Counterparty VAT,Country,Amount,Ccy,Issued,Due,Type\n"
    "INV-7001,SKYLINE FRT GMBH,DE 811907980,DE,\"5,400.00\",EUR,2026-06-10,2026-06-30,Purchase\n"
    "INV-7002,Pacific Forwarders,,SG,\"6,200.00\",USD,2026-06-11,2026-07-01,Sales\n"
    "INV-7003,Meridian Cargo SARL,,FR,\"3,100.00\",EUR,2026-06-12,2026-06-28,Sales\n"
)


def upload_confirm(client, name, content):
    """Upload a CSV and confirm import. Returns (upload_json, confirm_response)."""
    r = client.post("/upload", files={"file": (name, content, "text/csv")})
    if r.status_code != 200:
        return r.json(), r
    batch = r.json()["batch_id"]
    return r.json(), client.post(f"/upload/{batch}/confirm")


def obligation_count(conn):
    return conn.execute("SELECT COUNT(*) FROM obligations").fetchone()[0]
