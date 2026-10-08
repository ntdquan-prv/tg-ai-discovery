import json
from typing import cast

import pytest

from tg_discovery.domain.enums import EventStatus, EventType, LgbtqLevel
from tg_discovery.llm.schemas import (
    EventExtraction,
    ExtractedEvent,
    ExtractedLgbtq,
    ExtractedVenue,
    LocalQueries,
    VenueExtraction,
    tool_schema,
)


@pytest.mark.parametrize("model", [VenueExtraction, EventExtraction, LocalQueries])
def test_tool_schemas_are_self_contained(model: type[VenueExtraction | EventExtraction | LocalQueries]) -> None:
    schema = tool_schema(model)
    text = json.dumps(schema)

    assert "$ref" not in text
    assert "$defs" not in text
    assert '"title": "' not in text


def _dig(node: object, *path: str) -> object:
    for key in path:
        node = cast(dict[str, object], node)[key]
    return node


def test_the_venue_schema_lists_what_the_model_must_supply() -> None:
    schema = tool_schema(VenueExtraction)

    venue = _dig(schema, "properties", "venues", "items")
    required = cast(list[str], _dig(venue, "required"))
    assert {"name", "lgbtq", "source_urls"} <= set(required)
    assert "address" not in required
    assert _dig(venue, "properties", "lgbtq", "properties", "level", "enum") == ["primary", "friendly", "unclear", "no"]


def test_an_unknown_lgbtq_level_becomes_unclear() -> None:
    assert ExtractedLgbtq.model_validate({"level": "very gay"}).level is LgbtqLevel.UNCLEAR
    assert ExtractedLgbtq.model_validate({"level": "primary"}).level is LgbtqLevel.PRIMARY


def test_a_text_the_model_wrapped_in_a_list_is_read_as_text() -> None:
    # gpt-oss on Bedrock sent venue_type and address as lists for every venue it was asked about.
    venue = ExtractedVenue.model_validate(
        {
            "name": "Rise Bar",
            "venue_type": ["sports bar"],
            "address": ["100 7th Avenue South", "New York, NY"],
            "phone": [],
            "website": ["", "  "],
            "lgbtq": {"level": "primary"},
            "source_urls": ["https://x.com"],
        }
    )

    assert venue.venue_type == "sports bar"
    assert venue.address == "100 7th Avenue South, New York, NY"
    assert (venue.phone, venue.website) == (None, None)


def test_lists_stay_lists_where_a_list_is_expected_and_other_values_are_left_alone() -> None:
    event = ExtractedEvent.model_validate(
        {
            "name": ["Pride", "Night"],
            "venue_name": "Hall",
            "lgbtq": {"level": "primary"},
            "source_urls": ["https://x.com/a", "https://x.com/b"],
        }
    )

    assert event.name == "Pride, Night"
    assert event.venue_name == "Hall"
    assert event.source_urls == ["https://x.com/a", "https://x.com/b"]


def test_unknown_event_labels_fall_back_to_defaults() -> None:
    event = ExtractedEvent.model_validate(
        {
            "name": "Pride Night",
            "event_type": "party",
            "recurring_type": "daily",
            "audience": "everyone",
            "status": "TBD",
            "lgbtq": {"level": "primary"},
            "source_urls": ["https://x.com"],
        }
    )

    assert event.event_type is EventType.EVENT
    assert event.status is EventStatus.UNKNOWN
    assert event.recurring_type.value == "oneday"
    assert event.audience.value == "unknown"
