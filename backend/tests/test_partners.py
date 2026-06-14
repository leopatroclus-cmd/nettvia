"""Phase 8.7 done-gates: tax_id as primary identifier + editable partner info.

Covers: GET /parties surfaces tax_id and derives on_network; PATCH persists
identity edits; tax_id uniqueness is blocked (app-layer); on_network is
read-only/derived; every edit lands in the hash-chained audit log.
"""


def test_parties_surfaces_tax_id_and_excludes_self_and_admin(client):
    parties = client.get("/parties").json()
    by_id = {p["party_id"]: p for p in parties}
    assert 1 not in by_id                       # signed-in party excluded
    assert 8 not in by_id                       # NAP network operator (admin) excluded
    assert by_id[3]["tax_id"] == "SG198912345R"  # tax_id present as identifier
    # on_network is derived (Skyline owns a ledger; Nile is off-network).
    assert by_id[2]["on_network"] is True
    assert by_id[6]["on_network"] is False


def test_patch_persists_identity_edits(client, conn):
    res = client.patch("/parties/2", json={
        "legal_name": "Skyline Freight Group GmbH", "country": "de", "group_id": 7})
    assert res.status_code == 200
    row = conn.execute(
        "SELECT legal_name, country, group_id, jurisdiction FROM parties WHERE party_id=2"
    ).fetchone()
    assert row["legal_name"] == "Skyline Freight Group GmbH"
    assert row["country"] == "DE"               # normalized upper-case
    assert row["group_id"] == 7
    assert row["jurisdiction"] == "DEU"         # derived from country (Delos seam)


def test_tax_id_set_and_blank_clears(client, conn):
    client.patch("/parties/6", json={"tax_id": "  EG-555-NEW  "})
    assert conn.execute("SELECT tax_id FROM parties WHERE party_id=6").fetchone()[0] == "EG-555-NEW"
    client.patch("/parties/6", json={"tax_id": ""})   # blank clears the identifier
    assert conn.execute("SELECT tax_id FROM parties WHERE party_id=6").fetchone()[0] is None


def test_tax_id_uniqueness_blocked(client, conn):
    # Pacific (party 3) owns SG198912345R; assigning it to Skyline (party 2) is blocked.
    res = client.patch("/parties/2", json={"tax_id": "SG 198912345 R"})
    assert res.status_code == 409
    assert "already used by" in res.json()["detail"]
    # The collision did not mutate Skyline's tax_id.
    assert conn.execute("SELECT tax_id FROM parties WHERE party_id=2").fetchone()[0] == "DE811907980"


def test_on_network_is_read_only(client, conn):
    # Attempting to flip on_network via PATCH is ignored — it stays derived.
    res = client.patch("/parties/2", json={"on_network": 0, "legal_name": "Skyline X"})
    assert res.json()["on_network"] is True
    assert conn.execute("SELECT on_network FROM parties WHERE party_id=2").fetchone()[0] == 1


def test_edit_writes_audit_entry(client, conn):
    client.patch("/parties/2", json={"legal_name": "Skyline Renamed GmbH"})
    entry = conn.execute(
        "SELECT actor, action, entity_ref, before, after FROM audit_log "
        "WHERE action='party.edit' AND entity_ref='party:2' ORDER BY log_id DESC LIMIT 1"
    ).fetchone()
    assert entry is not None
    assert entry["action"] == "party.edit"
    assert "Skyline Renamed GmbH" in entry["after"]
    assert "Skyline" in entry["before"]
    # The hash chain stays intact after the edit.
    assert client.get("/audit/verify").json()["ok"] is True
