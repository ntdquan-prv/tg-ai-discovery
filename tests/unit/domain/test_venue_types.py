import pytest

from tg_discovery.domain.enums import VenueType
from tg_discovery.domain.venue_types import venue_type_from_categories


@pytest.mark.parametrize(
    ("label", "venue_type"),
    [
        # What the extractor is asked to write, and the free words it writes anyway.
        *((venue_type.value, venue_type) for venue_type in VenueType),
        ("cocktail bar", VenueType.BARS),
        ("gay bar", VenueType.BARS),
        ("Gay cruising bar", VenueType.ADULTS),
        ("gay sauna", VenueType.SAUNAS),
        ("nightclub", VenueType.DANCE_CLUB),
        ("day spa", VenueType.MASSAGE),
        ("coffee shop", VenueType.CAFE),
        ("bistro", VenueType.RESTAURANT),
        ("dinner cruise", VenueType.TOURS),
        ("walking tour", VenueType.TOURS),
        ("museum", VenueType.ATTRACTIONS),
        ("hotel", None),
        ("parking", None),
        ("", None),
    ],
)
def test_a_label_becomes_one_of_the_ten_travelgay_types_or_nothing(label: str, venue_type: VenueType | None) -> None:
    assert venue_type_from_categories([label]) == venue_type


def test_the_first_matching_type_wins_whatever_order_the_categories_come_in() -> None:
    assert venue_type_from_categories(["Restaurant", "Bar"]) is VenueType.BARS
    assert venue_type_from_categories(["Bar", "Night club"]) is VenueType.DANCE_CLUB
