import csv
import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from evals import store
from evals.data import RunData, build_run_data
from evals.labels import LabelError, export_sheet, import_sheet
from evals.metrics import Analysis, compute_metrics
from evals.models import DraftRow, LabelRecord
from evals.store import RunDir
from tests.evals_builders import NOW, call_log, info, make_data, venue_row
from tg_discovery.domain.enums import Flow
from tg_discovery.domain.models import SourceRef

PROVIDERS = [info("exa"), info("brave")]
WORDS: Sequence[str] = """
Aardvark Bluebird Cinnamon Dragonfly Eclipse Firefly Gazelle Hummingbird Iguana Jackal
Kingfisher Lemur Mongoose Nightjar Orchid Pelican Quokka Raccoon Starling Toucan
""".split()  # noqa: SIM905 - one word per entry is easier to read than a quoted list


def _places(provider: str, words: Sequence[str]) -> list[DraftRow]:
    """Distinct venues several kilometres apart, so the matcher never merges them."""
    lng = 20.03 if provider == "brave" else 20.0
    return [venue_row(provider, f"{word} Club", lat=10.0 + 0.02 * index, lng=lng) for index, word in enumerate(words)]


def _rows() -> list[DraftRow]:
    shared = [venue_row(p, "Zebra Club", lat=10.09, lng=20.09) for p in ("exa", "brave")]
    return [*_places("exa", WORDS[:8]), *_places("brave", WORDS[8:16]), *shared]


def _data(rows: list[DraftRow], labels: list[LabelRecord] | None = None) -> RunData:
    return make_data(providers=PROVIDERS, calls=[call_log("exa"), call_log("brave")], drafts=rows, labels=labels or [])


def _read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _write(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_the_sheet_hides_providers_and_the_key_file_records_them(tmp_path: Path) -> None:
    run_dir = RunDir(tmp_path)

    summary = export_sheet(Analysis(_data(_rows())), run_dir, per_provider=4, seed=1)

    sheet = _read(run_dir.file(store.LABELING_SHEET))
    key = _read(run_dir.file(store.LABEL_KEY))
    assert summary.items == len(sheet) == len(key)
    text = run_dir.file(store.LABELING_SHEET).read_text(encoding="utf-8-sig").lower()
    assert "exa" not in text
    assert "brave" not in text
    assert {row["providers"] for row in key} <= {"exa", "brave", "brave|exa"}
    assert len({row["item_id"] for row in sheet}) == len(sheet)
    assert all(row["label"] == "" for row in sheet)
    assert (tmp_path / "labeling_instructions.txt").exists()


def test_at_most_the_requested_number_of_items_per_provider_is_sampled(tmp_path: Path) -> None:
    summary = export_sheet(Analysis(_data(_rows())), RunDir(tmp_path), per_provider=3, seed=1)

    assert summary.per_provider == {"venue:exa": 3, "venue:brave": 3}
    assert 3 <= summary.items <= 6


def test_the_same_seed_gives_the_same_sample(tmp_path: Path) -> None:
    first = RunDir(tmp_path / "a")
    second = RunDir(tmp_path / "b")

    export_sheet(Analysis(_data(_rows())), first, per_provider=4, seed=5)
    export_sheet(Analysis(_data(_rows())), second, per_provider=4, seed=5)

    assert first.file(store.LABELING_SHEET).read_bytes() == second.file(store.LABELING_SHEET).read_bytes()


def test_import_attaches_verdicts_that_feed_the_precision_metric(tmp_path: Path) -> None:
    run_dir = RunDir(tmp_path)
    data = _data(_places("exa", WORDS[:4]))
    export_sheet(Analysis(data), run_dir, per_provider=10, seed=1)
    sheet_path = run_dir.file(store.LABELING_SHEET)
    sheet = _read(sheet_path)
    verdicts = ["valid", "Wrong Place", "not-lgbtq", ""]  # forgiving spellings; the last row stays blank
    for row, verdict in zip(sheet, verdicts, strict=True):
        row["label"] = verdict
        row["labeler"] = "Sam"
    _write(sheet_path, sheet)

    summary = import_sheet(run_dir, sheet_path)

    assert (summary.imported, summary.blank) == (3, 1)
    assert summary.by_label == {"valid": 1, "wrong_place": 1, "not_lgbtq": 1}
    labels = run_dir.read(store.LABELS, LabelRecord)
    assert {record.labeler for record in labels} == {"Sam"}
    relabelled = build_run_data(data.meta, data.inputs, data.calls, [], data.drafts, [], [], labels)
    metrics = compute_metrics(Analysis(relabelled), Flow.VENUE, "exa")
    assert (metrics.labeled_novel, metrics.precision_novel) == (3, pytest.approx(1 / 3))


def test_a_label_survives_when_a_resumed_run_adds_drafts_of_the_same_place(tmp_path: Path) -> None:
    run_dir = RunDir(tmp_path)
    rows = [venue_row("exa", "Heaven Bar", lat=10.0, lng=20.0, website="https://heaven.test")]
    export_sheet(Analysis(_data(rows)), run_dir, per_provider=5, seed=1)
    sheet_path = run_dir.file(store.LABELING_SHEET)
    sheet = _read(sheet_path)
    sheet[0]["label"] = "valid"
    _write(sheet_path, sheet)
    import_sheet(run_dir, sheet_path)

    # Later another provider finds the same place and more places appear, so the numbered entity ids all shift.
    more = [
        venue_row("brave", "Aardvark Club", lat=10.07, lng=20.07),
        *rows,
        venue_row("brave", "Heaven", lat=10.0, lng=20.0, website="https://heaven.test"),
    ]
    analysis = Analysis(_data(more, run_dir.read(store.LABELS, LabelRecord)))

    heaven = [e for e in analysis.data.entities if e.providers == frozenset({"exa", "brave"})]
    assert len(heaven) == 1
    assert analysis.is_valid(heaven[0]) is True
    aardvark = next(
        e for e in analysis.data.entities if e.representative is not None and e.representative.name == "Aardvark Club"
    )
    assert analysis.is_valid(aardvark) is None


def test_labelled_items_are_not_sampled_again(tmp_path: Path) -> None:
    run_dir = RunDir(tmp_path)
    rows = _places("exa", WORDS[:4])
    export_sheet(Analysis(_data(rows)), run_dir, per_provider=2, seed=1)
    sheet_path = run_dir.file(store.LABELING_SHEET)
    sheet = _read(sheet_path)
    for row in sheet:
        row["label"] = "valid"
    _write(sheet_path, sheet)
    import_sheet(run_dir, sheet_path)
    done = {row["item_id"] for row in sheet}

    second = export_sheet(
        Analysis(_data(rows, run_dir.read(store.LABELS, LabelRecord))), run_dir, per_provider=2, seed=2
    )

    assert second.items == 2
    assert not done & {row["item_id"] for row in _read(run_dir.file(store.LABELING_SHEET))}
    assert len(run_dir.read(store.LABELS, LabelRecord)) == 2


def test_a_bad_label_aborts_the_whole_import_and_names_the_row(tmp_path: Path) -> None:
    run_dir = RunDir(tmp_path)
    export_sheet(Analysis(_data(_places("exa", WORDS[:2]))), run_dir, per_provider=5, seed=1)
    sheet_path = run_dir.file(store.LABELING_SHEET)
    sheet = _read(sheet_path)
    sheet[0]["label"] = "valid"
    sheet[1]["label"] = "great place"
    _write(sheet_path, sheet)

    with pytest.raises(LabelError, match=r"row 3: 'great place' is not one of"):
        import_sheet(run_dir, sheet_path)

    assert run_dir.read(store.LABELS, LabelRecord) == []


def test_importing_without_an_export_says_what_to_do(tmp_path: Path) -> None:
    with pytest.raises(LabelError, match="run label-export first"):
        import_sheet(RunDir(tmp_path), tmp_path / "sheet.csv")


def test_a_source_page_that_gives_the_provider_away_is_not_shown(tmp_path: Path) -> None:
    run_dir = RunDir(tmp_path)
    own_page = venue_row("exa", "Aardvark Club", lat=10.0, lng=20.0)
    assert own_page.venue is not None
    own_page.venue.sources = [
        SourceRef(url="https://www.openstreetmap.org/node/42", provider="exa", retrieved_at=NOW),
        SourceRef(url="https://guide.test/clubs", provider="exa", retrieved_at=NOW),
    ]
    only_own = venue_row("exa", "Bluebird Club", lat=10.05, lng=20.0)
    assert only_own.venue is not None
    only_own.venue.sources = [SourceRef(url="https://www.openstreetmap.org/node/43", provider="exa", retrieved_at=NOW)]

    export_sheet(Analysis(_data([own_page, only_own])), run_dir, per_provider=5, seed=1)

    sources = {row["name"]: row["source_url"] for row in _read(run_dir.file(store.LABELING_SHEET))}
    assert sources == {"Aardvark Club": "https://guide.test/clubs", "Bluebird Club": ""}


def test_the_key_file_carries_the_identity_keys_as_json(tmp_path: Path) -> None:
    run_dir = RunDir(tmp_path)

    export_sheet(Analysis(_data([venue_row("exa", "Heaven", lat=10.0, lng=20.0)])), run_dir, per_provider=5, seed=1)

    (row,) = _read(run_dir.file(store.LABEL_KEY))
    keys = json.loads(row["keys"])
    assert keys[0].startswith("testville|ng:heaven|")
