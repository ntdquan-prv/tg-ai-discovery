"""Finding the pictures of a web page: pure functions over its HTML; nothing here touches the network.

A page names one picture many times: the social preview, the gallery, a blurred stand-in while it loads, a thumbnail for
every screen size, and the site's own logo and icons around them. ``find_pictures`` reads the HTML the way a browser
would, groups the spellings of one picture, drops what is not a picture of the page's subject (icons, logos, stand-ins,
banners, anything small or inside the page header, navigation or footer) and keeps the largest spelling of each.
"""

import json
import re
from contextlib import suppress
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Literal, cast
from urllib.parse import urljoin

from pydantic import BaseModel, ConfigDict

from tg_discovery.domain.urls import clean_image_url, is_placeholder, picture_key, picture_width

MAX_PICTURES = 12
MIN_WIDTH = 400  # a picture declared narrower than this many pixels is a thumbnail or an icon
CHROME_MIN_WIDTH = 600  # inside a header, navigation or footer only a big picture counts (a hero image, not a logo)
MAX_ASPECT = 4.0  # wider or taller than 4:1 is a banner or a strip, not a photograph
MAX_PREFERRED_WIDTH = 2048  # of the sizes a page offers, the widest up to this one is kept: more is only weight

_COVER_META = frozenset({"og:image", "og:image:url", "og:image:secure_url", "twitter:image", "twitter:image:src"})
_CHROME_TAGS = frozenset({"header", "nav", "footer", "aside"})
_CONTENT_TAGS = frozenset({"article", "main"})
_ICON_RELS = frozenset({"icon", "shortcut", "apple-touch-icon", "apple-touch-icon-precomposed", "mask-icon"})
# Where a page keeps the full picture when ``src`` is a small or blurred stand-in (Pinterest, WordPress, lazy loaders).
_LAZY_ATTRIBUTES = (
    "src",
    "data-src",
    "data-lazy-src",
    "data-original",
    "data-lazy",
    "data-pin-media",
    "data-orig-file",
    "data-large-file",
    "data-full-src",
    "data-zoom-src",
)
_NOT_CONTENT = re.compile(
    r"(?<![a-z])(?:logo|icons?|avatar|author|byline|headshot|sprite|emoji|badge|favicon)(?![a-z])", re.IGNORECASE
)
_SRCSET_SPLIT = re.compile(r",\s+|(?<=\d[wx]),(?=\S)")
_DESCRIPTOR = re.compile(r"^(\d+(?:\.\d+)?)([wx])$")
_PIXELS = re.compile(r"^\s*(\d{1,5})(?:px)?\s*$")
_IMAGE_FILE = re.compile(r"\.(?:jpe?g|png|webp|avif|gif)(?:$|[?#])", re.IGNORECASE)

Kind = Literal["cover", "structured", "body"]
_RANK: dict[Kind, int] = {"cover": 0, "structured": 1, "body": 2}


class PageImage(BaseModel):
    """The address of one picture on a page, with the text the page gives for it."""

    model_config = ConfigDict(frozen=True)

    url: str
    alt: str | None = None


@dataclass(slots=True)
class _Sighting:
    url: str
    kind: Kind
    width: int | None = None
    height: int | None = None
    alt: str | None = None
    chrome: bool = False  # inside the page header, navigation or footer
    content: bool = False  # inside the page's article or main element
    teaser: bool = False  # inside a link to another page: a card for a related story, not a picture of this one
    noise: bool = False  # its alt text, class or id says logo, icon, avatar...


def _pixels(value: str | None) -> int | None:
    found = _PIXELS.match(value or "")
    return int(found.group(1)) if found else None


def _image_values(value: object) -> list[str]:
    """The addresses in a JSON-LD ``image``: a string, an ``ImageObject`` or a list of either."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [address for item in cast(list[object], value) for address in _image_values(item)]
    if isinstance(value, dict):
        fields = cast(dict[str, object], value)
        return _image_values(fields.get("url")) + _image_values(fields.get("contentUrl"))
    return []


def _structured_images(node: object, depth: int = 0) -> list[str]:
    """Every ``image`` anywhere in a JSON-LD document (it nests them in ``@graph`` lists and in other objects)."""
    if depth > 6:
        return []
    found: list[str] = []
    if isinstance(node, list):
        for item in cast(list[object], node):
            found += _structured_images(item, depth + 1)
    elif isinstance(node, dict):
        for key, value in cast(dict[str, object], node).items():
            if key == "image":
                found += _image_values(value)
            else:
                found += _structured_images(value, depth + 1)  # a scalar has no images: the call returns at once
    return found


def _srcset(value: str) -> list[tuple[str, int | None]]:
    """The addresses of a ``srcset`` with the width each one declares (``480w``); a density (``2x``) declares none."""
    candidates: list[tuple[str, int | None]] = []
    for part in _SRCSET_SPLIT.split(value.strip()):
        tokens = part.split()
        if not tokens:
            continue
        descriptor = _DESCRIPTOR.match(tokens[-1]) if len(tokens) > 1 else None
        width = int(float(descriptor.group(1))) if descriptor and descriptor.group(2) == "w" else None
        candidates.append((tokens[0], width))
    return candidates


class _Collector(HTMLParser):
    """Gathers every sighting of a picture in a page, in the order the page mentions them."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._base = base_url
        self._page = base_url.split("#", 1)[0]
        self._anchor: str | None = None  # where the link we are inside points
        self.sightings: list[_Sighting] = []
        self.icons: set[str] = set()
        self._chrome = 0
        self._content = 0
        self._json: list[str] | None = None
        self._last_cover: _Sighting | None = None

    def _address(self, raw: str) -> str | None:
        value = raw.strip()
        if not value or value.lower().startswith(("data:", "javascript:", "blob:")):
            return None
        return urljoin(self._base, value)

    def _add(
        self,
        raw: str,
        kind: Kind,
        *,
        width: int | None = None,
        height: int | None = None,
        alt: str | None = None,
        noise: bool = False,
        teaser: bool = False,
    ) -> _Sighting | None:
        url = self._address(raw)
        if url is None:
            return None
        sighting = _Sighting(
            url=url,
            kind=kind,
            width=width or picture_width(url),
            height=height,
            alt=alt,
            chrome=self._chrome > 0,
            content=self._content > 0,
            teaser=teaser,
            noise=noise,
        )
        self.sightings.append(sighting)
        return sighting

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if tag in _CHROME_TAGS:
            self._chrome += 1
        elif tag in _CONTENT_TAGS:
            self._content += 1
        elif tag == "a":
            self._anchor = self._address(attributes.get("href", ""))
        elif tag == "base" and attributes.get("href"):
            self._base = urljoin(self._base, attributes["href"])
        elif tag == "meta":
            self._meta(attributes)
        elif tag == "link":
            self._link(attributes)
        elif tag == "script" and "ld+json" in attributes.get("type", "").lower():
            self._json = []
        elif tag == "img":
            self._img(attributes)
        elif tag == "source":
            self._sources(attributes, None, noise=False, teaser=self._in_teaser())

    def handle_endtag(self, tag: str) -> None:
        if tag in _CHROME_TAGS and self._chrome:
            self._chrome -= 1
        elif tag in _CONTENT_TAGS and self._content:
            self._content -= 1
        elif tag == "a":
            self._anchor = None
        elif tag == "script" and self._json is not None:
            self._structured("".join(self._json))
            self._json = None

    def handle_data(self, data: str) -> None:
        if self._json is not None:
            self._json.append(data)

    def _meta(self, attributes: dict[str, str]) -> None:
        name = (attributes.get("property") or attributes.get("name") or "").lower()
        content = attributes.get("content", "")
        if name in _COVER_META:
            self._last_cover = self._add(content, "cover")
        elif name == "og:image:width" and self._last_cover:
            self._last_cover.width = _pixels(content) or self._last_cover.width
        elif name == "og:image:height" and self._last_cover:
            self._last_cover.height = _pixels(content)

    def _link(self, attributes: dict[str, str]) -> None:
        rels = set(attributes.get("rel", "").lower().split())
        href = self._address(attributes.get("href", ""))
        if href is None:
            return
        if rels & _ICON_RELS:
            self.icons.add(href)
        elif "image_src" in rels:
            self._add(href, "cover")

    def _structured(self, text: str) -> None:
        try:
            document: object = json.loads(text)
        except ValueError:
            return
        for address in _structured_images(document):
            self._add(address, "structured")

    def _in_teaser(self) -> bool:
        """Whether we are inside a link to another page; a link to this page or to a bigger picture is no teaser."""
        if self._anchor is None:
            return False
        target = self._anchor.split("#", 1)[0]
        return target != self._page and _IMAGE_FILE.search(target) is None

    def _img(self, attributes: dict[str, str]) -> None:
        context = " ".join(attributes.get(key, "") for key in ("alt", "class", "id"))
        noise = _NOT_CONTENT.search(context) is not None
        alt = attributes.get("alt", "").strip() or None
        width, height = _pixels(attributes.get("width")), _pixels(attributes.get("height"))
        teaser = self._in_teaser()
        for key in _LAZY_ATTRIBUTES:
            if key in attributes:
                self._add(attributes[key], "body", width=width, height=height, alt=alt, noise=noise, teaser=teaser)
        self._sources(attributes, alt, noise=noise, teaser=teaser)

    def _sources(self, attributes: dict[str, str], alt: str | None, *, noise: bool, teaser: bool) -> None:
        for key in ("srcset", "data-srcset"):
            for address, width in _srcset(attributes.get(key, "")):
                self._add(address, "body", width=width, alt=alt, noise=noise, teaser=teaser)


def _usable(sighting: _Sighting) -> bool:
    """Whether one sighting may stand for a picture of the page's subject."""
    if sighting.noise or sighting.teaser or is_placeholder(sighting.url):
        return False
    if sighting.chrome and sighting.kind == "body" and (sighting.width or 0) < CHROME_MIN_WIDTH:
        return False
    if sighting.width and sighting.height:
        longest, shortest = max(sighting.width, sighting.height), min(sighting.width, sighting.height)
        if longest / shortest > MAX_ASPECT:
            return False
    return True


def _largest(spellings: list[_Sighting]) -> _Sighting:
    """The spelling to keep: the widest up to ``MAX_PREFERRED_WIDTH``, else the narrowest above it, else the first."""
    sized = [item for item in spellings if item.width]
    within = [item for item in sized if (item.width or 0) <= MAX_PREFERRED_WIDTH]
    if within:
        return max(within, key=lambda item: item.width or 0)
    if sized:
        return min(sized, key=lambda item: item.width or 0)
    return spellings[0]


def find_pictures(html: str, base_url: str, *, limit: int = MAX_PICTURES) -> list[PageImage]:
    """The pictures of a page, each once and in its largest spelling: the social preview first, then the others.

    ``base_url`` resolves relative addresses. Malformed HTML gives what could be read before the fault.
    """
    collector = _Collector(base_url)
    with suppress(Exception):  # a page that breaks the parser still gives the pictures read before the break
        collector.feed(html)
        collector.close()
    icons = {picture_key(address) for address in collector.icons}
    sightings = collector.sightings
    if any(item.content for item in sightings if item.kind == "body"):
        # The page marks its article: pictures around it (related stories, sidebars) are not about this page's subject.
        sightings = [item for item in sightings if item.kind != "body" or item.content]
    groups: dict[str, list[_Sighting]] = {}
    for sighting in sightings:
        key = picture_key(sighting.url)
        if key not in icons and clean_image_url(sighting.url) is not None:
            groups.setdefault(key, []).append(sighting)
    ranked = sorted(groups.values(), key=lambda group: min(_RANK[item.kind] for item in group))  # stable: page order
    pictures: list[PageImage] = []
    for group in ranked:
        usable = [item for item in group if _usable(item)]
        if not usable:
            continue
        best = _largest(usable)
        if best.width is not None and best.width < MIN_WIDTH:
            continue
        address = clean_image_url(best.url)
        if address is None:
            continue
        alt = next((item.alt for item in group if item.alt), None)
        pictures.append(PageImage(url=address, alt=alt))
        if len(pictures) == limit:
            break
    return pictures
