import pytest

from tg_discovery.domain.contacts import clean_email, email_in_text, is_role_email, phone_digits, phone_in_text


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Info@GenderFunk.vn", "info@genderfunk.vn"),
        ("mailto:booking@club.example ", "booking@club.example"),
        ("not an address", None),
        ("info@nodot", None),
        ("", None),
        (None, None),
    ],
)
def test_an_address_is_kept_in_lower_case_only_when_it_looks_like_one(value: str | None, expected: str | None) -> None:
    assert clean_email(value) == expected


@pytest.mark.parametrize(
    ("email", "shared"),
    [
        ("info@genderfunk.vn", True),
        ("booking.hcm@club.example", True),
        ("events-team@pride.example", True),
        ("hello@collective.example", True),
        ("jane.doe@gmail.com", False),  # a person's own address
        ("dragqueen99@yahoo.com", False),
        ("information.desk.jane@x.example", False),  # the first word must be a mailbox name, not start with one
    ],
)
def test_only_a_shared_mailbox_counts_as_an_organisations_address(email: str, shared: bool) -> None:
    assert is_role_email(email) is shared


def test_a_phone_number_is_found_in_a_page_however_its_digits_are_grouped() -> None:
    digits = phone_digits("+84 28 3823 4567")

    assert digits == "842838234567"
    assert phone_in_text(digits, "Call us: (+84) 28-3823-4567 any day")
    assert not phone_in_text(digits, "Call us: +84 28 3823 0000")
    assert phone_digits("ext. 12") is None  # too few digits to be a number


def test_digits_of_neighbouring_numbers_never_combine_into_a_phone_number() -> None:
    page = "Tickets 250000 VND on 16/10, doors 21:00. Call 0903 123 456."

    assert phone_in_text("0903123456", page)
    assert not phone_in_text("2500001610", page)  # a price and a date next to each other
    assert not phone_in_text("16102100", page)


def test_a_mailto_prefix_is_removed_whatever_its_case() -> None:
    assert clean_email("Mailto:Info@Club.example") == "info@club.example"


def test_an_address_is_found_in_a_page_whatever_its_case() -> None:
    assert email_in_text("info@genderfunk.vn", "Contact: INFO@GenderFunk.vn")
    assert not email_in_text("info@genderfunk.vn", "Contact us on Facebook")
