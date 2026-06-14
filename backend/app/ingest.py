"""Parsing (CSV/XLSX) + normalization of raw rows into canonical obligations.

Phase 2: no entity resolution and no matching. Rows land tagged to the
uploading party + NAP, with counterparty left as a raw name.
"""
import io
import re

import pandas as pd

# Canonical fields a column can map to (spec §6a). The counterparty_* signals
# (Phase 3) strengthen entity resolution; they are not stored on the obligation
# row directly — see extract_signals().
CANONICAL_FIELDS = [
    "invoice_number", "counterparty", "amount", "currency",
    "issue_date", "due_date", "direction", "status", "po_reference",
    "counterparty_tax_id", "counterparty_country", "counterparty_iban",
    "vat_treatment", "vat_rate", "vat_amount",
]

# Fields required to create a usable obligation (spec §14 minimum import set).
REQUIRED_FIELDS = [
    "invoice_number", "counterparty", "amount", "currency", "direction", "due_date",
]

_CCY_SYMBOLS = {"€": "EUR", "$": "USD", "£": "GBP", "₣": "CHF"}
_AR_WORDS = {"AR", "A/R", "RECEIVABLE", "RECEIVABLES", "SALES", "SALE",
             "INVOICE", "OUT", "OUTGOING", "DEBTOR"}
_AP_WORDS = {"AP", "A/P", "PAYABLE", "PAYABLES", "PURCHASE", "PURCHASES",
             "BILL", "IN", "INCOMING", "CREDITOR", "VENDOR"}


def parse_upload(filename, content):
    """Return (headers, rows) from a CSV or XLSX upload. Values are strings."""
    name = (filename or "").lower()
    buf = io.BytesIO(content)
    if name.endswith((".xlsx", ".xls")):
        df = pd.read_excel(buf, dtype=str)
    else:
        df = pd.read_csv(buf, dtype=str, keep_default_na=False, skipinitialspace=True)
    df = df.where(pd.notnull(df), None)
    headers = [str(c).strip() for c in df.columns]
    rows = []
    for rec in df.to_dict(orient="records"):
        rows.append({str(k).strip(): (None if v is None else str(v).strip())
                     for k, v in rec.items()})
    return headers, rows


def header_signature(headers):
    """Stable cache key for a file format: sorted, normalized header set."""
    return "|".join(sorted(h.strip().lower() for h in headers))


def _parse_amount(raw):
    """'1,840.50' / '€1.840' / 'USD 4,100' -> (major value, embedded ccy).

    Returns the major (decimal) value; the caller converts to minor units with
    the currency's exponent (so JPY/KWD etc. are correct, not assumed /100)."""
    if raw is None or str(raw).strip() == "":
        return None, None
    s = str(raw)
    ccy = None
    for sym, code in _CCY_SYMBOLS.items():
        if sym in s:
            ccy = code
    m = re.search(r"[A-Za-z]{3}", s)
    if m:
        ccy = ccy or m.group(0).upper()
    cleaned = re.sub(r"[^0-9.\-]", "", s.replace(",", ""))
    if cleaned in ("", "-", ".", "-."):
        return None, ccy
    try:
        return float(cleaned), ccy
    except ValueError:
        return None, ccy


def _parse_rate(raw):
    """'19' / '19%' / '0.19' -> float, or None."""
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return float(re.sub(r"[^0-9.\-]", "", str(raw)))
    except ValueError:
        return None


def _norm_currency(raw):
    if raw is None:
        return None
    s = str(raw).strip()
    if s in _CCY_SYMBOLS:
        return _CCY_SYMBOLS[s]
    s = s.upper()
    return s if re.fullmatch(r"[A-Z]{3}", s) else (s or None)


def _norm_direction(raw):
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if s in _AR_WORDS:
        return "AR"
    if s in _AP_WORDS:
        return "AP"
    return None


def _norm_date(raw):
    """Best-effort parse to ISO yyyy-mm-dd (what the prototype's render expects)."""
    if raw is None or str(raw).strip() == "":
        return None
    ts = pd.to_datetime(str(raw), errors="coerce", dayfirst=False)
    if ts is pd.NaT:
        ts = pd.to_datetime(str(raw), errors="coerce", dayfirst=True)
    return None if ts is pd.NaT else ts.strftime("%Y-%m-%d")


def normalize_row(raw, mapping):
    """Apply mapping -> canonical obligation dict. Returns (obligation, missing).

    `mapping` is {source_column: canonical_field}. `missing` lists required
    canonical fields that couldn't be resolved (the row "needs attention").
    """
    field_to_col = {}
    for col, field in mapping.items():
        field_to_col.setdefault(field, col)

    def val(field):
        col = field_to_col.get(field)
        return raw.get(col) if col else None

    amount, embedded_ccy = _parse_amount(val("amount"))
    currency = _norm_currency(val("currency")) or embedded_ccy

    obligation = {
        "invoice_number": (val("invoice_number") or None),
        "counterparty_raw": (val("counterparty") or None),
        "amount": amount,                       # MAJOR value; caller converts to minor
        "currency": currency,
        "issue_date": _norm_date(val("issue_date")),
        "due_date": _norm_date(val("due_date")),
        "direction": _norm_direction(val("direction")),
        "status_source": (val("status") or None),
        "po_reference": (val("po_reference") or None),
        # VAT seam — populated only if the upload carries these columns.
        "vat_treatment": (val("vat_treatment") or None),
        "vat_rate": _parse_rate(val("vat_rate")),
        "vat_amount": _parse_amount(val("vat_amount"))[0],   # MAJOR value or None
    }

    present = {
        "invoice_number": obligation["invoice_number"],
        "counterparty": obligation["counterparty_raw"],
        "amount": obligation["amount"],
        "currency": obligation["currency"],
        "direction": obligation["direction"],
        "due_date": obligation["due_date"],
    }
    # Missing = None or empty string. NOT falsy — a legitimate 0.00 / fully-
    # credited amount must import, not be dropped.
    missing = [f for f in REQUIRED_FIELDS if present[f] is None or present[f] == ""]
    return obligation, missing


def inbatch_duplicate_rows(rows, mapping):
    """Row indices that collide on the natural key WITHIN one upload —
    (invoice_number, direction, counterparty), case-insensitive. These must be
    flagged for the user to resolve, never silently collapsed by the upsert.
    A reused invoice number across *different* counterparties is not a dup."""
    from collections import Counter

    keys = []
    for raw in rows:
        ob, _ = normalize_row(raw, mapping)
        inv = (ob["invoice_number"] or "").strip().lower()
        cp = (ob["counterparty_raw"] or "").strip().lower()
        direction = ob["direction"] or ""
        keys.append((inv, direction, cp) if inv and direction and cp else None)
    dup = {k for k, n in Counter(k for k in keys if k).items() if n > 1}
    return [i for i, k in enumerate(keys) if k in dup]


def extract_signals(raw, mapping):
    """Entity-resolution signals for a row's counterparty (spec §6b):
    tax_id / country / IBAN, as written. None when not present."""
    field_to_col = {}
    for col, field in mapping.items():
        field_to_col.setdefault(field, col)

    def val(field):
        col = field_to_col.get(field)
        v = raw.get(col) if col else None
        return v.strip() if isinstance(v, str) and v.strip() else None

    return {
        "tax_id": val("counterparty_tax_id"),
        "country": val("counterparty_country"),
        "iban": val("counterparty_iban"),
    }
