from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from evals.inputs import InputsError, load_cities, load_golden_events, load_golden_venues, load_inputs, snapshot_inputs

EXAMPLES = Path(__file__).resolve().parents[3] / "evals" / "examples"
CITIES_HEADER = "slug,name,country,country_name,timezone,languages,aliases,tg_city_id,lat,lng,south,west,north,east\n"


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_the_example_inputs_load() -> None:
    inputs = load_inputs(EXAMPLES)

    assert [city.slug for city in inputs.cities] == ["testville", "exampleburg"]
    assert inputs.cities[1].languages == ("en", "de")
    assert inputs.cities[1].aliases == ("Exampleberg",)
    assert inputs.cities[0].bbox is not None
    assert inputs.cities[0].bbox.contains(10.0, 20.0)
    assert len(inputs.venues["testville"]) == 16
    assert len(inputs.events["exampleburg"]) == 12
    assert len(inputs.canaries["testville"]) == 4
    assert inputs.canaries["testville"][0].announced_at == datetime(2026, 10, 6, 18, tzinfo=UTC)


def test_an_event_schedule_spans_several_rows() -> None:
    inputs = load_inputs(EXAMPLES)

    weekly = next(event for event in inputs.events["testville"] if event.id == "tee03")
    festival = next(event for event in inputs.events["testville"] if event.id == "tee04")

    assert [o.start_date for o in weekly.occurrences] == [
        date(2026, 11, 1),
        date(2026, 11, 8),
        date(2026, 11, 15),
        date(2026, 11, 22),
    ]
    assert (festival.occurrences[0].start_date, festival.occurrences[0].end_date) == (
        date(2026, 11, 5),
        date(2026, 11, 7),
    )


def test_a_missing_cities_file_says_where_to_look(tmp_path: Path) -> None:
    with pytest.raises(InputsError, match=r"cities\.csv does not exist"):
        load_inputs(tmp_path)


def test_a_missing_required_column_names_the_row(tmp_path: Path) -> None:
    path = _write(tmp_path / "cities.csv", "slug,name\nbangkok,Bangkok\n")

    with pytest.raises(InputsError, match=r"cities\.csv row 2: column 'timezone' is required"):
        load_cities(path)


def test_an_unknown_time_zone_is_rejected(tmp_path: Path) -> None:
    row = "bangkok,Bangkok,TH,Thailand,Asia/Bangkokk,en,,,,,,,,\n"

    with pytest.raises(InputsError, match="unknown time zone 'Asia/Bangkokk'"):
        load_cities(_write(tmp_path / "cities.csv", CITIES_HEADER + row))


def test_a_partial_bounding_box_is_rejected(tmp_path: Path) -> None:
    row = "bangkok,Bangkok,TH,Thailand,Asia/Bangkok,en,,,,,13.5,100.3,,\n"

    with pytest.raises(InputsError, match="all of south, west, north and east"):
        load_cities(_write(tmp_path / "cities.csv", CITIES_HEADER + row))


def test_duplicate_slugs_are_rejected(tmp_path: Path) -> None:
    row = "bangkok,Bangkok,th,Thailand,Asia/Bangkok,en,,,,,,,,\n"

    with pytest.raises(InputsError, match="slugs must be unique"):
        load_cities(_write(tmp_path / "cities.csv", CITIES_HEADER + row + row))


def test_the_country_code_is_upper_cased(tmp_path: Path) -> None:
    row = "bangkok,Bangkok,th,Thailand,Asia/Bangkok,en|th,Krung Thep|กรุงเทพ,7,,,,,,\n"

    (city,) = load_cities(_write(tmp_path / "cities.csv", CITIES_HEADER + row))

    assert (city.country, city.languages, city.aliases, city.tg_city_id) == (
        "TH",
        ("en", "th"),
        ("Krung Thep", "กรุงเทพ"),
        7,
    )


def test_golden_venues_match_cities_by_name_or_alias_and_tolerate_bad_coordinates(tmp_path: Path) -> None:
    cities = load_cities(
        _write(
            tmp_path / "cities.csv", CITIES_HEADER + "bangkok,Bangkok,TH,Thailand,Asia/Bangkok,en,Krung Thep,,,,,,,\n"
        )
    )
    venues = _write(
        tmp_path / "venues.csv",
        "﻿id,name,city,latitude,longitude,website_url\n"  # a BOM, as Excel writes it
        "1,Heaven,Bangkok,13.7,100.5,https://heaven.test/?utm_source=x\n"
        "2,Spartacus,krung thep,not a number,,\n",
    )

    result = load_golden_venues(venues, cities)

    first, second = result["bangkok"]
    assert (first.lat, first.lng, first.website) == (13.7, 100.5, "https://heaven.test")
    assert (second.lat, second.lng, second.website) == (None, None, None)


def test_an_unknown_city_in_a_golden_file_is_an_error_not_a_silent_skip(tmp_path: Path) -> None:
    cities = load_cities(
        _write(tmp_path / "cities.csv", CITIES_HEADER + "bangkok,Bangkok,TH,Thailand,Asia/Bangkok,en,,,,,,,,\n")
    )
    venues = _write(tmp_path / "venues.csv", "id,name,city\n1,Heaven,Pattaya\n")

    with pytest.raises(InputsError, match=r"city 'Pattaya' is not in cities\.csv"):
        load_golden_venues(venues, cities)


def test_a_bad_event_date_names_the_row(tmp_path: Path) -> None:
    cities = load_cities(
        _write(tmp_path / "cities.csv", CITIES_HEADER + "bangkok,Bangkok,TH,Thailand,Asia/Bangkok,en,,,,,,,,\n")
    )
    events = _write(tmp_path / "events.csv", "id,name,city,start_date\n1,Party,Bangkok,14/11/2026\n")

    with pytest.raises(InputsError, match=r"events\.csv row 2: 'start_date' must be a date"):
        load_golden_events(events, cities)


def test_datetimes_from_mysql_are_accepted_as_dates(tmp_path: Path) -> None:
    cities = load_cities(
        _write(tmp_path / "cities.csv", CITIES_HEADER + "bangkok,Bangkok,TH,Thailand,Asia/Bangkok,en,,,,,,,,\n")
    )
    events = _write(tmp_path / "events.csv", "id,name,city,start_date\n1,Party,Bangkok,2026-11-14 00:00:00\n")

    assert load_golden_events(events, cities)["bangkok"][0].occurrences[0].start_date == date(2026, 11, 14)


def test_snapshot_copies_only_the_input_files_that_exist(tmp_path: Path) -> None:
    destination = tmp_path / "snapshot"

    snapshot_inputs(EXAMPLES, destination)

    assert sorted(path.name for path in destination.iterdir()) == [
        "canary.csv",
        "cities.csv",
        "events_golden.csv",
        "venues_golden.csv",
    ]
