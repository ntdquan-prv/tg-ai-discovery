"""Loads a run directory into memory and resolves its drafts into entities, ready for the metrics."""

from dataclasses import dataclass
from pathlib import Path

from evals import store
from evals.inputs import Inputs, load_inputs
from evals.matching import Entity, resolve_entities
from evals.models import (
    CallLog,
    CanaryObservation,
    DraftRow,
    LabelRecord,
    LlmLog,
    RunMeta,
    UrlCheck,
)
from evals.store import RunDir
from tg_discovery.domain.fingerprint import event_fingerprint, venue_keys
from tg_discovery.domain.models import EventDraft, VenueDraft


def draft_keys(row: DraftRow) -> list[str]:
    """Identity keys of one draft (the same keys the labels were exported with)."""
    if row.venue is not None:
        return venue_keys(row.venue, row.city)
    if row.event is not None:
        return [event_fingerprint(row.event, row.city)]
    return []


@dataclass(slots=True)
class RunData:
    meta: RunMeta
    inputs: Inputs
    calls: list[CallLog]
    llm_calls: list[LlmLog]
    drafts: list[DraftRow]
    canary_observations: list[CanaryObservation]
    url_checks: dict[str, UrlCheck]
    labels: list[LabelRecord]
    entities: list[Entity]
    _label_by_key: dict[str, LabelRecord]

    def label_of(self, entity: Entity) -> LabelRecord | None:
        """The admin verdict for an entity, found through any identity key its drafts share with a labelled one."""
        for index in entity.rows:
            for key in draft_keys(self.drafts[index]):
                if key in self._label_by_key:
                    return self._label_by_key[key]
        return None

    def draft_of(self, entity: Entity) -> VenueDraft | EventDraft | None:
        return entity.representative


def build_run_data(
    meta: RunMeta,
    inputs: Inputs,
    calls: list[CallLog],
    llm_calls: list[LlmLog],
    drafts: list[DraftRow],
    canary_observations: list[CanaryObservation],
    url_checks: list[UrlCheck],
    labels: list[LabelRecord],
) -> RunData:
    by_key: dict[str, LabelRecord] = {}
    for record in labels:
        for key in record.keys:
            by_key.setdefault(key, record)
    cities = meta.cities
    return RunData(
        meta=meta,
        inputs=inputs,
        calls=calls,
        llm_calls=llm_calls,
        drafts=drafts,
        canary_observations=canary_observations,
        url_checks={check.url: check for check in url_checks},
        labels=labels,
        entities=resolve_entities(drafts, cities, inputs.venues, inputs.events),
        _label_by_key=by_key,
    )


def load_run_data(run_dir: RunDir, *, inputs_dir: Path | None = None) -> RunData:
    """Load everything a run recorded. Golden inputs come from the snapshot taken at run time unless overridden."""
    meta = run_dir.read_meta()
    inputs = load_inputs(inputs_dir or run_dir.inputs)
    return build_run_data(
        meta=meta,
        inputs=inputs,
        calls=run_dir.read(store.CALLS, CallLog),
        llm_calls=run_dir.read(store.LLM_CALLS, LlmLog),
        drafts=run_dir.read(store.DRAFTS, DraftRow),
        canary_observations=run_dir.read(store.CANARY, CanaryObservation),
        url_checks=run_dir.read(store.URL_CHECKS, UrlCheck),
        labels=run_dir.read(store.LABELS, LabelRecord),
    )
