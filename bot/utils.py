# utils.py
"""
Utility helpers used by handlers.

Fungsi-fungsi:
- sanitize_input(text) -> str
- safe_int(value, default=0) -> int
- build_name_from_spec(category, specs) -> str
- make_txn_id(prefix="TXN") -> str
- parse_bool_like(value) -> bool
- ensure_str(value) -> str
"""

from __future__ import annotations
import re
import random
import string
from datetime import datetime
from typing import Any, Optional

# -------------------------
# Input cleaning / parsing
# -------------------------
def sanitize_input(text: Optional[str]) -> str:
    """Strip whitespace and normalize spaces. Return empty string for None."""
    if text is None:
        return ""
    # normalize CR/LF to single space, strip outer whitespace, collapse multiple spaces
    s = str(text).replace("\r", " ").replace("\n", " ").strip()
    s = re.sub(r"\s+", " ", s)
    return s

def safe_int(value: Any, default: int = 0) -> int:
    """
    Safely parse integer-like values.
    Accepts floats in string form, numeric strings, etc. Returns `default` on failure.
    """
    if value is None:
        return default
    try:
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        s = str(value).strip()
        if s == "":
            return default
        # remove thousand separators commonly used
        s = s.replace(",", "")
        # allow floats but cast to int
        if "." in s:
            return int(float(s))
        return int(s)
    except Exception:
        try:
            return int(float(str(value)))
        except Exception:
            return default

def ensure_str(value: Any) -> str:
    """Return string representation, safe for None."""
    if value is None:
        return ""
    return str(value)

def parse_bool_like(value: Any) -> bool:
    """Parse common truthy/falsy strings to boolean."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "y", "on", "t"):
        return True
    return False

# -------------------------
# Naming & IDs
# -------------------------
def build_name_from_spec(category: str, specs: Optional[str]) -> str:
    """
    Build canonical item name from category + specs.
    Rules:
      - If category is Custom, use specs (trimmed).
      - Otherwise combine: "<Category> <specs>" trimmed.
      - Collapse multiple spaces and remove leading/trailing separators.
    """
    cat = (category or "").strip()
    s = (specs or "").strip()
    if not cat:
        name = s or ""
    elif cat.lower() == "custom":
        name = s or cat
    else:
        if s:
            name = f"{cat} {s}"
        else:
            name = cat
    # Normalize spacing and separators
    name = re.sub(r"\s+", " ", name).strip()
    # Remove any leading/trailing punctuation leftover
    name = re.sub(r"^[\s\-\:\|]+", "", name)
    name = re.sub(r"[\s\-\:\|]+$", "", name)
    return name

def make_txn_id(prefix: str = "TXN") -> str:
    """
    Create a short unique transaction id:
      <PREFIX>-YYYYMMDD-HHMMSS-XXXX
    where XXXX is random alphanumeric 4 chars.
    """
    ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    rnd = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"{prefix}-{ts}-{rnd}"

# -------------------------
# Small helpers for validating dates (if ever needed)
# -------------------------
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

def looks_like_iso_date(s: Optional[str]) -> bool:
    if not s:
        return False
    return bool(_DATE_RE.match(s.strip()))

# -------------------------
# Optional convenience: convert user display name
# -------------------------
def display_name_from_user(user: Any) -> str:
    """
    Return a best-effort display name from telegram.user object or dict.
    """
    if user is None:
        return ""
    if hasattr(user, "full_name") and user.full_name:
        return user.full_name
    if hasattr(user, "first_name") and user.first_name:
        return user.first_name
    if isinstance(user, dict):
        return user.get("full_name") or user.get("first_name") or ""
    return str(user)

# -------------------------
# Exports
# -------------------------
__all__ = [
    "sanitize_input",
    "safe_int",
    "build_name_from_spec",
    "make_txn_id",
    "ensure_str",
    "parse_bool_like",
    "looks_like_iso_date",
    "display_name_from_user",
]
