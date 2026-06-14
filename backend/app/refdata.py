"""Reference data seams (Phase 6.5): currency minor-unit exponents + jurisdiction.

Currency amounts are stored in minor units; the exponent (per currency) governs
the major<->minor conversion at the display/ingest seam — replacing the old
hardcoded /100 (which was wrong for JPY/KWD/etc.).
"""
DEFAULT_EXPONENT = 2

# Seeded into the currencies table.
CURRENCIES = [
    ("EUR", 2), ("USD", 2), ("CHF", 2), ("GBP", 2), ("SEK", 2), ("NOK", 2),
    ("JPY", 0),               # zero-decimal
    ("BHD", 3), ("KWD", 3),   # three-decimal
]


def exponent(conn, code):
    if not code:
        return DEFAULT_EXPONENT
    r = conn.execute(
        "SELECT minor_unit_exponent FROM currencies WHERE code = ?", (code,)
    ).fetchone()
    return r["minor_unit_exponent"] if r else DEFAULT_EXPONENT


def to_minor(value, exp):
    """Major value -> integer minor units (e.g. 1840.50 EUR -> 184050; 10000 JPY -> 10000)."""
    if value is None:
        return None
    return int(round(value * (10 ** exp)))


def to_major(minor, exp):
    """Minor units -> the whole/decimal value to display."""
    if minor is None:
        return 0
    if exp <= 0:
        return int(minor)
    q = 10 ** exp
    return int(minor // q) if minor % q == 0 else round(minor / q, exp)


# ISO-3166 alpha-2 -> alpha-3, for the jurisdiction seam (derived from country).
ISO2_TO_ISO3 = {
    "GR": "GRC", "DE": "DEU", "SG": "SGP", "LB": "LBN", "CH": "CHE",
    "EG": "EGY", "HR": "HRV", "FR": "FRA", "EE": "EST", "GB": "GBR",
    "US": "USA", "JP": "JPN", "NL": "NLD", "IT": "ITA", "ES": "ESP",
    "SE": "SWE", "NO": "NOR", "PL": "POL", "AT": "AUT", "BE": "BEL",
}


def iso3(country2):
    if not country2:
        return None
    return ISO2_TO_ISO3.get(country2.upper(), country2.upper())
