"""A rehearsal of the pilot: the real adapters, registry, runner, extractor and model client over a mocked network.

Nothing here reaches a vendor. The responses are the hand-written fixtures of the adapter contract tests, and the model
is a small function that "extracts" one record per page it is shown, so the whole path a paid run would take is
exercised: key handling, terms gate, throttling, budget, extraction, call logs, drafts and the report.
"""

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path

import httpx
import pytest
import respx

from evals import store
from evals.models import CallLog, DraftRow, LlmLog, RunMeta
from evals.run import main
from evals.store import RunDir
from tests.support import load_fixture
from tg_discovery.providers import registry

EXAMPLES = Path(__file__).resolve().parents[2] / "evals" / "examples"
BEDROCK = "https://bedrock.test/v1/responses"
KEYS = {
    "BRAVE_API_KEY": "key-brave-123",
    "EXA_API_KEY": "key-exa-123",
    "TAVILY_API_KEY": "key-tavily-123",
    "PERPLEXITY_API_KEY": "key-perplexity-123",
    "PARALLEL_API_KEY": "key-parallel-123",
    "SERPAPI_API_KEY": "key-serpapi-123",
    "AWS_BEDROCK_API_TOKEN": "key-bedrock-123",
    "NOMINATIM_USER_AGENT": "tg-discovery rehearsal",
    "AWS_BEDROCK_OPENAI_URL": "https://bedrock.test/v1",
}
GATED = "brave_web,brave_llm_context,brave_place,parallel,serpapi_events,serpapi_maps"
SERPAPI = "https://serpapi.com/search"


@dataclass(frozen=True, slots=True)
class Vendor:
    """Where a vendor lives on the mocked network and which hand-written fixture answers for it."""

    method: str
    url: str
    folder: str
    fixture: str
    params: dict[str, str] | None = None  # SerpApi's two engines share one URL and differ by ``engine``


VENDORS = {
    "brave_web": Vendor("GET", "https://api.search.brave.com/res/v1/web/search", "brave", "web_search.json"),
    "brave_llm_context": Vendor("GET", "https://api.search.brave.com/res/v1/llm/context", "brave", "llm_context.json"),
    "brave_place": Vendor(
        "GET", "https://api.search.brave.com/res/v1/local/place_search", "brave_place", "gay_bar_berlin.json"
    ),
    "exa": Vendor("POST", "https://api.exa.ai/search", "exa", "search.json"),
    "tavily": Vendor("POST", "https://api.tavily.com/search", "tavily", "search_basic.json"),
    "perplexity_search": Vendor("POST", "https://api.perplexity.ai/search", "perplexity", "search_web.json"),
    "parallel": Vendor("POST", "https://api.parallel.ai/v1/search", "parallel", "search_fast.json"),
    "serpapi_events": Vendor("GET", SERPAPI, "serpapi_events", "events_bangkok.json", params={"engine": "google"}),
    "serpapi_maps": Vendor("GET", SERPAPI, "serpapi_maps", "gay_bar_bangkok.json", params={"engine": "google_maps"}),
    "osm_overpass": Vendor("POST", "https://overpass-api.de/api/interpreter", "osm_overpass", "berlin.json"),
}
STRUCTURED_VENUE_SOURCES = ("osm_overpass", "brave_place", "serpapi_maps")  # drafts come straight from the vendor


@pytest.fixture(autouse=True)
def _rehearsal_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keys for every vendor, and no waiting: the throttles and retry pauses are real-world courtesies."""
    MODEL_REQUESTS.clear()
    for name, value in KEYS.items():
        monkeypatch.setenv(name, value)
    for attribute in ("SEARCH_ENTRIES", "VENUE_ENTRIES"):
        entries = tuple(replace(entry, min_interval_s=0.0, retry_wait_s=0.0) for entry in getattr(registry, attribute))
        monkeypatch.setattr(registry, attribute, entries)


MODEL_REQUESTS: list[str] = []  # every request body the stand-in model received, to check what was sent to it


def _model(request: httpx.Request) -> httpx.Response:
    """A stand-in for the extraction model: one record per page it was shown, citing that page."""
    MODEL_REQUESTS.append(request.content.decode())
    body = json.loads(request.content)
    urls = re.findall(r"^URL: (.+)$", body["input"], flags=re.MULTILINE)
    if "extract venues" in body["instructions"]:
        arguments: dict[str, object] = {
            "venues": [
                {
                    "name": f"Mock Venue {number}",
                    "city": "Testville",
                    "address": f"{number} Mock Street",
                    "lgbtq": {"level": "primary"},
                    "source_urls": [url],
                }
                for number, url in enumerate(urls[:2], start=1)
            ]
        }
    else:
        arguments = {
            "events": [
                {
                    "name": f"Mock Party {number}",
                    "occurrences": [{"start_date": "2026-11-14"}],
                    "venue_name": "Mock Hall",
                    "lgbtq": {"level": "primary"},
                    "source_urls": [url],
                }
                for number, url in enumerate(urls[:2], start=1)
            ]
        }
    output = [{"type": "function_call", "name": "emit_candidates", "call_id": "c1", "arguments": json.dumps(arguments)}]
    return httpx.Response(
        200, json={"status": "completed", "output": output, "usage": {"input_tokens": 1000, "output_tokens": 200}}
    )


def _network(router: respx.MockRouter, *, failing: str | None = None) -> dict[str, respx.Route]:
    routes: dict[str, respx.Route] = {}
    for name, vendor in VENDORS.items():
        response = (
            httpx.Response(500, text="boom")
            if name == failing
            else httpx.Response(200, json=load_fixture(vendor.folder, vendor.fixture))
        )
        route = (
            router.route(method=vendor.method, url=vendor.url, params=vendor.params)
            if vendor.params
            else router.route(method=vendor.method, url=vendor.url)
        )
        routes[name] = route.mock(return_value=response)
    routes["bedrock"] = router.post(BEDROCK).mock(side_effect=_model)
    return routes


def _pilot(tmp_path: Path, *extra: str) -> int:
    return main(
        [
            "run",
            "--results", str(tmp_path),
            "--inputs", str(EXAMPLES),
            "--cities", "testville",
            "--run-id", "rehearsal",
            "--yes",
            "--no-local-queries",
            *extra,
        ]
    )  # fmt: skip


def test_every_vendor_is_called_and_the_run_is_recorded(tmp_path: Path) -> None:
    with respx.mock(assert_all_called=False) as router:
        routes = _network(router)

        assert _pilot(tmp_path, "--accept-terms", GATED) == 0

    assert [name for name, route in routes.items() if not route.called] == []
    run = RunDir(tmp_path / "rehearsal")
    calls = run.read(store.CALLS, CallLog)
    assert {call.provider for call in calls} == set(VENDORS)
    assert {call.outcome for call in calls} == {"ok"}
    drafts = run.read(store.DRAFTS, DraftRow)
    assert {row.provider for row in drafts} == set(VENDORS)  # search providers through the model, the others directly
    llm = run.read(store.LLM_CALLS, LlmLog)
    search_calls = [call for call in calls if call.provider not in STRUCTURED_VENUE_SOURCES]
    assert len(llm) == len(search_calls)
    assert all(entry.usage is not None and entry.usage.input_tokens == 1000 for entry in llm)
    meta = run.read_meta()
    assert [info.name for info in meta.providers if info.skipped_reason] == []
    report = (tmp_path / "rehearsal" / store.REPORT).read_text(encoding="utf-8")
    assert "## Venue flow" in report
    assert "## Event flow" in report
    assert "SIMULATED" not in report


def test_each_vendor_receives_its_own_key_and_nothing_else_does(tmp_path: Path) -> None:
    with respx.mock(assert_all_called=False) as router:
        routes = _network(router)
        _pilot(tmp_path, "--accept-terms", GATED)

    assert routes["brave_web"].calls.last.request.headers["x-subscription-token"] == KEYS["BRAVE_API_KEY"]
    assert routes["exa"].calls.last.request.headers["x-api-key"] == KEYS["EXA_API_KEY"]
    assert routes["bedrock"].calls.last.request.headers["authorization"] == f"Bearer {KEYS['AWS_BEDROCK_API_TOKEN']}"
    assert routes["osm_overpass"].calls.last.request.headers["user-agent"] == KEYS["NOMINATIM_USER_AGENT"]
    for name in ("serpapi_events", "serpapi_maps"):
        assert routes[name].calls.last.request.url.params["api_key"] == KEYS["SERPAPI_API_KEY"]
    assert MODEL_REQUESTS
    prompts = "".join(MODEL_REQUESTS)
    assert not any(secret in prompts for secret in KEYS.values() if secret.startswith("key-"))


def test_no_credential_is_ever_written_to_disk(tmp_path: Path) -> None:
    with respx.mock(assert_all_called=False) as router:
        _network(router)
        _pilot(tmp_path, "--accept-terms", GATED)

    secrets = [value for value in KEYS.values() if value.startswith("key-")]
    for path in (tmp_path / "rehearsal").rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            assert not any(secret in text for secret in secrets), path.name


def test_vendors_whose_terms_were_not_accepted_are_never_called(tmp_path: Path) -> None:
    with respx.mock(assert_all_called=False) as router:
        routes = _network(router)

        assert _pilot(tmp_path) == 0

    for name in GATED.split(","):
        assert not routes[name].called, name
    assert all(routes[name].called for name in ("exa", "tavily", "perplexity_search", "osm_overpass"))
    meta = RunDir(tmp_path / "rehearsal").read_meta()
    skipped = {info.name: info.skipped_reason for info in meta.providers if info.skipped_reason}
    assert set(skipped) == set(GATED.split(","))
    assert all("terms need review" in reason for reason in skipped.values() if reason)
    report = (tmp_path / "rehearsal" / store.REPORT).read_text(encoding="utf-8")
    assert "Brave API terms of 1 Sep 2026" in report


def test_one_failing_vendor_does_not_lose_the_run(tmp_path: Path) -> None:
    with respx.mock(assert_all_called=False) as router:
        _network(router, failing="tavily")

        assert _pilot(tmp_path, "--accept-terms", GATED) == 0

    run = RunDir(tmp_path / "rehearsal")
    by_outcome: dict[tuple[str, str], int] = {}
    for call in run.read(store.CALLS, CallLog):
        by_outcome[(call.provider, call.outcome)] = by_outcome.get((call.provider, call.outcome), 0) + 1
    assert by_outcome[("tavily", "error")] == 24
    assert ("tavily", "ok") not in by_outcome
    assert by_outcome[("exa", "ok")] == 24
    assert "`tavily` failed 100%" in (tmp_path / "rehearsal" / store.REPORT).read_text(encoding="utf-8")


def test_the_budget_stops_the_run_and_resume_finishes_it_without_repeating_calls(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with respx.mock(assert_all_called=False) as router:
        _network(router)
        assert _pilot(tmp_path, "--accept-terms", GATED, "--budget-usd", "0.2") == 0
        run = RunDir(tmp_path / "rehearsal")
        first = len(run.read(store.CALLS, CallLog))
        assert run.read_meta().stopped_by_budget
        assert "ran out" in capsys.readouterr().out

        assert _pilot(tmp_path, "--accept-terms", GATED, "--budget-usd", "5", "--resume") == 0

    calls = run.read(store.CALLS, CallLog)
    assert len(calls) > first
    finished = [(c.provider, c.flow, c.city, c.query_id) for c in calls if c.outcome == "ok"]
    assert len(finished) == len(set(finished))  # nothing was asked twice
    assert not run.read_meta().stopped_by_budget


def test_a_smoke_test_asks_only_the_first_queries(tmp_path: Path) -> None:
    with respx.mock(assert_all_called=False) as router:
        _network(router)

        assert _pilot(tmp_path, "--providers", "exa,osm_overpass", "--max-queries", "2") == 0

    calls = RunDir(tmp_path / "rehearsal").read(store.CALLS, CallLog)
    exa = sorted((c.flow.value, c.query_id) for c in calls if c.provider == "exa")
    assert exa == [("event", "e01"), ("event", "e02"), ("venue", "v01"), ("venue", "v02")]
    assert [c.query_id for c in calls if c.provider == "osm_overpass"] == ["area"]  # asked once per city regardless


def test_spend_never_exceeds_the_budget(tmp_path: Path) -> None:
    with respx.mock(assert_all_called=False) as router:
        _network(router)
        _pilot(tmp_path, "--accept-terms", GATED, "--budget-usd", "0.3")

    run = RunDir(tmp_path / "rehearsal")
    spent = sum(call.call.cost_usd for call in run.read(store.CALLS, CallLog))
    spent += sum(entry.call.cost_usd for entry in run.read(store.LLM_CALLS, LlmLog) if entry.call is not None)
    assert 0 < spent <= 0.3


def test_the_run_metadata_records_prices_and_the_extractor(tmp_path: Path) -> None:
    with respx.mock(assert_all_called=False) as router:
        _network(router)
        _pilot(tmp_path, "--accept-terms", GATED)

    meta: RunMeta = RunDir(tmp_path / "rehearsal").read_meta()
    assert meta.extractor_model == "openai.gpt-oss-120b"
    prices = {info.name: info.usd_per_unit for info in meta.providers}
    assert prices["osm_overpass"] == 0
    assert prices["exa"] == pytest.approx(0.007)
    assert not meta.dry_run
