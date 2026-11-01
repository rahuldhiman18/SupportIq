"""Privacy controls for text sent to external AI providers.

Database lookups stay inside SupportIq.  This module removes direct
identifiers from the final prompt boundary so a future caller cannot
accidentally send them to Gemini by adding a new prompt field.
"""

import re


_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d ()-]{7,}\d)(?!\w)")
_CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_ORDER_RE = re.compile(r"\b#?[A-Z]{1,4}-?\d{4,8}\b", re.I)


def _redact_phone(match: re.Match[str]) -> str:
    # Do not mistake dates such as 2026-10-01 for phone numbers.
    return "[REDACTED_PHONE]" if len(re.sub(r"\D", "", match.group())) >= 10 else match.group()


def sanitize_for_ai(value: str | None) -> str | None:
    """Remove direct customer/order identifiers from provider-bound text."""
    if value is None:
        return None
    value = _EMAIL_RE.sub("[REDACTED_EMAIL]", value)
    value = _PHONE_RE.sub(_redact_phone, value)
    value = _CARD_RE.sub("[REDACTED_PAYMENT_NUMBER]", value)
    value = _ORDER_RE.sub("[ORDER_REFERENCE]", value)
    return value
