# Netting MVP — backend

FastAPI + SQLite. The API serves the prototype as static files (single-origin),
exposes the obligations the Accounts screen renders, and ingests CSV/XLSX
uploads into canonical obligations.

- **Phase 0** — the frontend↔backend seam: the Accounts table loads from
  `GET /obligations` instead of a hardcoded array.
- **Phase 2** — ingestion + schema mapping (spec §5, §6a): upload a file →
  Claude (`claude-haiku-4-5`, or a deterministic fallback) proposes a
  `column → canonical_field` mapping → confirm → rows land as canonical
  obligations and show in Accounts.
- **Phase 3** — entity resolution (spec §6b): on import, each obligation's
  `counterparty_raw` is resolved to a canonical party.
- **Phase 4** — transaction matching (spec §6c): obligations are paired across
  resolved counterparties (A's AR to B ↔ B's AP to A), driving the real Accounts
  match badges. Ingest is idempotent (UPSERT on a natural key).
- **Phase 5** — disposition + nettable (spec §7, §8): persist each obligation's
  disposition and derive the nettable state.
- **Phase 6** — cycle engine with **staged deadlines** (spec §9): a two-stage
  timeline (upload cut-off → processing cut-off) and the OPEN → RECONCILING →
  LOCKED state machine. No netting calc/statement/reveal yet (Phase 7).
- **Phase 6.5** — forward-compat seams (Delos-shaping), additive only: party
  jurisdiction, currency exponents, a VAT seam, canonical-invoice minting, and a
  hash-chained audit log.
- **Phase 7** — netting calc + statement + reveal, computed over the canonical
  invoices in a locked cycle's snapshot. The finale.
- **Phase 8** — readiness: a pytest regression suite (done-gates), blocking
  runtime guards on the core invariants, and adversarial fixtures with a
  pass/break report.

## Money is stored in integer minor units (cents)

`obligations.amount` (and `matches.amount_delta`, snapshot/`net_positions`
money) are `INTEGER` cents — exact, no float drift, since balances net to zero.
The API divides by 100 for the prototype's whole-currency display.

- **Phase 9a** — deploy prep: env-driven config, seed-only-when-empty startup, a
  Dockerfile, and secrets hygiene (see Deploy).

## Money is stored in integer minor units

(`obligations.amount`, `matches.amount_delta`, snapshot/`net_positions`) are
`INTEGER` minor units — exact, no float drift. Major↔minor conversion uses the
currency's exponent at the display/ingest seam (so JPY/KWD are correct).

## Run (local dev)

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m app.db --reset    # dev: wipe + reseed app/netting.db
.venv/bin/uvicorn app.main:app --reload --port 8000
```

Open <http://localhost:8000/> → the Accounts screen renders from the API.
`python -m app.db` (no flag) ensures schema + seeds **only if empty** (the
production-safe path); `--reset` is the dev/test wipe.

## Deploy (Phase 9a)

Configured from the environment; nothing hardcoded:

- **`DB_PATH`** — SQLite file location. Local default `app/netting.db`; in prod
  point at a mounted volume, e.g. `/data/netting.db`, so data persists.
- **`PORT`** — injected by the host; the Docker `CMD` runs
  `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
- **`ANTHROPIC_API_KEY`** — optional, env-only (deterministic fallback without
  it). Never committed; `.env` is gitignored (`.env.example` documents the vars).

**Startup never wipes data:** `init_db()` runs the (idempotent) schema and seeds
the demo set **only when there are no obligations yet**. A redeploy/restart on an
existing volume preserves everything. `reset_db()` (dev/tests only) is the sole
wipe path and is never called from startup.

**Container** (`Dockerfile` + `.dockerignore` at repo root, single-origin — no
CORS):

```bash
docker build -t nettvia .
docker run -p 8000:8000 -e PORT=8000 -e DB_PATH=/data/netting.db \
  -v nettvia-data:/data nettvia
```

On Railway: deploy from the repo (Dockerfile detected), add a **volume mounted at
`/data`**, set `DB_PATH=/data/netting.db` (and `ANTHROPIC_API_KEY` if used).
`PORT` is provided automatically.

## Layout

- `app/schema.sql` — canonical SQLite schema (spec §4, all tables).
- `app/seed.py` — the messy mock set: matched pairs, the Pacific mismatch,
  the Nile one-sided, the Adriatic dispute, multi-currency EUR/USD/CHF, the
  Levant net-90 defer — plus the counterparty **mirror** rows (a real
  two-sided ledger; 17 obligations total, 9 owned by the signed-in party).
- `app/main.py` — `GET /obligations` projects canonical rows into the exact
  shape `render()` consumes: `{cp, on, dir, amt, c, iss, due, match, sugg, note}`.
  Match state (`matched | mismatch | unmatched`) is derived from `ingest_state`
  + the `matches` table; dates are formatted to the prototype's `DD Mon`.

## The seam (contract → prototype, not the reverse)

| prototype field | source |
|---|---|
| `cp` / `on`  | counterparty party `legal_name` / `on_network` |
| `dir` / `amt` / `c` | obligation `direction` / `amount` / `currency` |
| `iss` / `due` | `issue_date` / `due_date`, formatted `DD Mon` |
| `match` | derived: `unmatched` if no/none confirmed, `mismatch` if proposed, else `matched` |
| `sugg` / `note` | `suggested_disposition` / `match_note` |

## Ingestion (Phase 2)

- `app/ingest.py` — parse CSV/XLSX (pandas/openpyxl) and normalize a mapped
  row into a canonical obligation: amount → cents, direction (`Sales`/`AR`… →
  `AR`, `Purchase`/`AP`… → `AP`), currency, dates → ISO.
- `app/mapping.py` — `propose_mapping(headers, sample_rows)`. Primary path is
  the Anthropic API (`claude-opus-4-8`, structured output). With no
  `ANTHROPIC_API_KEY` it falls back to a deterministic header heuristic, so the
  flow is demonstrable offline.
- **Endpoints:**
  - `POST /upload` (multipart `file`) → parse, propose-or-recall mapping, stage
    an `upload_batch`. Returns the mapping + real `rows_read / auto_processed /
    need_attention` counts. A cache hit (same party + header set, seen before)
    skips the LLM and returns `cached: true`.
  - `POST /upload/{batch_id}/confirm` → cache the mapping in `source_formats`,
    normalize the staged rows into obligations (owner = uploading party,
    network = NAP, `counterparty_party_id` NULL, `ingest_state = 'ingested'`),
    and mark the batch imported.
- `sample_data/cargowise_AR_export.csv` — a messy fixture (non-canonical
  headers, mixed date formats, one row missing required fields).

## Entity resolution (Phase 3)

- `app/resolve.py` — layered, AI-proposes / human-confirms:
  1. **Deterministic** — exact `tax_id` (VAT/registration) vs existing parties →
     auto-confirm.
  2. **Fuzzy** — normalized-name similarity (rapidfuzz) + a corroborating signal
     (country) above threshold → propose.
  3. **LLM** — ambiguous clusters only, `claude-sonnet-4-6` proposes
     likely-same-party (no-op without an API key; the first two layers carry the
     demo).
- Runs inside `/upload/{id}/confirm`: deterministic + known-alias counterparties
  auto-resolve (set `counterparty_party_id`); fuzzy/LLM/unresolved surface in the
  Upload panel. Counterparty signals (`tax_id` / `country` / `iban`) are mapped
  as extra canonical columns and read per row (`ingest.extract_signals`).
- **Aliases** (`counterparty_aliases`): every confirmed resolution is stored
  `raw_name → resolved_party_id` and **never re-asked** — a known alias
  auto-resolves on the next encounter.
- **`on_network` is derived**: a party is on-network iff it also uploads a ledger
  (owns ≥1 obligation). New parties created via "It's new" are off-network.
- **Endpoints:** `GET /resolutions` (pending), `POST /resolutions/confirm`
  (`{raw_name, party_id}` → existing party, or `{raw_name}` → create a new
  off-network party; resolves all of that counterparty's obligations).
- `sample_data/ap_export_with_taxids.csv` — Skyline variant + same VAT
  (deterministic), Pacific variant + matching country (fuzzy propose), Meridian
  (genuinely new).

## Transaction matching (Phase 4)

- **Idempotent ingest** — a `UNIQUE` index on `(owner_party_id, invoice_number,
  direction)` + `INSERT … ON CONFLICT … DO UPDATE` means re-uploading the same
  invoice updates the row instead of duplicating it (a prior resolution is
  preserved). Matching can't be trusted on a duplicated ledger, so this is fixed
  first.
- `app/matching.py` `match_all(conn)` — pairs each AR obligation to its mirror AP
  on the counterparty's ledger. Tiered:
  - **Tier 1 (exact / auto-confirm)** — same normalized `invoice_number` + amount
    within tolerance (1% of the larger side, floor €/$1) + same currency +
    resolved pair → `match_status='confirmed'`.
  - **Tier 2 (fuzzy / review)** — `invoice_number` matches but amount beyond
    tolerance (the mismatch), OR a strong partial (amount within tolerance + same
    `due_date`, invoice missing/differing) → `match_status='proposed'`.
  - **Tier 3 (exception)** — no confident mirror → left unmatched (one-sided).
- The full match set is **computed, not seeded** — recomputed (idempotently)
  after seeding, after each import, and after each resolution confirmation.
- **Badges are driven by real match state** for every row (`_match_state`):
  `confirmed→Matched`, `proposed→Mismatch` (with the counterparty's amount shown),
  `resolved-but-no-mirror→One-sided`, `counterparty-unresolved→Pending`. The
  "needs attention" filter surfaces Tier-2 + Tier-3 (mismatch + one-sided), not
  Pending.

Scope held: **pairing only** — no disposition wiring (Phase 5), no net positions
or cycle (6–7). Metrics strip and the Netting/Partners screens remain the
prototype's static values.

## Disposition + nettable (Phase 5)

- **Persisted disposition** — `obligations.disposition` ∈ {pending, accepted,
  disputed, deferred} + `settlement_mode` ∈ {net, direct} when accepted. The
  prototype's four codes map to these: `net`→accepted/net, `direct`→accepted/
  direct, `defer`→deferred, `dispute`→disputed.
- **`POST /dispositions`** `{ids:[...], disp}` — sets one or many. **Server-
  enforced gate (spec §8):** `disp:"net"` is rejected (400) unless the obligation
  has a confirmed match.
- **Suggested disposition is computed** from match state + due date vs the open
  cycle (`_suggested`): mismatch→dispute, one-sided/pending→settle-direct,
  confirmed match due in-cycle→accept&net, long-dated (beyond cut-off + one cycle
  length)→defer. The seeded `sugg` is no longer used.
- **Effective disposition** = the persisted choice, or — while still `pending` —
  the computed suggestion (upload implies acceptance of your own side, spec §7).
  So an obligation defaults to its suggestion and the user overrides.
- **Nettable is derived, not a button** (`_pair_state`, spec §8): a confirmed
  match is nettable iff **both** sides chose accept & net (neither disputed/
  deferred/direct). The projection exposes `nettable` and `cpNet` (the
  counterparty side accepted) so the Accounts row can surface it. The demo seeds
  the **counterparty** side of every pair to accept & net, so a pair goes
  nettable the moment the signed-in party accepts its side — the per-side logic
  is real.
- **Durability:** dispositions live on the obligation and are never touched by
  the matcher; the ingest UPSERT preserves them on re-import. The matcher also
  preserves human-touched matches (`match_tier='manual'`) across its full
  recompute.
- The Accounts tally footer and the disposition buttons are driven by this real
  state (existing UI, no redesign).

Scope held: **disposition + nettable only** — no cycle open/lock/snapshot (6),
no net positions/statement (7).

## Cycle engine — staged deadlines (Phase 6) — `app/cycles.py`

- **Two-stage timeline.** Network config carries `upload_cutoff_offset_days`
  (25) and `processing_cutoff_offset_days` (29) alongside `cycle_length_days`.
  A cycle computes from `opens_at`: `upload_cutoff_at`, `processing_cutoff_at`,
  and `settlement_date` (= processing cut-off + `settlement_lag_days`). The old
  single `cut_off_at` is gone.
- **State machine:** `OPEN` (uploading) → `RECONCILING` (uploads closed;
  matching/disposing continue) → `LOCKED` (immutable snapshot) → (`NETTED`,
  Phase 7) → `CLOSED`.
- **Transitions** (operator endpoints):
  - `POST /cycles/{id}/close-uploads` — `OPEN → RECONCILING`, and opens the next
    cycle so late uploads route there (new obligations enter the network's
    current OPEN cycle on ingest).
  - `POST /cycles/{id}/lock` — requires `RECONCILING`. Resolves no-action
    obligations per `default_on_no_action` (`roll` → next cycle, `auto_accept` →
    concretize the suggestion), freezes the nettable set into `cycle_obligations`
    (immutable), `→ LOCKED`, opens the next cycle. Obligations in a locked cycle
    reject disposition edits.
- **Read endpoints:** `GET /cycles` (all), `GET /cycles/current` (the earliest
  not-closed cycle — drives the Netting timeline).
- **Suggestion math reads the live cycle:** long-dated = due beyond the
  obligation's cycle's processing cut-off (so Levant, due 27 Aug, defers against
  the 30 Jun cut-off). No more hardcoded cut-off constant.
- **Frontend (light):** the Netting progress bar/meta shows the staged
  milestones (Opened · Upload cut-off · Processing cut-off · Settle), the current
  phase chip, and an operator button (Close uploads / Lock). Net amounts + the
  reveal stay hardcoded (Phase 7).

`app/cycles.py` also now owns the disposition/suggestion/match-state/nettable
helpers (they depend on the live cycle horizon); `main.py` delegates to it.

Scope held: **staged deadlines + state machine + the two transitions only** — no
net positions / statement / reveal (Phase 7).

## Forward-compat seams (Phase 6.5, additive)

Delos-shaping seams added without touching the netting/matching logic.

- **Jurisdiction** — `parties.jurisdiction` (ISO-3166 alpha-3), derived from
  `country` ([refdata.py](app/refdata.py) `iso3`). The matching/cycle engine stays
  **jurisdiction-blind** — nothing in `matching.py`/`cycles.py` reads it.
- **Currency exponents** — a `currencies` table (`code → minor_unit_exponent`,
  default 2, JPY 0, KWD/BHD 3). The display seam converts minor↔major by exponent
  ([refdata.py](app/refdata.py) `to_major`/`to_minor`), replacing the hardcoded
  /100. Ingest stores amounts in minor units via the currency exponent (so a JPY
  10,000 invoice stores/shows 10000, not 1,000,000).
- **VAT seam** — nullable `obligations.vat_treatment` / `vat_rate` /
  `vat_amount_minor`, populated from the upload if those columns are present.
- **Canonical invoices** ([canonical.py](app/canonical.py)) — when a match is
  **confirmed**, mint/upsert one `canonical_invoices` row (the clean single-record
  OUTPUT): biller/payer derived from the AR side's owner+direction (AR → owner =
  biller), agreed amount/currency/dates, jurisdictions from the parties. Idempotent
  (anchored on `ar_obligation_id`); re-confirm updates, never duplicates;
  mismatches (proposed) and one-sided rows do **not** mint; un-confirming removes.
  Kept out of `matching.py` so the engine stays country-blind. The two-sided
  obligations remain the reconciliation INPUT; this is the OUTPUT Phase 7 nets on.
- **Hash-chained audit** ([audit.py](app/audit.py)) — each `audit_log` entry
  carries `prev_hash` + `entry_hash` (`sha256(prev | canonical fields)`,
  Delos AuditEvent shape). Disposition changes, entity resolutions, match
  confirmations (canonical mint), and cycle transitions each append an event.
  `GET /audit/verify` recomputes the chain end-to-end (detects any tamper).

Scope held: **seams only** — no netting calc/statement/reveal (Phase 7), no
settlement legs / rules engine / rails / compliance (deferred).

## Netting calc + statement + reveal (Phase 7) — `app/netting.py`

The finale: multilateral netting over the **canonical invoices** in a locked
cycle's snapshot.

- **`default_on_no_action` is now real.** Uploads/seed arrive genuinely
  `pending`; the demo only seeds the *explicit* accept&net decisions (the
  signed-in party's matched in-cycle invoices + the counterparty mirrors). At
  lock, pending obligations follow the cycle's policy — `roll` → next cycle (do
  **not** net), `auto_accept` → accept&net.
- **Canonical amount + tolerance.** `gross_amount_minor` is the **biller's**
  (AR-side) agreed figure — the biller issues the invoice, so it's the source of
  truth; the payer's record is the reconciliation check. Tier-1 auto-confirm uses
  a small **fixed** minor-unit tolerance (`matching.TIER1_TOLERANCE_MINOR`, not
  1%); material differences drop to Tier-2 (Mismatch) and never auto-confirm,
  keeping `amount_delta` for audit.
- **Net positions** per party per currency: `receivable = Σ canonical where
  biller = party`, `payable = Σ where payer = party`, `net = receivable −
  payable`. Computed over the canonical invoices in the locked snapshot (or, for
  an open/reconciling cycle, a **provisional** view of the current nettable set).
  A **regenerable projection** — recomputed from the snapshot each call, never the
  source of truth. **Σ net = 0 per currency** by construction (one figure serves
  the biller's + and the payer's −), asserted in `compute()`.
- **Reconstruction key.** Each net figure carries the canonical-invoice ids
  behind it (`{receivable_invoice_ids, payable_invoice_ids}`). Every snapshot
  invoice appears in exactly one party's key per side, and the keys reconcile back
  to the net figures.
- **Compression** (per party): gross = invoices a party is in, net = distinct
  non-zero positions; the network aggregate sums across parties.
- **Estimated savings** = `(gross − net) × cost_per_payment` (network config),
  annualized `× cycles/year`, with a network-aggregate. `cost_per_payment` is
  adjustable and recomputes live in the UI.
- **Endpoints:** `GET /netting` (regenerable result; provisional until lock),
  `POST /cycles/{id}/net` (LOCKED → NETTED; persists `net_positions` as a
  projection/statement record), `GET /statement` (per-party, includes the
  reconstruction key).
- **Reveal wired to real data:** the Netting screen's net cards, compression
  (gross→net, payments eliminated, % reduction), before/after bars, estimated
  savings (live cost-per-payment, annualized) and the network aggregate all read
  computed values — nothing hardcoded.

Scope held: **netting calc + statement + reveal only** — no settlement / money
movement, no legs / hub ledger / rules engine / rails / compliance (deferred).

## Tests + runtime guards (Phase 8)

```bash
cd backend
.venv/bin/python -m pytest          # 40 tests; each runs on a fresh temp-DB seed
.venv/bin/python -m pytest tests/test_adversarial.py -s   # see the Part C report
```

- **Part A — regression suite** (`tests/`): the per-phase done-gates as automated
  tests — ingestion + idempotency, deterministic schema mapping (no API key),
  entity resolution + alias persistence + `on_network`, tiered matching +
  recompute + manual-match survival, disposition gate + nettable + durability,
  staged cycle deadlines + state machine + snapshot + `default_on_no_action`
  (roll vs auto_accept) + post-lock rejection, canonical minting, netting
  invariants (Σnet=0, regenerable, reconstruction keys), audit chain + tamper,
  currency exponent round-trip (JPY/BHD), **country-blindness** (asserts
  `matching.py`/`cycles.py`/`netting.py` contain no jurisdiction/country logic),
  and determinism. Each test runs against an isolated temp DB (the dev
  `netting.db` is never touched).
- **Part B — runtime guards** (`netting.check_invariants` + `_require_audit_ok`):
  a cycle that fails Σnet=0, whose reconstruction keys don't reconcile, or whose
  audit chain doesn't verify **refuses** to produce a netting result/statement or
  advance state (HTTP 409) — never emits a wrong result. (Σnet=0 and
  reconstruction hold by construction over canonical invoices, so they're
  code-regression tripwires unit-tested directly; the audit guard is exercised
  end-to-end at the endpoints.)
- **Part C — adversarial fixtures** (`tests/test_adversarial.py`): nasty inputs
  run through ingestion; asserts the hard bar (no crash / no 500) and prints a
  pass/break report. **Handled:** credit note (negative amount), missing required
  fields (flagged → `need_attention`), malformed CSV (BOM + quoted commas).

Scope held: **tests + guards + adversarial report only** — no new features
(Supabase auth is Phase 9).

## Pre-real-data hardening (Phase 8.5)

The Part C backlog is fixed, and the one data-breakable correctness guard added:

1. **In-batch duplicates flagged, never collapsed.** Two rows in one upload that
   hit the natural key are flagged (`needs attention` / `skipped_breakdown`,
   `duplicate_in_batch`) and not imported — never last-wins. The natural key now
   includes `counterparty_raw`, so a reused invoice number across *different*
   counterparties is legitimate (both import).
2. **Zero-amount imports.** The required-field check is `is None` / empty-string,
   not falsy — a legitimate `0.00` / fully-credited line now imports.
3. **Unknown currency rejected/flagged.** A currency code absent from the
   `currencies` table is flagged (`unknown_currency`) and not imported at the
   default exponent. `/upload` returns an `attention_breakdown`; confirm returns
   `skipped_breakdown`.
4. **Canonical-vs-sources guard (data-breakable).** `netting.compute` now blocks
   if any canonical invoice diverges from BOTH its source obligations (AR and AP)
   beyond the Tier-1 tolerance, or disagrees on currency — validating the net
   against the two-sided gross, alongside the audit + Σnet/reconstruction guards.

Adversarial report now shows **no gaps**. Scope held: small fixes only, no new
features.
