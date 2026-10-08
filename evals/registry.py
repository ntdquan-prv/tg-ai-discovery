"""The bake-off's view of the provider registry: what each provider is, priced and described for the run metadata.

The registry itself (entries, terms notes, building) is shared with the service in ``tg_discovery.providers.registry``.
"""

from collections.abc import Sequence

from evals.models import ProviderInfo
from tg_discovery.providers.registry import SEARCH_ENTRIES, VENUE_ENTRIES

CLI_HINT = "pass --accept-terms {name}"  # how the owner accepts a provider's terms on the bake-off command line


def provider_infos(names: Sequence[str] | None, skipped: dict[str, str]) -> list[ProviderInfo]:
    """The run metadata for every wanted provider, the skipped ones with their reason."""
    infos: list[ProviderInfo] = []
    for kind, entries in (("search", SEARCH_ENTRIES), ("venue", VENUE_ENTRIES)):
        for entry in entries:
            if names is not None and entry.name not in names:
                continue
            infos.append(
                ProviderInfo(
                    name=entry.name,
                    kind=kind,  # pyright: ignore[reportArgumentType]
                    flows=sorted(entry.flows),
                    google_derived=entry.google_derived,
                    price_unit=entry.price.unit,
                    usd_per_unit=entry.price.usd_per_unit,
                    price_verified_on=entry.price.verified_on,
                    price_source=entry.price.source_url,
                    price_notes=entry.price.notes,
                    terms_note=entry.terms,
                    skipped_reason=skipped.get(entry.name),
                )
            )
    return infos
