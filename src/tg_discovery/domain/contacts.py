"""Checks on the contact details of an event's organiser: only an organisation's own contacts may be kept.

Personal data of private individuals is never stored, and an organiser is often a person (a drag artist, a DJ). So an
email address is kept only when it is a shared mailbox (``info@``, ``booking@``), never a named one (``jane.doe@``), and
both an address and a phone number must appear on a page the record cites.
"""

import re

_EMAIL = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
# Shared mailboxes of an organisation: the first word of the local part ("info", "booking.hcm", "events-team").
_ROLE_MAILBOXES = frozenset(
    {
        "admin",
        "booking",
        "bookings",
        "contact",
        "enquiries",
        "enquiry",
        "event",
        "events",
        "general",
        "hello",
        "help",
        "hi",
        "info",
        "inquiries",
        "inquiry",
        "mail",
        "management",
        "marketing",
        "media",
        "office",
        "partnerships",
        "press",
        "pride",
        "reservations",
        "sales",
        "support",
        "team",
        "tickets",
        "ticketing",
    }
)
_MIN_PHONE_DIGITS = 7
_NUMBER_RUN = re.compile(r"[+(]*\d(?:[\s().\-/+]{0,2}\d)*")


def clean_email(value: str | None) -> str | None:
    """The address in lower case when it looks like one (a ``mailto:`` prefix is removed), otherwise ``None``."""
    text = (value or "").strip().lower().removeprefix("mailto:").strip()
    return text if _EMAIL.match(text) else None


def is_role_email(email: str) -> bool:
    """Whether an address is a shared mailbox of an organisation rather than a person's own."""
    local = email.split("@", 1)[0]
    first_word = re.split(r"[._+-]", local, maxsplit=1)[0]
    return first_word in _ROLE_MAILBOXES


def phone_digits(value: str | None) -> str | None:
    """The digits of a phone number, or ``None`` when there are too few to be one."""
    digits = re.sub(r"\D", "", value or "")
    return digits if len(digits) >= _MIN_PHONE_DIGITS else None


def email_in_text(email: str, text: str) -> bool:
    return email in text.lower()


def phone_in_text(digits: str, text: str) -> bool:
    """Whether the number appears in the text, however its digits are grouped there.

    Each number-like run of the text (digits with at most two separators between them) is compared on its own, so the
    digits of a neighbouring date or price never combine into a match.
    """
    return any(digits in re.sub(r"\D", "", run) for run in _NUMBER_RUN.findall(text))
