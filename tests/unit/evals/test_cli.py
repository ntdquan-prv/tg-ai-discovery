import csv
import json
import shutil
from pathlib import Path

import pytest

from evals import store
from evals.run import main

EXAMPLES = Path(__file__).resolve().parents[3] / "evals" / "examples"


def _dry_run(results: Path, *extra: str) -> int:
    return main(["run", "--dry-run", "--results", str(results), "--inputs", str(EXAMPLES), *extra])


@pytest.fixture(scope="module")
def dry_run_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One dry run shared by the tests that only read it; tests that change a run work on a copy."""
    results = tmp_path_factory.mktemp("dry-run-results")
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path_factory.mktemp("cwd"))  # never pick up a developer's .env
        assert _dry_run(results) == 0
    return results / "dry-run"


@pytest.fixture
def run_copy(dry_run_dir: Path, tmp_path: Path) -> Path:
    destination = tmp_path / "run"
    shutil.copytree(dry_run_dir, destination)
    return destination


def test_a_dry_run_produces_every_artifact_and_a_complete_report(dry_run_dir: Path) -> None:
    for name in (store.META, store.CALLS, store.LLM_CALLS, store.DRAFTS, store.CANARY, store.URL_CHECKS, store.LABELS):
        assert (dry_run_dir / name).stat().st_size > 0, name
    assert (dry_run_dir / store.LABELING_SHEET).exists()
    assert (dry_run_dir / "inputs" / "cities.csv").exists()
    report = (dry_run_dir / store.REPORT).read_text(encoding="utf-8")
    assert report.startswith("# Bake-off report: dry-run")
    assert "SIMULATED DATA" in report
    for heading in (
        "## Venue flow",
        "## Event flow",
        "### Combinations",
        "## Licensing and price notes",
        "## Metric definitions",
    ):
        assert heading in report
    assert (dry_run_dir / "adr_draft.md").read_text(encoding="utf-8").startswith("# 0001")


def test_a_dry_run_says_it_spends_nothing_and_is_repeatable(
    dry_run_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _dry_run(tmp_path) == 0

    assert "Nothing is sent or spent" in capsys.readouterr().out
    first = (dry_run_dir / store.REPORT).read_text(encoding="utf-8")
    second = (tmp_path / "dry-run" / store.REPORT).read_text(encoding="utf-8")
    assert first == second


def test_a_report_is_rebuilt_from_the_stored_run_without_calling_anyone(run_copy: Path) -> None:
    report = run_copy / store.REPORT
    original = report.read_text(encoding="utf-8")
    report.unlink()

    assert main(["run", "--replay", str(run_copy)]) == 0

    assert report.read_text(encoding="utf-8") == original


def test_the_replay_picks_up_labels_imported_later(run_copy: Path) -> None:
    (run_copy / store.LABELS).unlink()
    assert main(["run", "--replay", str(run_copy)]) == 0
    assert "No admin labels were imported" in (run_copy / store.REPORT).read_text(encoding="utf-8")

    assert main(["label-export", "--run", str(run_copy), "--per-provider", "5"]) == 0
    sheet = run_copy / store.LABELING_SHEET
    with sheet.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        row["label"] = "valid"
    with sheet.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    assert main(["label-import", "--run", str(run_copy), "--file", str(sheet)]) == 0

    assert "No admin labels were imported" not in (run_copy / store.REPORT).read_text(encoding="utf-8")


def test_the_sheet_for_the_admins_names_no_provider(run_copy: Path) -> None:
    main(["label-export", "--run", str(run_copy), "--per-provider", "10"])

    sheet = (run_copy / store.LABELING_SHEET).read_text(encoding="utf-8-sig").lower()
    assert "fake_" not in sheet
    assert "fake_" in (run_copy / store.LABEL_KEY).read_text(encoding="utf-8")


def test_plan_prints_calls_and_prices_and_sends_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["plan", "--inputs", str(EXAMPLES), "--cities", "testville"]) == 0

    out = capsys.readouterr().out
    assert "Nothing was sent." in out
    assert "SKIPPED: EXA_API_KEY is not set" in out  # no key in the test environment
    assert "its terms need review first: pass --accept-terms brave_web" in out
    assert "Vendor terms to read:" in out


def test_plan_prices_exa_at_the_worst_case_the_budget_guard_will_reserve(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXA_API_KEY", "key")

    main(["plan", "--inputs", str(EXAMPLES), "--cities", "testville", "--providers", "exa", "--flows", "venue"])

    line = next(row for row in capsys.readouterr().out.splitlines() if row.strip().startswith("exa"))
    assert "12 calls" in line
    assert "$0.0170" in line  # 0.007 for the search plus ten pages of text at 0.001


def test_max_queries_shrinks_the_plan(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXA_API_KEY", "key")

    main(["plan", "--inputs", str(EXAMPLES), "--cities", "testville", "--providers", "exa", "--max-queries", "2"])

    line = next(row for row in capsys.readouterr().out.splitlines() if row.strip().startswith("exa"))
    assert "4 calls" in line  # two queries, two flows


def test_an_unknown_provider_is_rejected_with_the_known_names(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["plan", "--inputs", str(EXAMPLES), "--providers", "bing"]) == 2

    assert "unknown provider bing; known: brave_web" in capsys.readouterr().err


def test_an_unknown_city_is_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["plan", "--inputs", str(EXAMPLES), "--cities", "atlantis"]) == 2

    assert "unknown city atlantis; cities.csv has testville, exampleburg" in capsys.readouterr().err


def test_a_real_run_without_any_key_names_what_is_missing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["run", "--results", str(tmp_path), "--inputs", str(EXAMPLES), "--run-id", "pilot", "--yes"])

    assert code == 2
    err = capsys.readouterr().err
    assert "no provider can run" in err
    assert "exa: EXA_API_KEY is not set" in err
    assert not (tmp_path / "pilot").exists()


def test_a_real_run_never_starts_without_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("NOMINATIM_USER_AGENT", "tests")  # the OSM provider needs no key beyond this

    code = main(
        [
            "run",
            "--results",
            str(tmp_path),
            "--inputs",
            str(EXAMPLES),
            "--run-id",
            "pilot",
            "--providers",
            "osm_overpass",
        ]
    )

    assert code == 2
    assert "pass --yes to go ahead" in capsys.readouterr().err
    assert not (tmp_path / "pilot").exists()


def test_an_existing_run_is_never_overwritten(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "pilot").mkdir()
    (tmp_path / "pilot" / store.META).write_text(json.dumps({}), encoding="utf-8")

    code = main(["run", "--results", str(tmp_path), "--inputs", str(EXAMPLES), "--run-id", "pilot", "--yes"])

    assert code == 2
    assert "already exists: pass --resume" in capsys.readouterr().err


def test_replay_needs_a_run_directory(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "--replay", str(tmp_path)]) == 2

    assert "is not a run directory" in capsys.readouterr().err
