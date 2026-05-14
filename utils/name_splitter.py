"""
utils/name_splitter.py — Splits an owner_name string into first and last name.

Handles the three formats found in Ohio county data:
    "Last, First [Middle]"     → ("First", "Last")
    "FIRST [MIDDLE] LAST"      → ("FIRST", "LAST")
    Entity / multi-owner names → ("", "")  — do not attempt to split

Returns ("", "") for any name that looks like a business entity, trust,
multi-owner string, or single-word entry that cannot be reliably split.
"""

import re

# Markers that indicate a business entity rather than a person name.
# Checked as whole words (surrounded by word boundaries).
_ENTITY_MARKERS = re.compile(
    r"\b(LLC|INC|CORP|CO|COMPANY|CHURCH|TRUST|TRUSTEES|ESTATE|FOUNDATION|"
    r"ASSOCIATION|ASSOC|PROPERTIES|REALTY|INVESTMENTS|GROUP|"
    r"PARTNERS|PARTNERSHIP|LP|LLP|PLC|LTD|DBA|HOLDINGS|ENTERPRISES|"
    r"SERVICES|MANAGEMENT|DEVELOPMENT|SOLUTIONS|BANK|FSB|TOWNSHIP|"
    r"RESTORATION|COUNTY|MUNICIPALITY|CITY|VILLAGE|BOARD)\b",
    re.IGNORECASE,
)

# Ampersand, " AND ", or " % " between words signals multiple owners — don't split.
_MULTI_OWNER = re.compile(r"\s+AND\s+|\s*&\s*|\s*%\s*", re.IGNORECASE)

# Suffixes to strip before splitting so they don't end up as the "last name".
_SUFFIX_RE = re.compile(
    r",?\s*(JR\.?|SR\.?|II|III|IV|V|ESQ\.?)$",
    re.IGNORECASE,
)

# Legal noise to strip from the end of a name.
_LEGAL_NOISE = re.compile(
    r"\s*,?\s*(ET\.?\s*AL\.?|ETAL|AKA|A/K/A|FKA|F/K/A|DECEASED|DECD\.?|"
    r"SUCC TRS|SUCC\.? TRUSTEE|ADMINISTRATOR|EXECUTOR|FIDUCIAR\w*).*$",
    re.IGNORECASE,
)

# Sources where the county stores names as "LAST FIRST [MIDDLE]" without a comma.
_LAST_FIRST_SOURCES = {"tax_lien"}


def split_owner_name(name: str, source_type: str = "") -> tuple[str, str]:
    """Return (first_name, last_name) from a raw owner_name string.

    Returns ("", "") when the name cannot be reliably split (entities,
    multi-owner strings, single tokens, or ambiguous single-letter tokens).

    Args:
        name:        Raw owner_name value from the database.
        source_type: The lead's source_type (e.g. "tax_lien"). Used to select
                     the correct name order — county assessors store names as
                     "LAST FIRST" while courts store them as "First Last".

    Returns:
        Tuple of (first_name, last_name), both stripped strings.
        Either or both may be empty if splitting is not possible.
    """
    if not name:
        return "", ""

    # Strip outer whitespace and quotes introduced by court scrapers.
    cleaned = name.strip().strip("'\"")

    # Entity detection — skip business names entirely.
    if _ENTITY_MARKERS.search(cleaned):
        return "", ""

    # Multiple owners — skip (we store the full string in owner_name).
    if _MULTI_OWNER.search(cleaned):
        return "", ""

    # Strip legal noise from the end ("ET AL", "ETAL", "AKA ...", etc.)
    cleaned = _LEGAL_NOISE.sub("", cleaned).strip().rstrip(",").strip()

    # Strip name suffixes (JR, SR, III) so they don't become the last name.
    cleaned = _SUFFIX_RE.sub("", cleaned).strip()

    if not cleaned:
        return "", ""

    # --- Format 1: "Last, First [Middle]" (comma present — unambiguous) ---
    if "," in cleaned:
        parts = cleaned.split(",", 1)
        last = parts[0].strip()
        rest = parts[1].strip()
        first = rest.split()[0] if rest else ""
        if last and first and len(first) > 1:  # reject single-letter initials as first
            return _title(first), _title(last)

    # --- Format 2: space-separated (order depends on source) ---
    tokens = [t for t in cleaned.split() if len(t) > 1]  # drop single-letter initials
    if not tokens:
        return "", ""

    # County assessor records (tax_lien): stored as "LAST FIRST [MIDDLE]"
    if source_type in _LAST_FIRST_SOURCES:
        if len(tokens) >= 2:
            return _title(tokens[1]), _title(tokens[0])  # swap: first=tokens[1], last=tokens[0]
        return "", _title(tokens[0])

    # Court / probate records: stored as "FIRST [MIDDLE] LAST"
    if len(tokens) == 2:
        return _title(tokens[0]), _title(tokens[1])
    if len(tokens) >= 3:
        return _title(tokens[0]), _title(tokens[-1])
    if len(tokens) == 1:
        return "", _title(tokens[0])

    return "", ""


def _title(s: str) -> str:
    """Title-case a name token, preserving Mc/Mac prefixes."""
    if not s:
        return s
    s = s.strip(".,")
    # Already mixed-case (not all-caps or all-lower) — leave as-is.
    if not (s.isupper() or s.islower()):
        return s
    return s.capitalize()


def split_into_fields(name: str, source_type: str = "") -> dict:
    """Return a dict with owner_first_name and owner_last_name.

    Convenience wrapper for use in record dicts:
        record = {"owner_name": name, **split_into_fields(name, source_type), ...}
    """
    first, last = split_owner_name(name, source_type)
    return {"owner_first_name": first or None, "owner_last_name": last or None}
