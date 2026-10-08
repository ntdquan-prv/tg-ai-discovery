"""TravelGay venue types from the free-text categories that map APIs attach to a place ("Gay Bar", "Coffee & Tea")."""

import re
from collections.abc import Iterable

# The first type with a matching category wins, so the more specific types come first.
_VENUE_TYPES = tuple(
    (venue_type, re.compile(pattern, re.IGNORECASE))
    for venue_type, pattern in (
        ("Dance Club", r"\b(?:night ?clubs?|dance clubs?|discos?|discotheques?)\b"),
        ("Sauna", r"\b(?:saunas?|bath ?houses?|steam rooms?)\b"),
        ("Gym", r"\b(?:gyms?|fitness)\b"),
        ("Spa", r"\bspas?\b"),
        ("Hotels", r"\b(?:hotels?|hostels?|motels?|guest ?houses?)\b"),
        ("Bar", r"\b(?:bars?|pubs?)\b"),
        ("Cafe", r"\b(?:cafes?|cafés?|coffee)\b"),
        ("Restaurant", r"\brestaurants?\b"),
        ("Beach", r"\bbeach(?:es)?\b"),
        ("Shop", r"\b(?:stores?|boutiques?|sex shops?|gift shops?)\b"),
    )
)


def venue_type_from_categories(categories: Iterable[str]) -> str | None:
    """The TravelGay venue type that the categories point to, or ``None`` when none does."""
    found = list(categories)
    for venue_type, pattern in _VENUE_TYPES:
        if any(pattern.search(category) for category in found):
            return venue_type
    return None
