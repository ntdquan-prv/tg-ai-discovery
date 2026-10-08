"""Blind labelling: export a shuffled sheet without provider names, then import the admins' verdicts.

Admins see only the item (name, place, dates, a quote, one source link). Which providers found it stays in a separate
key file, so nobody is biased towards or against a vendor. A verdict belongs to the whole entity, so an item found by
three providers is labelled once and counts for all three.
"""

import csv
import hashlib
import json
import random
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from evals import store
from evals.data import RunData, draft_keys
from evals.matching import Entity
from evals.metrics import Analysis
from evals.models import LABELS, Label, LabelRecord
from evals.store import RunDir
from tg_discovery.domain.models import EventDraft, VenueDraft
from tg_discovery.domain.urls import registrable_domain

SHEET_COLUMNS: tuple[str, ...] = (
    "item_id",
    "flow",
    "city",
    "name",
    "place",
    "website",
    "dates",
    "quote",
    "source_url",
    "label",
    "labeler",
)
KEY_COLUMNS: tuple[str, ...] = ("item_id", "entity_id", "flow", "city", "providers", "keys")
# Pages on these domains would tell an admin which provider found an item, so they are never shown as a source.
PROVIDER_DOMAINS = frozenset(
    {"openstreetmap.org", "brave.com", "exa.ai", "tavily.com", "perplexity.ai", "parallel.ai", "google.com"}
)
INSTRUCTIONS = """\
Fill the 'label' column of labeling_sheet.csv with exactly one of:

  valid              a real place or event that is in the city and is LGBTQ+ (or clearly LGBTQ+ welcoming)
  not_lgbtq          real, but not LGBTQ+ related
  wrong_place        real, but not in this city
  past_or_cancelled  an event that already happened or was cancelled, or a venue that has closed
  duplicate          the same place or event as another row in the sheet
  spam               not a real place or event (an article, an ad, a listing page)

Optionally put your name in the 'labeler' column. Do not edit any other column. Open each source_url if the row is
unclear: it is where the item was found.
"""


@dataclass(frozen=True, slots=True)
class ExportSummary:
    items: int
    per_provider: dict[str, int]
    sheet: Path


@dataclass(frozen=True, slots=True)
class ImportSummary:
    imported: int
    blank: int
    by_label: dict[str, int]


class LabelError(ValueError):
    """The sheet cannot be imported as it is."""


def _dates(draft: EventDraft) -> str:
    shown = [occurrence.start_date.isoformat() for occurrence in draft.occurrences[:3]]
    extra = len(draft.occurrences) - len(shown)
    return "; ".join(shown) + (f" (+{extra} more)" if extra > 0 else "")


def _neutral_source(draft: VenueDraft | EventDraft) -> str:
    """The first source page that does not give the provider away (OpenStreetMap's own pages, a vendor's domain)."""
    for source in draft.sources:
        if registrable_domain(source.url) not in PROVIDER_DOMAINS:
            return source.url
    return ""


def _describe(draft: VenueDraft | EventDraft) -> dict[str, str]:
    quote = draft.lgbtq.evidence[0].quote if draft.lgbtq and draft.lgbtq.evidence else ""
    source = _neutral_source(draft)
    if isinstance(draft, VenueDraft):
        return {
            "name": draft.name,
            "place": draft.address or draft.city or "",
            "website": draft.website or "",
            "dates": "",
            "quote": quote,
            "source_url": source,
        }
    place = ", ".join(part for part in (draft.venue.name, draft.venue.address, draft.venue.city) if part)
    return {
        "name": draft.name,
        "place": place,
        "website": draft.links.official or draft.links.tickets or "",
        "dates": _dates(draft),
        "quote": quote,
        "source_url": source,
    }


def _entity_keys(data: RunData, entity: Entity) -> list[str]:
    return sorted({key for index in entity.rows for key in draft_keys(data.drafts[index])})


def _item_id(keys: list[str]) -> str:
    """A short id derived from the entity's identity keys, so ids never collide across several exports."""
    return "L" + hashlib.sha1("\n".join(keys).encode("utf-8"), usedforsecurity=False).hexdigest()[:8]


def export_sheet(analysis: Analysis, run_dir: RunDir, *, per_provider: int = 30, seed: int = 7) -> ExportSummary:
    """Sample up to ``per_provider`` unlabelled novel entities per provider and write the blind sheet and its key."""
    data = analysis.data
    rng = random.Random(seed)
    chosen: dict[str, Entity] = {}
    sampled_per_provider: dict[str, int] = {}
    for flow in data.meta.flows:
        for provider in analysis.providers(flow):
            pool = sorted(
                (entity for entity in analysis.novel(flow, provider) if data.label_of(entity) is None),
                key=lambda entity: entity.id,
            )
            sample = rng.sample(pool, min(per_provider, len(pool)))
            sampled_per_provider[f"{flow.value}:{provider}"] = len(sample)
            chosen.update({entity.id: entity for entity in sample})
    items = sorted(chosen.values(), key=lambda entity: entity.id)
    rng.shuffle(items)

    sheet = run_dir.file(store.LABELING_SHEET)
    run_dir.path.mkdir(parents=True, exist_ok=True)
    with (
        sheet.open("w", encoding="utf-8-sig", newline="") as sheet_file,
        run_dir.file(store.LABEL_KEY).open("w", encoding="utf-8", newline="") as key_file,
    ):
        sheet_writer = csv.DictWriter(sheet_file, fieldnames=SHEET_COLUMNS)
        key_writer = csv.DictWriter(key_file, fieldnames=KEY_COLUMNS)
        sheet_writer.writeheader()
        key_writer.writeheader()
        for entity in items:
            if entity.representative is None:
                continue
            keys = _entity_keys(data, entity)
            item_id = _item_id(keys or [entity.id])
            sheet_writer.writerow(
                {
                    "item_id": item_id,
                    "flow": entity.flow.value,
                    "city": entity.city,
                    "label": "",
                    "labeler": "",
                    **_describe(entity.representative),
                }
            )
            key_writer.writerow(
                {
                    "item_id": item_id,
                    "entity_id": entity.id,
                    "flow": entity.flow.value,
                    "city": entity.city,
                    "providers": "|".join(sorted(entity.providers)),
                    "keys": json.dumps(keys),
                }
            )
    run_dir.file("labeling_instructions.txt").write_text(INSTRUCTIONS, encoding="utf-8")
    return ExportSummary(items=len(items), per_provider=sampled_per_provider, sheet=sheet)


def _normalise_label(value: str) -> Label | None:
    cleaned = value.strip().lower().replace(" ", "_").replace("-", "_")
    for label in LABELS:
        if cleaned == label:
            return label
    return None


def import_sheet(run_dir: RunDir, sheet: Path) -> ImportSummary:
    """Merge the verdicts of a filled sheet into the run; rows left blank are skipped, bad labels abort the import."""
    key_path = run_dir.file(store.LABEL_KEY)
    if not key_path.exists():
        msg = f"{key_path} is missing: run label-export first"
        raise LabelError(msg)
    with key_path.open(encoding="utf-8", newline="") as handle:
        key_rows = {row["item_id"]: row for row in csv.DictReader(handle)}
    with sheet.open(encoding="utf-8-sig", newline="") as handle:
        sheet_rows = list(csv.DictReader(handle))

    imported: dict[str, LabelRecord] = {record.item_id: record for record in run_dir.read(store.LABELS, LabelRecord)}
    blank = 0
    problems: list[str] = []
    fresh: list[LabelRecord] = []
    for number, row in enumerate(sheet_rows, start=2):
        raw = (row.get("label") or "").strip()
        if not raw:
            blank += 1
            continue
        label = _normalise_label(raw)
        item_id = (row.get("item_id") or "").strip()
        if label is None:
            problems.append(f"row {number}: '{raw}' is not one of {', '.join(LABELS)}")
        elif item_id not in key_rows:
            problems.append(f"row {number}: item {item_id or '(empty)'} is not in the exported sheet")
        else:
            key = key_rows[item_id]
            fresh.append(
                LabelRecord(
                    item_id=item_id,
                    entity_id=key["entity_id"],
                    keys=json.loads(key["keys"]),
                    label=label,
                    labeler=(row.get("labeler") or "").strip() or None,
                )
            )
    if problems:
        raise LabelError("the sheet has problems, nothing was imported:\n" + "\n".join(problems))
    imported.update({record.item_id: record for record in fresh})
    run_dir.replace(store.LABELS, imported.values())
    return ImportSummary(imported=len(fresh), blank=blank, by_label=dict(Counter(record.label for record in fresh)))
