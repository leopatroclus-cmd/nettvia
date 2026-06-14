"""Schema mapping (spec §6a): propose source_column -> canonical_field.

Primary path: the Anthropic API (Claude) given the file's headers + a few
sample rows. Falls back to a deterministic header heuristic when no API key is
configured or the call fails, so the flow is demonstrable either way.
"""
import json
import os
import re

from .ingest import CANONICAL_FIELDS

# Schema mapping is a simple, high-volume extraction task → use the small model.
MODEL = "claude-haiku-4-5"

_MAPPING_SCHEMA = {
    "type": "object",
    "properties": {
        "mappings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source_column": {"type": "string"},
                    "canonical_field": {"type": "string", "enum": CANONICAL_FIELDS},
                },
                "required": ["source_column", "canonical_field"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["mappings"],
    "additionalProperties": False,
}


def propose_mapping(headers, sample_rows):
    """Return (mapping, method) where mapping is {source_column: canonical_field}
    for the columns confidently mapped, and method is 'llm' or 'heuristic'."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return _llm_mapping(headers, sample_rows), "llm"
        except Exception as e:  # pragma: no cover - network/SDK variance
            print("schema-mapping: LLM path failed, using heuristic:", e)
    return _heuristic_mapping(headers), "heuristic"


def _llm_mapping(headers, sample_rows):
    import anthropic

    client = anthropic.Anthropic()
    preview = sample_rows[:8]
    prompt = (
        "You map messy AP/AR ledger column headers to a fixed set of canonical "
        "fields for a reconciliation engine.\n\n"
        f"Canonical fields: {', '.join(CANONICAL_FIELDS)}.\n\n"
        f"File headers: {json.dumps(headers)}\n\n"
        f"Sample rows (up to 8):\n{json.dumps(preview, indent=2, default=str)}\n\n"
        "Return one mapping entry per source column that clearly corresponds to a "
        "canonical field. Omit columns that don't map. Each canonical field should "
        "be used at most once. Use the header name AND the sample values to decide "
        "(e.g. a column of AR/AP or Sales/Purchase values is 'direction')."
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        output_config={"format": {"type": "json_schema", "schema": _MAPPING_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    text = next(b.text for b in resp.content if b.type == "text")
    data = json.loads(text)
    header_set = set(headers)
    mapping = {}
    for m in data.get("mappings", []):
        col, field = m.get("source_column"), m.get("canonical_field")
        if col in header_set and field in CANONICAL_FIELDS and field not in mapping.values():
            mapping[col] = field
    return mapping


# --- deterministic fallback ---------------------------------------------------

def _norm(h):
    return re.sub(r"[^a-z0-9]", "", h.lower())


# (canonical_field, [substrings]) — order matters: earlier wins a column, and
# more-specific fields are checked before generic ones (due before date,
# tax_id/country before counterparty, etc.).
_RULES = [
    # VAT columns before counterparty_tax_id so "VAT Amount"/"VAT Rate" aren't
    # grabbed by the broad "vat" tax-id needle.
    ("vat_amount", ["vatamount", "taxamount", "vatvalue", "vatsum"]),
    ("vat_rate", ["vatrate", "taxrate", "vatpct", "vatpercent", "vatperc"]),
    ("vat_treatment", ["vattreatment", "taxtreatment", "vatcategory", "reversecharge", "vatcode"]),
    ("counterparty_tax_id", ["taxid", "taxno", "taxnumber", "vatid", "vatno",
                             "vatnumber", "vat", "registrationno", "regno",
                             "companyno", "companynumber", "fiscalcode", "uid", "ein"]),
    ("counterparty_iban", ["iban"]),
    ("counterparty_country", ["country", "countrycode", "nation"]),
    ("po_reference", ["ponumber", "poref", "purchaseorder", "ponum"]),
    ("due_date", ["duedate", "due", "maturity", "payby", "paymentdue"]),
    ("issue_date", ["issuedate", "invoicedate", "docdate", "documentdate", "date", "issued"]),
    ("invoice_number", ["invoicenumber", "invoiceno", "invoicenum", "docnumber",
                        "docno", "documentno", "invoice", "reference", "ref"]),
    ("currency", ["currency", "ccy", "curr"]),
    ("amount", ["amount", "amt", "gross", "nett", "net", "value", "total", "sum"]),
    ("direction", ["direction", "aprar", "drcr", "type", "ledger", "dir"]),
    ("counterparty", ["counterparty", "tradingpartner", "partner", "customer",
                      "vendor", "supplier", "client", "name", "account"]),
    ("status", ["status", "state", "paid"]),
]


def _heuristic_mapping(headers):
    mapping = {}
    used = set()
    for field, needles in _RULES:
        if field in used:
            continue
        for h in headers:
            if h in mapping:
                continue
            n = _norm(h)
            if any(needle in n for needle in needles):
                mapping[h] = field
                used.add(field)
                break
    return mapping
