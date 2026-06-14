# Netting MVP — Build Spec (v2)

A build brief for the AP/AR reconciliation + multilateral netting MVP, to hand directly to Claude Code. Build approach is **frontend-first / contract-first**: the prototype UI defines the data contracts, then the backend is built to satisfy them.

---

## 1. Scope

**What this MVP is:** A reconciliation engine that ingests participants' AP/AR ledgers, normalizes heterogeneous data, matches obligations across counterparties, lets participants dispose of their own invoices (accept & net / accept & settle-direct / defer / dispute), groups mutually-netted obligations into network-defined cycles, and produces a **netting statement** per participant per cycle — including a compression reveal and an estimated-savings figure.

**What this MVP is NOT:**
- No money movement — the clearing point is a *calculation and reconciliation checkpoint*, not a settlement.
- No credit, insurance, default fund, or stablecoin settlement rail.
- No FX consolidation (net per-currency only).
- No cross-network netting (tag obligations with a network; net only within one).
- No production-grade auth/security hardening.

**Starting conditions:** One network (NAP). Intra-group (sister entities) and cross-party (independent firms) use the **same engine** — the difference is a trust/permissions flag, not different logic.

---

## 2. Build approach

- **Frontend-first / contract-first.** Build the prototype UI on mock data first. Its real deliverable is the set of data shapes and actions the backend must produce. It also doubles as the demo.
- **Mock the messy states, not the happy path.** The mock dataset must contain a mismatch, a one-sided invoice, a dispute, and a multi-currency cycle — so the contracts account for the engine's real, probabilistic output.
- **Build the backend against real, messy files from hour one** (Craft exports, NAP member data, or FHF invoices as a stand-in). The last 20% of matches (partial payments, credit notes, FX rounding, consolidated invoices, missing keys) is where real data punishes you.
- **One phase per Claude Code session**, each with a done-gate, verified against the fixtures before moving on. Commit after every green phase.

---

## 3. Domain glossary

- **Party** — a legal entity that uploads a ledger. Sister companies are distinct parties (optionally sharing `group_id`). A party is **on-network** (also uploads, so both sides can be matched and netted) or **off-network** (just a name on someone's ledger; one-sided, not nettable).
- **Network / ruleset** — a closed group (NAP) whose operator sets cycle rules. Obligations are tagged to a network.
- **Obligation** — one normalized invoice row from one party's ledger, directional: `AR` (owed to them) or `AP` (they owe).
- **Match** — a confirmed link between an AR obligation and its mirror AP obligation on the counterparty's ledger.
- **Disposition** — a party's decision about its own obligation: accept (with settlement mode `net` or `direct`), dispute, or defer.
- **Nettable** — a *derived* state: a confirmed match where **both sides chose accept & net** and neither disputed.
- **Cycle** — a window of obligations that freezes at cut-off, gets netted, and produces statements.

---

## 4. Data model

SQLite for MVP. Key tables and the fields that matter:

### networks
| field | notes |
|---|---|
| network_id (pk) | |
| name | "NAP" |
| cycle_length_days | e.g. 30 |
| cut_off_rule | how cut-off date is derived |
| settlement_lag_days | net date → settlement date (modelled, unused in MVP) |
| term_mode | `respect_due_date` \| `standardize_to_cycle` |
| default_on_no_action | `roll` \| `auto_accept` (default `roll`) |
| cost_per_payment | blended assumed cost, for savings calc |
| cost_components | json, optional breakdown: bank_fee / fx_cost / admin_cost |

### parties
| field | notes |
|---|---|
| party_id (pk) | |
| legal_name | |
| tax_id | strong entity-resolution key |
| country | |
| group_id (null) | sister-entity grouping |
| on_network (bool) | true = also uploads, so nettable |

(`party_networks` join table for membership.)

### source_formats — cached schema mappings
`format_id, party_id, source_label, column_mapping(json)`

### counterparty_aliases — entity resolution
`alias_id, raw_name, resolved_party_id(null), signals(json: tax_id/IBAN/email/address), match_confidence, match_status(unresolved|proposed|confirmed)`

### obligations — core table
| field | notes |
|---|---|
| obligation_id (pk) | |
| owner_party_id | whose ledger this came from |
| direction | `AR` \| `AP` |
| counterparty_raw / counterparty_party_id | as-written / resolved |
| network_id | |
| invoice_number | primary join key |
| amount / currency | |
| issue_date | shown on row, context |
| due_date | drives cycle assignment + suggested disposition |
| status_source | open / paid / partial (from source) |
| po_reference (null) | secondary join key |
| source_format_id / upload_batch_id | |
| ingest_state | `ingested` \| `matched` \| `unmatched` |
| disposition | `pending` \| `accepted` \| `disputed` \| `deferred` |
| settlement_mode | `net` \| `direct` (meaningful when `accepted`) |
| assigned_cycle_id (null) | |
| raw_row (json) | original, for audit |

### matches
`match_id, ar_obligation_id, ap_obligation_id, match_tier(exact|fuzzy|manual), match_confidence, amount_delta, match_status(proposed|confirmed|rejected)`

### cycles
`cycle_id, network_id, sequence_no, start_date, cut_off_at, settlement_date(computed), state(open|locked|netted|closed)`

### cycle_obligations — immutable snapshot at lock
`cycle_id, obligation_id, frozen_amount, frozen_currency`

### net_positions — netting output
| field | notes |
|---|---|
| cycle_id / party_id / currency | per-currency |
| gross_payable / gross_receivable | |
| net_amount | sign = payer/receiver |
| gross_payment_count / net_payment_count | for compression reveal |
| estimated_savings | (gross_count − net_count) × cost_per_payment |

### audit_log — append-only
Every state transition, match confirmation, disposition action, entity resolution: `(actor, action, entity_ref, before, after, timestamp)`. The trust backbone.

---

## 5. Ingestion

Primary path: **CSV / XLSX upload** (pandas / openpyxl). No integrations in MVP. Each upload creates an `upload_batch`, parses rows, stores `raw_row`, triggers processing (schema-map → entity-resolve → match). (Later: unified accounting API, CargoWise export, email/PDF parsing.)

---

## 6. Normalization & matching — three distinct layers

Three different problems, three different tools. Do not collapse into one "let the AI figure it out" step.

**6a. Schema mapping (LLM-assisted, cached).** Send Claude headers + 5–10 sample rows → proposed `source_col → canonical_field` mapping. Human confirms once, cache in `source_formats`. Canonical fields: `invoice_number, counterparty, amount, currency, issue_date, due_date, direction, status, po_reference`.

**6b. Entity resolution (layered, human-confirmed).** (1) Deterministic — exact tax_id/VAT/registration/IBAN → auto-confirm. (2) Fuzzy — normalized-name similarity (rapidfuzz) + corroborating signal → proposed. (3) LLM — ambiguous clusters only, proposes likely-same-party → human confirms. Confirmations stored in `counterparty_aliases`, never re-asked.

**6c. Transaction matching (tiered).** Tier 1 exact/auto-confirm: invoice_number + amount-within-tolerance + same currency + resolved pair → `confirmed`. Tier 2 probable/review: strong partial or amount_delta beyond tolerance → `proposed`, to review queue. Tier 3 exception: no confident match → `unmatched` (one-sided).

**Principle:** AI proposes, deterministic rules handle the bulk, a human confirms anything financial. Only Tier-1 auto-confirms. No LLM-decided match silently enters a net.

---

## 7. Disposition model — the commitment ladder

A party's disposition of its **own** obligation. Four choices, ordered by commitment:

| Disposition | Meaning | Underlying state |
|---|---|---|
| **Accept & net** | Agreed, and committed to *this* cycle's netting set | `accepted` + `settlement_mode=net` |
| **Accept, settle direct** | Agreed and reconciled, but excluded from netting; settled outside the network on its own terms | `accepted` + `settlement_mode=direct` |
| **Defer** | Intends to net, just not this cycle | `deferred` |
| **Dispute** | Not agreed; excluded, routed to resolution | `disputed` |

Rules:
- **Upload implies acceptance of your own side.** An uploaded `AP` row defaults to `accepted` (settlement_mode per the suggested disposition). Never make a user click "accept" on data they just uploaded.
- **Suggested disposition** is computed from `due_date` (due this cycle → suggest accept & net; long-dated → suggest defer). The user confirms or overrides. **Keep the default suggestion as "accept & net"** so "settle direct" is a deliberate opt-out, not the path of least resistance — otherwise netting density erodes.
- **"Settle direct" is a soft opt-out that preserves agreement** — distinct from dispute (which kills agreement). A settle-direct obligation still counts as a reconciled, mutually-agreed balance; it just doesn't enter the cycle. This is what lets a cautious participant use the platform as reconciliation-only and opt into netting invoice-by-invoice.

---

## 8. Nettable — derived, not a button

An obligation pair is **nettable for cycle C** iff:
1. a `match` exists with `match_status = confirmed`, AND
2. **both** owners chose **accept & net** (`settlement_mode = net`) targeting cycle C, AND
3. neither side disputed.

When all hold, both obligations get `assigned_cycle_id = C` and enter the snapshot at lock.

Conflict resolution:
- Either side **disputes** → excluded (contested).
- Either side chooses **settle direct** → agreed but not netted (soft opt-out).
- Either side **defers**, or hasn't acted by cut-off → rolls (an obligation can't be half-in a cycle).

**This self-gates the rollout:** you can only net where both sides are on-network and both chose to net — intra-group first, then cross-party as pairs come on.

---

## 9. Cycle engine

Per-network config (cycle length, cut-off, `term_mode`, `default_on_no_action`).

`term_mode` is the rule-setter lever: `respect_due_date` (on-ramp — cycle schedules settlement of what's *due*, bilateral terms respected, due date drives the suggestion) vs `standardize_to_cycle` (destination — membership means all intra-network obligations clear on the cycle rhythm). Switchable per network as trust matures.

State machine:
```
OPEN (accumulate obligations + dispositions)
  → [at cut_off_at] LOCK: snapshot nettable set into cycle_obligations (IMMUTABLE)
  → NETTED: run netting calc, write net_positions
  → statement generated
  → CLOSED
```
Snapshot is immutable; post-lock corrections go to the next cycle or a dispute path. **MVP stops at NETTED + statement. No settlement.**

---

## 10. Netting calculation, statement & the reveal

On the frozen set, per network, per cycle, **per currency**:

`net_amount(party) = Σ(accept&net AR to them) − Σ(accept&net AP they owe)`. Net receivers vs payers; positions sum to zero per currency.

**The reveal (the demo money-shot):**
- **Compression:** `gross_payment_count → net_payment_count`, e.g. "47 invoices · 9 counterparties · €620k gross → 3 net payments · €88k." Headline metric = *payments eliminated* and *% reduction* (more visceral than the euro figure).
- **Estimated savings:** `(gross_count − net_count) × cost_per_payment`. Show the assumption and the math (e.g. "44 avoided × €35 = €1,540"); make `cost_per_payment` adjustable and decomposed (bank fee + FX + admin). **Annualize** it (×cycles/year). Show a **network-aggregate** figure as well as the participant's own.
- Keep payment-cost savings only for now — FX-spread and float/working-capital savings are larger, separate levers for later. Don't fold them in yet.

Statement (per party per cycle): gross AR/AP, invoice count, net position per currency, included obligations, compression, estimated savings, projected settle date. Read-only + export in MVP.

---

## 11. Frontend — four screens (the contracts)

Sidemenu navigation. For each screen: data in / actions out. Layout is the builder's; keep the contracts.

### Partners ("partners") — ERP-like counterparty view
- **Shows:** per partner — basic trading info, transaction summary, current **balance per currency**, and an **on-network / off-network** flag. Balance is qualified: *agreed* (matched both sides) for on-network partners, *claimed* (own ledger only) for off-network.
- **In:** list of resolved partners with aggregates. **Actions:** view partner detail; manage/merge entities.

### Upload ("upload") — ingest AP/AR
- **Flow:** upload box (CSV/XLSX) → processing (parse → schema-map → entity-resolve → match) → **summary screen**.
- **The summary is an *ingestion* confirmation, NOT disposition.** It confirms schema mapping (if new format), resolves *new* counterparties, and surfaces exceptions: "300 rows in, 280 auto-processed, 20 need attention." Confirm-all or handle individually.
- **Disposition does NOT happen here** — it happens in Accounts, after matching.
- **In:** parsed batch, proposed mapping, proposed entity resolutions, exception list. **Actions:** confirm mapping, confirm/merge entities, confirm batch.

### Accounts ("Accounts") — disposition view / dashboard
- **Top:** core metrics — gross AR/AP, # pending disposition, # needing attention, preview of net-this-cycle.
- **Table rows:** issue date, due date, counterparty, **direction (AR/AP)**, amount, currency, **match status**, and the **disposition control** (accept & net / accept & settle-direct / defer / dispute) with the **suggested disposition pre-highlighted**.
- **Match status gates the disposition** — you can't accept an unmatched/mismatched invoice into a cycle. Also surface whether the **counterparty has also chosen accept & net** (i.e. the derived nettable state).
- **"Needs attention" filter** is the home for exceptions (mismatches, unmatched) — no separate screen.
- **In:** obligations with state. **Actions:** per-row disposition + bulk.

### Netting ("Netting") — cycle + the reveal
- **Shows:** cycle **progress bar**, **running net amount** (provisional until cut-off) per currency and directional (pay/receive), **projected settle date**, and the **reveal** — compression (gross→net count) + *payments eliminated / % reduction* + **estimated savings** (adjustable cost assumption, annualized, plus network aggregate).
- Optional: a simple before→after visual (tangle of payment arrows collapsing to a few lines).
- **In:** current cycle state, net positions, compression + savings. **Actions:** adjust cost assumption; view/export statement (read-only MVP).

---

## 12. Tech stack

Backend: Python (FastAPI). Ingest: pandas / openpyxl. Store: SQLite (MVP) → Postgres. Fuzzy: rapidfuzz. AI: Anthropic API for schema mapping + ambiguous entity/match proposals only. Frontend: builder's design (Phase-1 prototype can be a single file).

---

## 13. Build sequence (each phase = one Claude Code session with a done-gate)

0. **Scaffold + shared types + messy mock dataset.** Canonical shapes + fixtures (matched pair, mismatch, one-sided, dispute, multi-currency). *Done:* types compile, mock loads.
1. **Frontend prototype on mock data** (sub-prompts): 1a Accounts/disposition (core), 1b Netting + reveal/savings, 1c Upload summary + exceptions + entity review, 1d Partners. *Done:* clickable, every messy state renders, actions log payloads. Finalizes the contracts; is the demo.
2. **Ingestion + schema mapping.** Upload → parse → LLM mapping (cached) → canonical obligations. *Done:* real messy file lands as normalized obligations.
3. **Entity resolution.** Deterministic + fuzzy + LLM, with review surfacing to the Upload summary. *Done:* two files yield confirmed + proposed parties; `on_network` set correctly.
4. **Transaction matching.** Tiered, feeding the Accounts match status + "needs attention". *Done:* two ledgers → confirmed matches, flagged mismatches, one-sided unmatched. **Budget the most iteration here.**
5. **Disposition + nettable.** Wire the four-disposition ladder + settlement_mode; derive nettable from both-sides-accept&net. *Done:* dispositions change state correctly; nettable derives right.
6. **Cycle engine.** Per-network config, open → lock → immutable snapshot. *Done:* locked cycle freezes its set, rejects edits.
7. **Netting calc + statement + reveal.** Multilateral net per party per currency; compression; estimated savings (+ aggregate + annualized). *Done:* fixtures produce a correct statement with the headline gross→net figure and savings.
8. **Swap mock for real.** Point the Phase-1 frontend at real endpoints. *Done:* upload → reconcile → dispose → cycle → statement runs end-to-end through the real UI. Mostly wiring, because contracts were fixed in Phase 0/1.

---

## 14. Minimum import fields

Matching quality is capped by a stable join key. Require per row: `invoice_number` (or usable reference), `amount`, `currency`, `counterparty name`, `direction` (AR/AP), `due_date`. A counterparty `tax_id` massively strengthens entity resolution. Be explicit with pilot customers about this minimum.
