"""A run directory: one folder of JSON lines holding everything a run produced, so a report can be rebuilt from it.

Only derived facts are stored: drafts (facts, URLs, quotes of at most 300 characters), call logs and verdicts. Raw
provider responses are never written, which is also what makes ``--replay`` safe for Google-derived providers.
"""

from collections.abc import Iterable
from pathlib import Path

from pydantic import BaseModel

from evals.models import CallLog, RunMeta

META = "run.json"
CALLS = "calls.jsonl"
LLM_CALLS = "llm_calls.jsonl"
DRAFTS = "drafts.jsonl"
CANARY = "canary.jsonl"
URL_CHECKS = "url_checks.jsonl"
LABELS = "labels.jsonl"
LABELING_SHEET = "labeling_sheet.csv"
LABEL_KEY = "label_key.csv"
REPORT = "report.md"


class RunDir:
    def __init__(self, path: Path) -> None:
        self.path = path

    def exists(self) -> bool:
        return (self.path / META).exists()

    @property
    def inputs(self) -> Path:
        return self.path / "inputs"

    def file(self, name: str) -> Path:
        return self.path / name

    def write_meta(self, meta: RunMeta) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        self.file(META).write_text(meta.model_dump_json(indent=2), encoding="utf-8")

    def read_meta(self) -> RunMeta:
        return RunMeta.model_validate_json(self.file(META).read_text(encoding="utf-8"))

    def append(self, name: str, record: BaseModel) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        with self.file(name).open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(record.model_dump_json() + "\n")

    def read[T: BaseModel](self, name: str, model: type[T]) -> list[T]:
        path = self.file(name)
        if not path.exists():
            return []
        with path.open(encoding="utf-8") as handle:
            return [model.model_validate_json(line) for line in handle if line.strip()]

    def replace(self, name: str, records: Iterable[BaseModel]) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        with self.file(name).open("w", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(record.model_dump_json() + "\n")

    def completed_calls(self) -> set[tuple[str, str, str, str]]:
        """Calls that already succeeded, as ``(provider, flow, city, query id)``; a resumed run skips them."""
        return {
            (row.provider, row.flow.value, row.city, row.query_id)
            for row in self.read(CALLS, CallLog)
            if row.outcome == "ok"
        }
