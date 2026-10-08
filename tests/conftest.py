"""Shared test configuration."""

from pathlib import Path

import pytest

from tg_discovery.config import Settings


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Start every test from an empty directory and a clean environment.

    A developer's ``.env`` and exported credentials must never leak into a test, so settings are only ever built from
    variables the test sets itself.
    """
    monkeypatch.chdir(tmp_path)
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
