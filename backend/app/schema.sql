-- Netting MVP — canonical SQLite schema (spec §4)
-- Phase 0: the full data model is defined here so later phases inherit it.
-- This phase only exercises networks / parties / obligations / matches.

PRAGMA foreign_keys = ON;

-- §4 networks
CREATE TABLE IF NOT EXISTS networks (
  network_id          INTEGER PRIMARY KEY,
  name                TEXT NOT NULL,
  cycle_length_days   INTEGER,
  cut_off_rule        TEXT,
  settlement_lag_days INTEGER,
  term_mode           TEXT CHECK (term_mode IN ('respect_due_date','standardize_to_cycle')),
  default_on_no_action TEXT CHECK (default_on_no_action IN ('roll','auto_accept')) DEFAULT 'roll',
  cost_per_payment    REAL,
  cost_components     TEXT,           -- json: bank_fee / fx_cost / admin_cost
  -- Staged deadlines: offsets (days from opens_at) for the two cut-offs.
  upload_cutoff_offset_days     INTEGER DEFAULT 25,
  processing_cutoff_offset_days INTEGER DEFAULT 29
);

-- §4 parties
CREATE TABLE IF NOT EXISTS parties (
  party_id    INTEGER PRIMARY KEY,
  legal_name  TEXT NOT NULL,
  tax_id      TEXT,                   -- strong entity-resolution key
  country     TEXT,                   -- ISO-2
  city        TEXT,                   -- shown under the partner name in the UI
  group_id    INTEGER,                -- sister-entity grouping (nullable)
  on_network  INTEGER NOT NULL DEFAULT 0,  -- bool: true => also uploads, so nettable
  jurisdiction TEXT                    -- ISO-3166 alpha-3, derived from country (Delos seam)
);

-- Currency reference: code -> minor-unit exponent (default 2; JPY=0; KWD/BHD=3).
-- Drives exponent-based major<->minor conversion at the display seam.
CREATE TABLE IF NOT EXISTS currencies (
  code                TEXT PRIMARY KEY,
  minor_unit_exponent INTEGER NOT NULL DEFAULT 2
);

-- §4 party_networks (membership join)
CREATE TABLE IF NOT EXISTS party_networks (
  party_id    INTEGER NOT NULL REFERENCES parties(party_id),
  network_id  INTEGER NOT NULL REFERENCES networks(network_id),
  role        TEXT NOT NULL DEFAULT 'participant'
                CHECK (role IN ('participant','admin')),  -- NAP operator = admin
  PRIMARY KEY (party_id, network_id)
);

-- upload_batches — one row per CSV/XLSX upload (spec §5). Holds the parsed raw
-- rows between the parse/propose step and the human-confirmed import.
CREATE TABLE IF NOT EXISTS upload_batches (
  batch_id         INTEGER PRIMARY KEY,
  party_id         INTEGER REFERENCES parties(party_id),
  network_id       INTEGER REFERENCES networks(network_id),
  source_label     TEXT,        -- filename, for display
  header_signature TEXT,        -- sorted headers; the source_formats cache key
  status           TEXT NOT NULL DEFAULT 'parsed'
                     CHECK (status IN ('parsed','imported')),
  row_count        INTEGER,
  raw_rows         TEXT,        -- json: original rows as parsed
  proposed_mapping TEXT,        -- json: source_col -> canonical_field
  created_at       TEXT
);

-- §4 source_formats — cached schema mappings
CREATE TABLE IF NOT EXISTS source_formats (
  format_id      INTEGER PRIMARY KEY,
  party_id       INTEGER REFERENCES parties(party_id),
  source_label   TEXT,
  column_mapping TEXT                 -- json
);

-- §4 counterparty_aliases — entity resolution
CREATE TABLE IF NOT EXISTS counterparty_aliases (
  alias_id          INTEGER PRIMARY KEY,
  raw_name          TEXT NOT NULL,
  resolved_party_id INTEGER REFERENCES parties(party_id),
  signals           TEXT,            -- json: tax_id / IBAN / email / address
  match_confidence  REAL,
  match_status      TEXT CHECK (match_status IN ('unresolved','proposed','confirmed'))
);

-- §4 obligations — core table
CREATE TABLE IF NOT EXISTS obligations (
  obligation_id         INTEGER PRIMARY KEY,
  owner_party_id        INTEGER NOT NULL REFERENCES parties(party_id),  -- whose ledger
  direction             TEXT NOT NULL CHECK (direction IN ('AR','AP')),
  counterparty_raw      TEXT,                                           -- as-written
  counterparty_party_id INTEGER REFERENCES parties(party_id),           -- resolved
  network_id            INTEGER REFERENCES networks(network_id),
  invoice_number        TEXT,                                           -- primary join key
  amount                INTEGER NOT NULL,                               -- minor units (cents); exact, no float drift — nets to zero
  currency              TEXT NOT NULL,
  issue_date            TEXT,                                           -- ISO yyyy-mm-dd
  due_date              TEXT,                                           -- ISO yyyy-mm-dd
  status_source         TEXT,                                           -- open / paid / partial
  po_reference          TEXT,                                           -- secondary join key
  source_format_id      INTEGER REFERENCES source_formats(format_id),
  upload_batch_id       INTEGER REFERENCES upload_batches(batch_id),
  ingest_state          TEXT NOT NULL DEFAULT 'ingested'
                          CHECK (ingest_state IN ('ingested','matched','unmatched')),
  disposition           TEXT NOT NULL DEFAULT 'pending'
                          CHECK (disposition IN ('pending','accepted','disputed','deferred')),
  settlement_mode       TEXT CHECK (settlement_mode IN ('net','direct')),
  assigned_cycle_id     INTEGER REFERENCES cycles(cycle_id),
  raw_row               TEXT,                                           -- json, audit
  -- Phase-0 seam columns: drive the prototype contract (suggested action +
  -- the row annotation the UI shows). suggested_disposition is computed from
  -- due_date / match state in later phases; persisted here for the seed.
  suggested_disposition TEXT CHECK (suggested_disposition IN ('net','direct','defer','dispute')),
  match_note            TEXT,
  -- VAT seam (Delos): populated only if the upload carries them.
  vat_treatment         TEXT,
  vat_rate              REAL,
  vat_amount_minor      INTEGER
);

-- Natural key for idempotent ingest: re-uploading the same invoice updates the
-- existing obligation instead of duplicating it (matching can't be trusted on a
-- duplicated ledger). Includes counterparty_raw so a reused invoice number
-- across different counterparties is legitimate (not a forced collision).
CREATE UNIQUE INDEX IF NOT EXISTS idx_obligation_natural
  ON obligations(owner_party_id, invoice_number, direction, counterparty_raw);

-- §4 matches
CREATE TABLE IF NOT EXISTS matches (
  match_id          INTEGER PRIMARY KEY,
  ar_obligation_id  INTEGER REFERENCES obligations(obligation_id),
  ap_obligation_id  INTEGER REFERENCES obligations(obligation_id),
  match_tier        TEXT CHECK (match_tier IN ('exact','fuzzy','manual')),
  match_confidence  REAL,
  amount_delta      INTEGER,                  -- minor units (cents)
  match_status      TEXT CHECK (match_status IN ('proposed','confirmed','rejected'))
);

-- §4 cycles — staged two-stage timeline computed from opens_at + network offsets.
CREATE TABLE IF NOT EXISTS cycles (
  cycle_id             INTEGER PRIMARY KEY,
  network_id           INTEGER REFERENCES networks(network_id),
  sequence_no          INTEGER,
  opens_at             TEXT,
  upload_cutoff_at     TEXT,   -- opens_at + upload_cutoff_offset_days   (uploads close)
  processing_cutoff_at TEXT,   -- opens_at + processing_cutoff_offset_days (lock fires)
  settlement_date      TEXT,   -- processing_cutoff_at + settlement_lag_days
  state                TEXT DEFAULT 'open'
                         CHECK (state IN ('open','reconciling','locked','netted','closed'))
);

-- §4 cycle_obligations — immutable snapshot at lock
CREATE TABLE IF NOT EXISTS cycle_obligations (
  cycle_id        INTEGER NOT NULL REFERENCES cycles(cycle_id),
  obligation_id   INTEGER NOT NULL REFERENCES obligations(obligation_id),
  frozen_amount   INTEGER,                    -- minor units (cents)
  frozen_currency TEXT,
  PRIMARY KEY (cycle_id, obligation_id)
);

-- §4 net_positions — netting output
CREATE TABLE IF NOT EXISTS net_positions (
  cycle_id            INTEGER NOT NULL REFERENCES cycles(cycle_id),
  party_id            INTEGER NOT NULL REFERENCES parties(party_id),
  currency            TEXT NOT NULL,
  gross_payable       INTEGER,                -- minor units (cents)
  gross_receivable    INTEGER,
  net_amount          INTEGER,
  gross_payment_count INTEGER,
  net_payment_count   INTEGER,
  estimated_savings   INTEGER,
  PRIMARY KEY (cycle_id, party_id, currency)
);

-- canonical_invoices — the clean, single-record OUTPUT of a confirmed match
-- (Delos-shaped). The two-sided obligations remain the reconciliation INPUT;
-- this is what Phase-7 net/statement/reconstruction will reference.
CREATE TABLE IF NOT EXISTS canonical_invoices (
  canonical_invoice_id INTEGER PRIMARY KEY,
  ar_obligation_id     INTEGER UNIQUE REFERENCES obligations(obligation_id),  -- idempotency anchor
  ap_obligation_id     INTEGER REFERENCES obligations(obligation_id),
  cycle_id             INTEGER REFERENCES cycles(cycle_id),
  biller_id            INTEGER REFERENCES parties(party_id),
  biller_jurisdiction  TEXT,
  payer_id             INTEGER REFERENCES parties(party_id),
  payer_jurisdiction   TEXT,
  currency             TEXT,
  gross_amount_minor   INTEGER,
  service_ref          TEXT,
  vat_treatment        TEXT,
  vat_rate             REAL,
  vat_amount_minor     INTEGER,
  issue_date           TEXT,
  due_date             TEXT,
  status               TEXT,
  created_at           TEXT
);

-- §4 audit_log — append-only, hash-chained trust backbone (Delos AuditEvent).
CREATE TABLE IF NOT EXISTS audit_log (
  log_id     INTEGER PRIMARY KEY,
  actor      TEXT,
  action     TEXT,
  entity_ref TEXT,
  before     TEXT,   -- json
  after      TEXT,   -- json
  timestamp  TEXT,
  prev_hash  TEXT,   -- entry_hash of the prior entry (tamper-evident chain)
  entry_hash TEXT
);
