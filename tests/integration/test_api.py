"""The whole API over a real Postgres, with the providers replaced by a script: start a run, review, decide."""

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from structlog.testing import capture_logs

from tests.pipeline_builders import event, venue
from tg_discovery.config import load_settings
from tg_discovery.domain.enums import Flow, VenueType
from tg_discovery.domain.geo import BoundingBox
from tg_discovery.main import create_app
from tg_discovery.pipelines.common.budget import Budget
from tg_discovery.pipelines.discovery import CityBatch, CityRequest, CityTarget, RunEnvironment
from tg_discovery.store.models import AuditLog, DiscoveryRun, PublishOutbox

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_database")]

TOKEN = "service-token-for-tests"
SOON = date.today() + timedelta(days=10)
BODY: dict[str, Any] = {
    "country": "ZZ",
    "country_name": "Testland",
    "cities": [{"name": "Testville", "timezone": "Etc/UTC", "lat": 10.0, "lng": 20.0, "tg_city_id": 7}],
}


class ScriptedCollector:
    """Answers every city with the same drafts, or fails, instead of calling vendors."""

    def __init__(self, *, venues: list[Any] | None = None, events: list[Any] | None = None, fail: bool = False) -> None:
        self.venues = venues or []
        self.events = events or []
        self.fail = fail
        self.venue_types: list[VenueType] = []  # what the last venue run asked for

    async def collect(
        self,
        flow: Flow,
        city: CityTarget,
        *,
        window_days: int,
        today: date,
        venue_types: Sequence[VenueType] = (),
    ) -> CityBatch:
        self.venue_types = list(venue_types)
        if self.fail:
            msg = "the providers are down"
            raise RuntimeError(msg)
        return CityBatch(city=city, venues=list(self.venues), events=list(self.events))


class ScriptedGeocoder:
    async def locate(self, request: CityRequest, country: str) -> BoundingBox | None:
        if request.name == "Nowhere":
            return None
        return BoundingBox.around(request.lat or 10.0, request.lng or 20.0, 15)


def two_events_and_a_past_one() -> ScriptedCollector:
    return ScriptedCollector(
        events=[
            (
                "exa",
                event(
                    "Drag Brunch",
                    start=SOON,
                    price="Free",
                    organizer="Brunch Co",
                    images=["https://cdn.test/posters/brunch.jpg", "https://cdn.test/gallery/1.jpg"],
                ).model_copy(update={"organizer_email": "info@brunch.test", "organizer_url": "https://brunch.test"}),
            ),
            ("serpapi_events", event("Drag Brunch", start=SOON, official=None, provider="serpapi_events")),
            ("exa", event("Pride Picnic", start=SOON + timedelta(days=3), venue_name="The Park", address=None)),
            ("exa", event("Last Week's Party", start=date.today() - timedelta(days=7))),
        ]
    )


@asynccontextmanager
async def api(
    database_url: str, monkeypatch: pytest.MonkeyPatch, collector: ScriptedCollector, *, token: str | None = None
) -> AsyncGenerator[httpx.AsyncClient]:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("APP_ENV", "test")
    if token:
        monkeypatch.setenv("SERVICE_TOKEN", token)

    @asynccontextmanager
    async def open_environment() -> AsyncGenerator[RunEnvironment]:
        yield RunEnvironment(collector=collector, geocoder=ScriptedGeocoder(), budget=Budget(1.0), providers=["exa"])

    app = create_app(load_settings(), open_environment=open_environment, inline_runs=True)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test/api/v1") as client:
            yield client


async def start(client: httpx.AsyncClient, flow: str = "events", **changes: Any) -> dict[str, Any]:
    response = await client.post(f"/{flow}/runs", json={**BODY, **changes})
    assert response.status_code == 202, response.text
    return response.json()


async def test_a_run_finds_scores_and_stores_candidates_for_review(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with api(database_url, monkeypatch, two_events_and_a_past_one()) as client:
        accepted = await start(client)

        assert accepted["deduplicated"] is False
        assert accepted["code"].startswith(f"DSC-{datetime.now(UTC):%Y%m%d}-")
        run = (await client.get(f"/runs/{accepted['run_id']}")).json()
        assert run["status"] == "succeeded"
        assert run["flow"] == "event"
        assert run["stats"]["candidates"] == {"new": 2, "changed": 0, "unchanged": 0, "rejected": {"expired": 1}}
        assert run["stats"]["providers"] == ["exa"]
        results = (await client.get(f"/runs/{accepted['run_id']}/results")).json()
        assert [item["name"] for item in results["items"]] == ["Drag Brunch", "Pride Picnic"]
        assert results["total"] == 2
        assert results["next_offset"] is None
        first = results["items"][0]
        assert first["city"] == "Testville"
        assert (first["next_date"], first["time"]) == (SOON.isoformat(), "21:00")
        assert first["contact"] == {
            "name": "Brunch Co",
            "phone": None,
            "email": "info@brunch.test",
            "url": "https://brunch.test",
        }
        assert first["found_by"] == ["exa", "serpapi_events"]
        assert first["status"] == "needs_review"
        queue = (await client.get("/events/candidates")).json()
        assert queue["total"] == 2


async def test_a_run_reports_how_long_it_took_and_logs_it(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    async with api(database_url, monkeypatch, two_events_and_a_past_one()) as client:
        with capture_logs() as logs:  # after the app is built: building it configures the logging again
            accepted = await start(client)
        run = (await client.get(f"/runs/{accepted['run_id']}")).json()

    assert isinstance(run["duration_s"], float)
    assert run["duration_s"] >= 0
    timing = run["stats"]["timing"]
    assert timing["total_s"] >= 0
    (city,) = timing["cities"]
    assert city["city"] == "Testville"
    assert {"seconds", "locate_s", "collect_s", "merge_s", "save_s", "busy"} <= set(city)
    events = {entry["event"]: entry for entry in logs if entry["event"] in {"run started", "city done", "run finished"}}
    assert list(events) == ["run started", "city done", "run finished"]
    assert events["run started"]["cities"] == ["Testville"]
    assert events["city done"]["city"] == "Testville"
    assert events["city done"]["candidates"] == 2
    assert events["city done"]["rejected"] == 1
    assert events["run finished"]["status"] == "succeeded"
    assert events["run finished"]["seconds"] >= 0
    assert events["run finished"]["run"] == accepted["code"]


async def test_the_review_packet_has_the_reasons_the_doubts_and_the_place_in_the_run(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with api(database_url, monkeypatch, two_events_and_a_past_one()) as client:
        accepted = await start(client)
        rows = (await client.get(f"/runs/{accepted['run_id']}/results")).json()["items"]

        packet = (await client.get(f"/events/candidates/{rows[0]['id']}", params={"run_id": accepted["run_id"]})).json()

        assert packet["flow"] == "event"
        assert packet["run"] == {"id": accepted["run_id"], "code": accepted["code"]}
        assert packet["position"] == {"index": 1, "total": 2, "prev_id": None, "next_id": rows[1]["id"]}
        assert packet["event"]["name"] == "Drag Brunch"
        assert packet["event"]["venue"]["name"] == "Rainbow Bar"
        assert packet["event"]["tickets"] == {"kind": "free", "price_text": "Free", "note": None, "url": None}
        assert packet["event"]["occurrences"][0]["start_time"] == "21:00"
        assert packet["event"]["images"] == [
            {
                "url": "https://cdn.test/posters/brunch.jpg",
                "kind": None,
                "source_url": "https://src.test/page",
                "alt": None,
            },
            {"url": "https://cdn.test/gallery/1.jpg", "kind": None, "source_url": "https://src.test/page", "alt": None},
        ]
        assert packet["lgbtq_level"] == "primary"
        assert packet["why_relevant"][0]["quote"] == "A bar for the community"
        assert packet["scores"]["total"] > 0.5
        assert set(packet["scores"]["parts"]) == {
            "time_validity",
            "lgbtq_relevance",
            "location_match",
            "verifiability",
            "completeness",
            "source_trust",
        }
        assert packet["sources"][0]["label"] == "src.test"
        assert packet["review"]["can"] == ["approve", "reject"]
        assert packet["history"]["seen_count"] == 1
        second = (await client.get(f"/events/candidates/{rows[1]['id']}", params={"run_id": accepted["run_id"]})).json()
        assert second["position"] == {"index": 2, "total": 2, "prev_id": rows[0]["id"], "next_id": None}
        assert "Only one source lists it." in second["ai_review"]["cons"]
        assert second["event"]["images"] == []


async def test_approving_queues_the_candidate_and_a_second_decision_is_refused(
    database_url: str, monkeypatch: pytest.MonkeyPatch, engine: AsyncEngine
) -> None:
    async with api(database_url, monkeypatch, two_events_and_a_past_one()) as client:
        accepted = await start(client)
        row = (await client.get(f"/runs/{accepted['run_id']}/results")).json()["items"][0]

        stale = await client.post(f"/events/candidates/{row['id']}/approve", json={"version": 99})
        approved = await client.post(
            f"/events/candidates/{row['id']}/approve", json={"version": 1, "note": "Looks right"}
        )
        again = await client.post(f"/events/candidates/{row['id']}/approve", json={"version": 2})
        packet = (await client.get(f"/events/candidates/{row['id']}")).json()
        queue = (await client.get("/events/candidates")).json()

        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "version_conflict"
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"
        assert (approved.json()["version"], approved.json()["queued_for_publishing"]) == (2, True)
        assert approved.json()["reviewed_by"] == "dev"
        assert again.status_code == 409
        assert again.json()["detail"]["code"] == "already_decided"
        assert packet["review"]["can"] == []
        assert packet["review"]["note"] == "Looks right"
        assert queue["total"] == 1
    async with AsyncSession(engine) as session:
        assert await session.scalar(select(func.count()).select_from(PublishOutbox)) == 1
        audit = (await session.scalars(select(AuditLog))).all()
        assert [(entry.actor, entry.action) for entry in audit] == [("dev", "approve")]


async def test_rejecting_needs_a_reason_and_queues_nothing(
    database_url: str, monkeypatch: pytest.MonkeyPatch, engine: AsyncEngine
) -> None:
    async with api(database_url, monkeypatch, two_events_and_a_past_one()) as client:
        accepted = await start(client)
        row = (await client.get(f"/runs/{accepted['run_id']}/results")).json()["items"][0]

        invalid = await client.post(f"/events/candidates/{row['id']}/reject", json={"version": 1, "reason": "boring"})
        rejected = await client.post(
            f"/events/candidates/{row['id']}/reject", json={"version": 1, "reason": "past_or_cancelled"}
        )
        missing = await client.post(f"/events/candidates/{uuid4()}/reject", json={"version": 1, "reason": "spam"})

        assert invalid.status_code == 422
        assert (rejected.status_code, rejected.json()["status"]) == (200, "rejected")
        assert rejected.json()["queued_for_publishing"] is False
        assert missing.status_code == 404
    async with AsyncSession(engine) as session:
        assert await session.scalar(select(func.count()).select_from(PublishOutbox)) == 0


async def test_the_same_request_is_answered_by_the_earlier_run_unless_forced(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with api(database_url, monkeypatch, two_events_and_a_past_one()) as client:
        first = await start(client)
        second = await start(client)
        forced = await start(client, force=True)
        other_flow = await start(client, "venues")

        assert second["deduplicated"] is True
        assert second["run_id"] == first["run_id"]
        assert forced["deduplicated"] is False
        assert forced["run_id"] != first["run_id"]
        assert forced["code"].endswith("-002")
        assert other_flow["run_id"] not in (first["run_id"], forced["run_id"])  # the venue flow has runs of its own
        runs = (await client.get("/runs", params={"flow": "event"})).json()
        assert [run["code"] for run in runs] == [forced["code"], first["code"]]


async def test_finding_a_candidate_again_keeps_the_decision_and_counts_the_sighting(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with api(database_url, monkeypatch, two_events_and_a_past_one()) as client:
        first = await start(client)
        row = (await client.get(f"/runs/{first['run_id']}/results")).json()["items"][0]
        await client.post(f"/events/candidates/{row['id']}/approve", json={"version": 1})

        second = await start(client, force=True)

        run = (await client.get(f"/runs/{second['run_id']}")).json()
        assert run["stats"]["candidates"]["new"] == 0
        assert run["stats"]["candidates"]["unchanged"] == 2
        packet = (await client.get(f"/events/candidates/{row['id']}")).json()
        assert packet["status"] == "approved"
        assert packet["history"]["seen_count"] == 2
        unchanged = (await client.get(f"/runs/{second['run_id']}/results", params={"outcome": "unchanged"})).json()
        assert unchanged["total"] == 2
        assert (await client.get(f"/runs/{second['run_id']}/results", params={"outcome": "new"})).json()["total"] == 0


async def test_a_later_run_adds_pictures_to_a_candidate_quietly(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    collector = ScriptedCollector(events=[("exa", event("Drag Brunch", start=SOON))])
    async with api(database_url, monkeypatch, collector) as client:
        first = await start(client)
        row = (await client.get(f"/runs/{first['run_id']}/results")).json()["items"][0]
        assert (await client.get(f"/events/candidates/{row['id']}")).json()["event"]["images"] == []

        collector.events = [("exa", event("Drag Brunch", start=SOON, images=["https://cdn.test/a.jpg"]))]
        second = await start(client, force=True)
        collector.events = [
            ("exa", event("Drag Brunch", start=SOON, images=["https://cdn.test/a.jpg", "https://cdn.test/b.jpg"]))
        ]
        third = await start(client, force=True)

        packet = (await client.get(f"/events/candidates/{row['id']}")).json()
        assert [image["url"] for image in packet["event"]["images"]] == [
            "https://cdn.test/a.jpg",
            "https://cdn.test/b.jpg",
        ]
        assert packet["version"] == 1  # a picture is not a material change: nobody has to look at it again
        for run_id in (second["run_id"], third["run_id"]):
            unchanged = (await client.get(f"/runs/{run_id}/results", params={"outcome": "unchanged"})).json()
            assert unchanged["total"] == 1


async def test_pictures_are_not_added_to_a_candidate_that_was_already_decided(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    collector = ScriptedCollector(events=[("exa", event("Drag Brunch", start=SOON))])
    async with api(database_url, monkeypatch, collector) as client:
        first = await start(client)
        row = (await client.get(f"/runs/{first['run_id']}/results")).json()["items"][0]
        await client.post(f"/events/candidates/{row['id']}/approve", json={"version": 1})

        collector.events = [("exa", event("Drag Brunch", start=SOON, images=["https://cdn.test/a.jpg"]))]
        await start(client, force=True)

        assert (await client.get(f"/events/candidates/{row['id']}")).json()["event"]["images"] == []


async def test_the_venue_flow_merges_scores_and_lists_venues(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    collector = ScriptedCollector(
        venues=[
            ("osm_overpass", venue("Heaven", external_ids={"osm": "node/1"}, phone="+1 555 0100")),
            (
                "serpapi_maps",
                venue("Heaven", external_ids={"google_place_id": "g1"}, website=None, provider="serpapi_maps"),
            ),
            ("exa", venue("Quiet Cafe", level=None, website=None, address=None, lat=None, lng=None)),
        ]
    )
    async with api(database_url, monkeypatch, collector) as client:
        accepted = await start(client, "venues")

        run = (await client.get(f"/runs/{accepted['run_id']}")).json()
        queue = (await client.get("/venues/candidates", params={"city": "testville"})).json()
        packet = (await client.get(f"/venues/candidates/{queue['items'][0]['id']}")).json()

        assert run["status"] == "succeeded"
        assert queue["items"][0]["name"] == "Heaven"
        assert queue["items"][0]["contact"]["phone"] == "+1 555 0100"
        assert packet["flow"] == "venue"
        assert packet["event"] is None
        assert packet["venue"]["address"] == "1 Main Street"
        assert packet["venue"]["operating_status"] == "unknown"
        assert packet["fast_approve"] is True
        approved = await client.post(f"/venues/candidates/{packet['id']}/approve", json={"version": packet["version"]})
        assert approved.json()["status"] == "approved"


async def test_a_venue_run_searches_the_chosen_types_or_all_of_them(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    collector = ScriptedCollector(venues=[("exa", venue("Heaven"))])
    async with api(database_url, monkeypatch, collector) as client:
        everything = await start(client, "venues")
        assert collector.venue_types == list(VenueType)

        chosen = await start(client, "venues", venue_types=["Saunas", "Bars"])
        assert collector.venue_types == [VenueType.BARS, VenueType.SAUNAS]
        # Other types are another request, not the one that ran a moment ago; the same types in any order are not.
        assert chosen["deduplicated"] is False
        again = await start(client, "venues", venue_types=["Bars", "Saunas", "Bars"])
        assert (again["deduplicated"], again["run_id"]) == (True, chosen["run_id"])
        assert (await start(client, "venues", venue_types=[]))["run_id"] == everything["run_id"]

        unknown = await client.post("/venues/runs", json={**BODY, "venue_types": ["Hotels"]})
        assert unknown.status_code == 422


async def test_a_later_run_fills_the_missing_details_of_a_venue_quietly(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    collector = ScriptedCollector(venues=[("serpapi_maps", venue("Heaven", phone="+1 555 0100"))])
    async with api(database_url, monkeypatch, collector) as client:
        first = await start(client, "venues")
        row = (await client.get(f"/runs/{first['run_id']}/results")).json()["items"][0]

        # The same place again (as with KEEP_GOOGLE_DETAILS on): new details fill the gaps, the stored phone stays.
        richer = venue("Heaven", phone="+1 555 9999", description="A club.").model_copy(
            update={"opening_hours": "Monday 8 PM-2 AM", "price_text": "$$"}
        )
        collector.venues = [("serpapi_maps", richer)]
        second = await start(client, "venues", force=True)

        venue_block = (await client.get(f"/venues/candidates/{row['id']}")).json()
        assert venue_block["version"] == 1
        details = venue_block["venue"]
        assert (details["phone"], details["opening_hours"], details["price_text"]) == (
            "+1 555 0100",
            "Monday 8 PM-2 AM",
            "$$",
        )
        assert details["description"]["text"] == "A club."
        run = (await client.get(f"/runs/{second['run_id']}")).json()
        assert run["stats"]["candidates"]["unchanged"] == 1


async def test_details_are_not_added_to_a_venue_that_was_already_decided(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    collector = ScriptedCollector(venues=[("serpapi_maps", venue("Heaven"))])
    async with api(database_url, monkeypatch, collector) as client:
        first = await start(client, "venues")
        row = (await client.get(f"/runs/{first['run_id']}/results")).json()["items"][0]
        await client.post(f"/venues/candidates/{row['id']}/reject", json={"version": 1, "reason": "not_lgbtq"})

        collector.venues = [("serpapi_maps", venue("Heaven", phone="+1 555 0100"))]
        await start(client, "venues", force=True)

        assert (await client.get(f"/venues/candidates/{row['id']}")).json()["venue"]["phone"] is None


async def test_a_run_whose_providers_fail_is_recorded_as_failed(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with api(database_url, monkeypatch, ScriptedCollector(fail=True)) as client:
        accepted = await start(client)

        run = (await client.get(f"/runs/{accepted['run_id']}")).json()

        assert run["status"] == "failed"
        assert "the providers are down" in run["error"]
        retry = await start(client)  # a failed run does not answer for the request: it runs again
        assert retry["deduplicated"] is False


async def test_a_city_that_cannot_be_found_is_reported_and_the_others_still_run(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    cities = [*BODY["cities"], {"name": "Nowhere", "timezone": "Etc/UTC"}]
    async with api(database_url, monkeypatch, two_events_and_a_past_one()) as client:
        accepted = await start(client, cities=cities)

        run = (await client.get(f"/runs/{accepted['run_id']}")).json()

        assert run["status"] == "partial"
        assert run["stats"]["problems"] == ["Nowhere: no such city in ZZ"]
        assert run["stats"]["candidates"]["new"] == 2


async def test_a_run_with_no_provider_able_to_run_says_why(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("APP_ENV", "test")

    @asynccontextmanager
    async def open_environment() -> AsyncGenerator[RunEnvironment]:
        yield RunEnvironment(
            collector=ScriptedCollector(),
            geocoder=ScriptedGeocoder(),
            budget=Budget(1.0),
            providers=[],
            skipped={"serpapi_events": "its terms need review first", "exa": "EXA_API_KEY is not set"},
        )

    app = create_app(load_settings(), open_environment=open_environment, inline_runs=True)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test/api/v1") as client,
    ):
        accepted = await start(client)
        run = (await client.get(f"/runs/{accepted['run_id']}")).json()

    assert run["status"] == "failed"
    assert "no provider can run" in run["error"]
    assert "EXA_API_KEY is not set" in run["error"]


async def test_bad_requests_and_unknown_ids_are_refused_clearly(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with api(database_url, monkeypatch, ScriptedCollector()) as client:
        no_cities = await client.post("/events/runs", json={**BODY, "cities": []})
        bad_zone = await client.post(
            "/events/runs", json={**BODY, "cities": [{"name": "Testville", "timezone": "Mars/Base"}]}
        )
        bad_country = await client.post("/events/runs", json={**BODY, "country": "ZZZ"})

        assert no_cities.status_code == bad_zone.status_code == bad_country.status_code == 422
        assert "unknown time zone" in bad_zone.text
        assert (await client.get(f"/runs/{uuid4()}")).status_code == 404
        assert (await client.get(f"/runs/{uuid4()}/results")).status_code == 404
        assert (await client.get(f"/events/candidates/{uuid4()}")).status_code == 404
        assert (await client.get("/events/candidates/not-a-uuid")).status_code == 422


async def test_the_service_token_and_the_admin_role_decide_who_may_do_what(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = {"Authorization": f"Bearer {TOKEN}", "X-Admin-Id": "ann", "X-Admin-Role": "runner"}
    viewer = {**runner, "X-Admin-Id": "vic", "X-Admin-Role": "viewer"}
    reviewer = {**runner, "X-Admin-Id": "rae", "X-Admin-Role": "reviewer"}
    async with api(database_url, monkeypatch, two_events_and_a_past_one(), token=TOKEN) as client:
        assert (await client.get("/runs")).status_code == 401
        assert (await client.get("/runs", headers={"Authorization": "Bearer wrong"})).status_code == 401
        assert (await client.get("/runs", headers={"Authorization": f"Bearer {TOKEN}"})).status_code == 400
        assert (await client.post("/events/runs", json=BODY, headers=reviewer)).status_code == 403

        accepted = (await client.post("/events/runs", json=BODY, headers=runner)).json()
        row = (await client.get(f"/runs/{accepted['run_id']}/results", headers=viewer)).json()["items"][0]
        refused = await client.post(f"/events/candidates/{row['id']}/approve", json={"version": 1}, headers=viewer)
        approved = await client.post(f"/events/candidates/{row['id']}/approve", json={"version": 1}, headers=reviewer)

        assert refused.status_code == 403
        assert (approved.status_code, approved.json()["reviewed_by"]) == (200, "rae")


async def test_a_run_that_was_in_progress_when_the_service_stopped_is_failed_on_startup(
    database_url: str, monkeypatch: pytest.MonkeyPatch, engine: AsyncEngine
) -> None:
    stuck_id = uuid4()
    async with AsyncSession(engine) as session:
        session.add(
            DiscoveryRun(
                id=stuck_id, code="DSC-20200101-001", flow="event", filter={}, filter_hash="x" * 64, status="running"
            )
        )
        await session.commit()

    async with api(database_url, monkeypatch, ScriptedCollector()) as client:
        run = (await client.get(f"/runs/{stuck_id}")).json()

        assert run["status"] == "failed"
        assert "stopped while the run was in progress" in run["error"]
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM discovery_runs WHERE status = 'running'")) == 0
