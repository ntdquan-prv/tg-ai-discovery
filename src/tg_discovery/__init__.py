"""TG Discovery: finds new LGBTQ+ venues and events for TravelGay admins to review."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("tg-discovery")
except PackageNotFoundError:  # pragma: no cover - a source tree that was never installed
    __version__ = "0.0.0"
