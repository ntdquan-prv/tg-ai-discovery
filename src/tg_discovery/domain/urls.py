"""URL clean-up and website identity. Pure functions: nothing here touches the network."""

import ipaddress
import re
from functools import cache
from typing import NamedTuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from publicsuffixlist import PublicSuffixList

_TRACKING_PARAMS = frozenset({"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid", "yclid", "_ga"})
_DEFAULT_PORTS = {"http": 80, "https": 443}
_HOST_PORT = re.compile(r"[^:/]+:\d+")

# Registrable domains that never identify a single venue: social networks, link pages, maps, listings and ticketing.
_AGGREGATOR_DOMAINS = frozenset(
    """
    facebook.com fb.com fb.me instagram.com twitter.com x.com tiktok.com youtube.com youtu.be threads.net bsky.app
    linktr.ee linkin.bio beacons.ai wa.me t.me line.me weibo.com vk.com goo.gl bit.ly wikipedia.org wikidata.org
    openstreetmap.org eventbrite.com meetup.com ticketmaster.com songkick.com bandsintown.com allevents.in dice.fm
    ra.co residentadvisor.net travelgay.com gaycities.com
    """.split()  # noqa: SIM905 - one word per entry is easier to maintain than a quoted list
)
# Brands that own many country domains (google.co.th, tripadvisor.co.uk, booking.com...), matched on the first label.
_AGGREGATOR_LABELS = frozenset({"google", "tripadvisor", "yelp", "booking", "agoda", "expedia", "hotels", "airbnb"})


class _Parts(NamedTuple):
    scheme: str
    host: str
    port: int | None  # None when it is the scheme's default port
    path: str
    query: str

    @property
    def netloc(self) -> str:
        return f"{self.host}:{self.port}" if self.port else self.host


@cache
def _suffix_list() -> PublicSuffixList:
    return PublicSuffixList()


def _ascii_host(host: str) -> str:
    """Lower-case the host and convert an internationalised name to punycode so both spellings compare equal."""
    host = host.strip().strip(".").lower()
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def _split(url: str) -> _Parts | None:
    """Split an http(s) URL; bare domains such as ``www.heaven.com/events`` get ``https://``."""
    candidate = url.strip()
    if not candidate or any(char.isspace() for char in candidate):
        return None
    if "://" not in candidate:
        head = candidate.split("/", 1)[0]
        if (":" in head and not _HOST_PORT.fullmatch(head)) or "." not in head:
            return None  # mailto:, tel:, javascript:, plain words
        candidate = f"https://{candidate}"
    try:
        parts = urlsplit(candidate)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not parts.hostname:
        return None
    return _Parts(
        scheme=scheme,
        host=_ascii_host(parts.hostname),
        port=None if port in (None, _DEFAULT_PORTS[scheme]) else port,
        path=parts.path.rstrip("/"),
        query=parts.query,
    )


def _clean_query(query: str) -> str:
    kept = [
        (key, value)
        for key, value in parse_qsl(query, keep_blank_values=True)
        if key.lower() not in _TRACKING_PARAMS and not key.lower().startswith("utm_")
    ]
    return urlencode(sorted(kept))


def clean_url(url: str) -> str | None:
    """Return a fetchable http(s) URL without fragment or tracking parameters, or ``None`` if it is not a web URL."""
    parts = _split(url)
    if parts is None:
        return None
    query = _clean_query(parts.query)
    path = parts.path if parts.path or not query else "/"
    return urlunsplit((parts.scheme, parts.netloc, path, query, ""))


def url_identity(url: str) -> str | None:
    """Return a scheme-less, ``www``-less key that is equal for equivalent page URLs. Not meant for fetching."""
    parts = _split(url)
    if parts is None:
        return None
    netloc = parts.netloc.removeprefix("www.")
    query = _clean_query(parts.query)
    return f"{netloc}{parts.path}?{query}" if query else f"{netloc}{parts.path}"


def registrable_domain(url_or_host: str) -> str | None:
    """Return the registrable domain (``events.heaven.co.uk`` gives ``heaven.co.uk``); ``None`` for IPs."""
    parts = _split(url_or_host)
    if parts is None or _is_ip(parts.host):
        return None
    return _suffix_list().privatesuffix(parts.host)


def is_aggregator(domain: str) -> bool:
    """Whether a registrable domain is a social network, listing or ticketing site rather than a venue's own site."""
    return domain in _AGGREGATOR_DOMAINS or domain.split(".", 1)[0] in _AGGREGATOR_LABELS


def website_key(url: str | None) -> str | None:
    """Return the registrable domain of a venue's own website, or ``None`` when the URL cannot identify a venue."""
    if not url:
        return None
    domain = registrable_domain(url)
    if domain is None or is_aggregator(domain):
        return None
    return domain
