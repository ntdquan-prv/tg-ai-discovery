"""Identity rules for venues and events. Pure functions; the production dedupe (M2) uses the same rules.

A venue is identified by the strongest key it has, in this order: Google ``place_id``, OSM element, Foursquare id,
the registrable domain of its own website, and finally its normalised name plus a geohash. Every key is paired with a
scope (country and city) so that same-name venues in different cities never merge. Two records are the same venue when
any of their keys match.

An event is identified by its core title (no year or episode number) plus its venue or, failing that, its city. Dates
are deliberately not part of the key: a recurring event is one candidate with many occurrences.
"""

import re
import unicodedata

from rapidfuzz import fuzz

from tg_discovery.domain.geo import geohash_encode, haversine_m
from tg_discovery.domain.models import EventDraft, EventOccurrence, VenueDraft
from tg_discovery.domain.urls import website_key

# Words that say what a place is rather than which place it is. "The Heaven Gay Bar" and "Heaven" are one venue.
GENERIC_NAME_WORDS = frozenset(
    """
    the and bar bars club clubs pub pubs cafe caffe kaffee lounge gay lesbian lgbt lgbtq queer disco discoteca
    nightclub sauna spa bathhouse restaurant ristorante restaurante hotel kneipe bodega
    """.split()  # noqa: SIM905 - one word per entry is easier to maintain than a quoted list
)
_WORD = re.compile(r"\w+")
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_EDITION = re.compile(r"\b(?:vol|volume|ep|episode|edition|part|pt|no|number)\.?\s*\d+\b|#\s*\d+")
_ORDINAL_EDITION = re.compile(r"\b\d+(?:st|nd|rd|th)\s+(?:annual|edition|anniversary)\b|\bannual\b")
TITLE_MATCH_THRESHOLD = 0.88
_VENUE_NAME_MATCH_THRESHOLD = 0.85
_SAME_PLACE_METRES = 150


def _strip_latin_diacritics(text: str) -> str:
    """Remove accents from Latin, Greek and Cyrillic letters but leave kana, hangul, Thai and the like untouched."""
    kept: list[str] = []
    for char in unicodedata.normalize("NFC", text):
        decomposed = unicodedata.normalize("NFKD", char)
        base = decomposed[0]
        script = unicodedata.name(base, "")
        if script.startswith(("LATIN", "GREEK", "CYRILLIC")):
            kept.append(base)
        else:
            kept.append(char)
    return "".join(kept)


def _words(text: str) -> list[str]:
    return _WORD.findall(_strip_latin_diacritics(text.casefold().replace("&", " and ")))


def normalize_name(name: str) -> str:
    """Casefold, strip accents, drop generic words ("bar", "club", "the"...) and punctuation; CJK is kept as is."""
    words = _words(name)
    significant = [word for word in words if word not in GENERIC_NAME_WORDS]
    return " ".join(significant or words)


def core_title(title: str) -> str:
    """An event title without years, edition and episode numbers, so yearly or numbered editions share one key."""
    lowered = _strip_latin_diacritics(title.casefold())
    for pattern in (_YEAR, _EDITION, _ORDINAL_EDITION):
        lowered = pattern.sub(" ", lowered)
    return " ".join(_WORD.findall(lowered))


def similarity(a: str, b: str) -> float:
    """String similarity in [0, 1] that tolerates word order."""
    if not a or not b:
        return 0.0
    return max(fuzz.ratio(a, b), fuzz.token_sort_ratio(a, b)) / 100


def venue_keys(draft: VenueDraft, scope: str) -> list[str]:
    """Every identity key of a venue, strongest first. ``scope`` is the country and city the venue was found in."""
    keys: list[str] = []
    for prefix, id_name in (("gpid", "google_place_id"), ("osm", "osm"), ("fsq", "fsq")):
        if value := draft.external_ids.get(id_name):
            keys.append(f"{prefix}:{value}")
    if domain := website_key(draft.website):
        keys.append(f"{scope}|dom:{domain}")
    name = normalize_name(draft.name)
    if draft.lat is not None and draft.lng is not None:
        keys.append(f"{scope}|ng:{name}|{geohash_encode(draft.lat, draft.lng, 7)}")
    elif not keys:
        # A text-only draft has no coordinates; its name inside one city is the weakest key and only used alone.
        keys.append(f"{scope}|n:{name}")
    return keys


def venue_fingerprint(draft: VenueDraft, scope: str) -> str:
    """The strongest identity key of a venue."""
    return venue_keys(draft, scope)[0]


def _event_venue_key(draft: EventDraft) -> str:
    if draft.venue.google_place_id:
        return f"gpid:{draft.venue.google_place_id}"
    if draft.venue.name:
        return f"v:{normalize_name(draft.venue.name)}"
    return ""


def event_fingerprint(draft: EventDraft, scope: str) -> str:
    """Core title plus venue (or the city when the venue is unknown); the date is not part of the key."""
    return f"{scope}|{core_title(draft.name)}|{_event_venue_key(draft) or 'city'}"


def _same_venue(a: EventDraft, b: EventDraft) -> bool:
    if a.venue.google_place_id and a.venue.google_place_id == b.venue.google_place_id:
        return True
    if None not in (a.venue.lat, a.venue.lng, b.venue.lat, b.venue.lng):
        distance = haversine_m(a.venue.lat or 0, a.venue.lng or 0, b.venue.lat or 0, b.venue.lng or 0)
        if distance <= _SAME_PLACE_METRES:
            return True
    if a.venue.name and b.venue.name:
        return similarity(normalize_name(a.venue.name), normalize_name(b.venue.name)) >= _VENUE_NAME_MATCH_THRESHOLD
    return False


def _overlap(a: EventOccurrence, b: EventOccurrence) -> bool:
    return a.start_date <= b.last_date and b.start_date <= a.last_date


def events_match(a: EventDraft, b: EventDraft, *, threshold: float = TITLE_MATCH_THRESHOLD) -> bool:
    """Fuzzy merge rule: similar core titles and either the same venue or overlapping dates."""
    if similarity(core_title(a.name), core_title(b.name)) < threshold:
        return False
    if _same_venue(a, b):
        return True
    return any(_overlap(x, y) for x in a.occurrences for y in b.occurrences)
