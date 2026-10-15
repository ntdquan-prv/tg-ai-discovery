"""TravelGay venue types from the free-text categories that sources attach to a place ("Gay Bar", "Coffee & Tea").

Map APIs give categories, and the extractor gives the type a page suggests ("cocktail bar"); both are read here, so a
venue is labelled with one of the ``VenueType`` values or with nothing when its type is not clear.
"""

import re
from collections.abc import Iterable

from tg_discovery.domain.enums import VenueType

# The first type with a matching category wins, so the more specific types come first: a "Gay sauna" is a sauna, a
# "Cruising bar" is for adults, a "Bar & grill" is a bar and a "Boat tour" is a tour, not an attraction.
_VENUE_TYPES = tuple(
    (venue_type, re.compile(pattern, re.IGNORECASE))
    for venue_type, pattern in (
        (
            VenueType.ADULTS,
            r"\b(?:adults|cruising|cruise (?:clubs?|bars?)|sex (?:clubs?|shops?)|erotic|strip ?clubs?"
            r"|adult (?:clubs?|entertainment|shops?|stores?)|swingers?(?: clubs?)?|dark ?rooms?)\b",
        ),
        (VenueType.SAUNAS, r"\b(?:saunas?|bath ?houses?|steam rooms?)\b"),
        (VenueType.DANCE_CLUB, r"\b(?:night ?clubs?|dance clubs?|discos?|discotheques?)\b"),
        (VenueType.MASSAGE, r"\b(?:massages?|spas?|wellness)\b"),
        (VenueType.GYMS, r"\b(?:gyms?|fitness|health clubs?)\b"),
        (VenueType.BARS, r"\b(?:bars?|pubs?|lounges?|taverns?|taprooms?)\b"),
        (VenueType.CAFE, r"\b(?:cafes?|cafés?|coffee|tea ?houses?|tea rooms?)\b"),
        (VenueType.RESTAURANT, r"\b(?:restaurants?|bistros?|diners?|eatery|eateries|brasseries?|trattorias?)\b"),
        (VenueType.TOURS, r"\b(?:tours?|tour (?:operators?|agency|agencies)|sightseeing|guided walks?|cruises?)\b"),
        (
            VenueType.ATTRACTIONS,
            r"\b(?:attractions?|museums?|landmarks?|monuments?|memorials?|galleries|gallery|historical"
            r"|beach(?:es)?|parks?|zoos?|aquariums?|viewpoints?|points? of interest)\b",
        ),
    )
)


def venue_type_from_categories(categories: Iterable[str]) -> VenueType | None:
    """The TravelGay venue type that the categories point to, or ``None`` when none does."""
    found = list(categories)
    for venue_type, pattern in _VENUE_TYPES:
        if any(pattern.search(category) for category in found):
            return venue_type
    return None
