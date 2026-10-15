"""URL clean-up and website identity. Pure functions: nothing here touches the network."""

import ipaddress
import re
from functools import cache
from typing import NamedTuple
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

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


# Image URLs that are never a picture of the thing itself: icons, logos, avatars, sprites, tracking pixels, vectors.
_NOT_A_PHOTO = re.compile(
    r"\.(?:svg|ico|gif)(?:$|[?#])|1x1"
    r"|(?<![a-z])(?:logo|icons?|avatar|headshot|favicon|sprite|spacer|pixel|badge|img-default|default-(?:image|img)"
    r"|no-?image|image-not-found)(?![a-z])",
    re.IGNORECASE,
)
# Hosts that serve tracking pixels and ads, never a picture of anything.
_TRACKER_HOSTS = (
    "scorecardresearch.com",
    "doubleclick.net",
    "googlesyndication.com",
    "google-analytics.com",
    "googletagmanager.com",
    "adsrvr.org",
    "quantserve.com",
    "facebook.net",
)


# Query parameters that only say how a picture is sized or encoded, so that spellings of one picture share a key.
_RESIZE_PARAMS = frozenset(
    """
    w h width height maxwidth maxheight size resize fit crop rect quality q auto fm format dpr sharp blur fill s sig
    signature coordinates ar gravity ssl strip zoom
    """.split()  # noqa: SIM905 - one word per entry is easier to maintain than a quoted list
)
_PHOTON_HOST = re.compile(r"i[0-3]\.wp\.com")
_WIX_MEDIA = re.compile(r"^/media/([^/]+)")
_CDN_RESIZE = re.compile(r"^/cdn-cgi/image/[^/]*/")  # Cloudflare Image Resizing: the options, then the original
_SIZE_DIRS = re.compile(r"/\d{3,4}/\d{3,4}(?=/[^/]+$)")  # /400/225/ (not a /2024/10/ date) right before the file name
_SIZE_SUFFIX = re.compile(r"(?:[-_]\d{2,4}x\d{2,4}|_\d{2,4}x|@\dx|-scaled)(?=\.[a-z0-9]+$)", re.IGNORECASE)
_IMAGE_EXTENSION = re.compile(r"\.(?:jpe?g|png|webp|avif)$", re.IGNORECASE)
_WIDTH_IN_PATH = re.compile(r"(?:^|[/,_-])(?:w_|width=)(\d{2,5})(?=[,/_.&]|$)")
_WIDTH_IN_DIRS = re.compile(r"/(\d{3,4})/\d{3,4}(?=/[^/]+$)")
_WIDTH_IN_NAME = re.compile(r"[-_](\d{2,4})x\d{0,4}(?=\.[a-z0-9]+$)", re.IGNORECASE)
_PLACEHOLDER = re.compile(r"blur[_=]|lqip|placeholder", re.IGNORECASE)


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


def clean_image_url(url: str) -> str | None:
    """Return an absolute http(s) image URL without its fragment, or ``None`` if it is not a usable photo.

    Unlike ``clean_url`` the query is kept exactly as written: signed image URLs break when parameters are reordered.
    Data URIs, relative paths, icons, logos, avatars, sprites, tracking pixels and vector or animated files are refused.
    """
    candidate = url.strip()
    parts = _split(candidate) if candidate.lower().startswith(("http://", "https://")) else None
    if parts is None or _NOT_A_PHOTO.search(candidate):
        return None
    if any(parts.host == tracker or parts.host.endswith(f".{tracker}") for tracker in _TRACKER_HOSTS):
        return None
    return candidate.split("#", 1)[0]


def picture_key(url: str) -> str:
    """A key that is the same for every spelling of one picture: other sizes, crops and encodings of its address.

    Understands Wix (``/media/<id>/v1/fill/w_888...``), Cloudflare (``/cdn-cgi/image/width=640/<original>``), WordPress
    (``-300x200``), Shopify (``_800x``), size folders (``/400/225/``), another file format of the same picture, and CDNs
    that size pictures with query parameters (imgix, Eventbrite's). An address it does not recognise keys on itself, so
    the worst case is a repeated picture; the aim is never to merge two different pictures.
    """
    parts = _split(url)
    if parts is None:
        return url
    host = parts.host.removeprefix("www.")
    path = unquote(parts.path)
    wix = _WIX_MEDIA.match(path) if host.endswith("wixstatic.com") else None
    if wix:
        return f"wix:{wix.group(1)}"
    resized = _CDN_RESIZE.match(path)
    if resized:
        original = path[resized.end() :]
        return (
            picture_key(original)
            if original.startswith(("http://", "https://"))
            else picture_key(f"https://{host}/{original}")
        )
    if path == "/_next/image":  # Next.js: the picture is the ``url`` parameter, the rest only sizes it
        original = next((value for key, value in parse_qsl(parts.query) if key == "url"), "")
        if original:
            return picture_key(original if original.startswith("http") else f"https://{host}/{original.lstrip('/')}")
    if _PHOTON_HOST.fullmatch(host):  # WordPress's image proxy: i0.wp.com/<the site>/<path>
        return picture_key(f"https://{path.lstrip('/')}")
    named_file = _IMAGE_EXTENSION.search(path) is not None
    path = _IMAGE_EXTENSION.sub("", _SIZE_SUFFIX.sub("", _SIZE_DIRS.sub("", path)))
    # A file name ending in .jpg identifies the picture; only its ``id`` can say more. Otherwise the query may.
    ignored = _RESIZE_PARAMS | _TRACKING_PARAMS
    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query)
        if (key.lower() == "id" if named_file else key.lower() not in ignored and not key.lower().startswith("utm_"))
    ]
    query = urlencode(sorted(kept))
    return f"{host}{path}?{query}" if query else f"{host}{path}"


def picture_width(url: str) -> int | None:
    """The width in pixels an address asks for (``w_888``, ``width=640``, ``?w=888``, ``/400/225/``, ``-300x200``)."""
    parts = _split(url)
    if parts is None:
        return None
    path = unquote(parts.path)
    if found := _WIDTH_IN_PATH.search(path):
        return int(found.group(1))
    for key, value in parse_qsl(parts.query):
        if key.lower() in ("w", "width", "maxwidth") and value.isdigit():
            return int(value)
    found = _WIDTH_IN_NAME.search(path) or _WIDTH_IN_DIRS.search(path)
    return int(found.group(1)) if found else None


def is_placeholder(url: str) -> bool:
    """Whether an address is the blurred stand-in a page shows while the real picture loads."""
    return _PLACEHOLDER.search(unquote(url)) is not None


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
