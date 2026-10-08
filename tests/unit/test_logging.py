import json

import pytest
import structlog

from tg_discovery.logging import REDACTED, configure_logging, redact_secrets


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


def test_json_output_never_contains_the_secret(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", json_logs=True)

    structlog.get_logger("test").info("connecting", database_password="hunter2", host="db")

    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["database_password"] == REDACTED
    assert line["host"] == "db"
    assert line["level"] == "info"
