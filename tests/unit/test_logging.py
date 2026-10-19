import json
import logging

import pytest
import structlog

from tg_discovery.logging import REDACTED, configure_logging, mask_text, redact_secrets


def test_credential_keys_are_masked_at_any_depth() -> None:
    event = {
        "event": "provider call",
        "api_key": "abc",
        "headers": {"Authorization": "Bearer xyz", "accept": "application/json"},
        "attempts": [{"token": "t-1", "n": 1}],
    }

    redacted = redact_secrets(None, "info", event)

    assert redacted["event"] == "provider call"
    assert redacted["api_key"] == REDACTED
    assert redacted["headers"] == {"Authorization": REDACTED, "accept": "application/json"}
    assert redacted["attempts"] == [{"token": REDACTED, "n": 1}]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "HTTP Request: GET https://serpapi.com/search?engine=google&q=gay+bars&api_key=6003abc&hl=en",
            "HTTP Request: GET https://serpapi.com/search?engine=google&q=gay+bars&api_key=***&hl=en",
        ),
        ("connect with x-api-key: abc123 now", "connect with x-api-key: *** now"),
        ("Authorization: Bearer abc.def-ghi", "Authorization: Bearer ***"),
        ("sent Bearer xyz to the model", "sent Bearer *** to the model"),
        ("token=t-1&password=hunter2", "token=***&password=***"),
        ('{"api_key": "abc"}', '{"api_key": "***"}'),
        ("nothing to hide at https://example.org/a?b=c", "nothing to hide at https://example.org/a?b=c"),
        ("the token limit was reached", "the token limit was reached"),
        ("", ""),
    ],
)
def test_credentials_written_inside_a_text_are_masked(text: str, expected: str) -> None:
    assert mask_text(text) == expected


def test_token_counts_and_similar_keys_are_not_masked() -> None:
    event = {"input_tokens": 1200, "max_output_tokens": 8192, "accessToken": "t", "api-key": "k", "client_secret": "s"}

    redacted = redact_secrets(None, "info", event)

    assert (redacted["input_tokens"], redacted["max_output_tokens"]) == (1200, 8192)
    assert redacted["accessToken"] == redacted["api-key"] == redacted["client_secret"] == REDACTED


def test_a_log_line_from_another_library_is_masked_too(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", json_logs=True)

    logging.getLogger("some.library").warning("GET https://vendor.example/search?api_key=SECRETKEY&q=x")

    out = capsys.readouterr().out
    assert "SECRETKEY" not in out
    assert "api_key=***" in out


def test_the_http_client_does_not_log_its_request_urls_unless_debugging(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", json_logs=True)
    logging.getLogger("httpx").info('HTTP Request: GET https://vendor.example/search?api_key=SECRETKEY "200 OK"')
    assert "SECRETKEY" not in capsys.readouterr().out
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING

    configure_logging("DEBUG", json_logs=True)
    logging.getLogger("httpx").info('HTTP Request: GET https://vendor.example/search?api_key=SECRETKEY "200 OK"')
    out = capsys.readouterr().out
    assert "HTTP Request" in out  # when debugging the line is back ...
    assert "SECRETKEY" not in out  # ... with the key masked


def test_json_output_never_contains_the_secret(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", json_logs=True)

    structlog.get_logger("test").info("connecting", database_password="hunter2", host="db")

    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["database_password"] == REDACTED
    assert line["host"] == "db"
    assert line["level"] == "info"
