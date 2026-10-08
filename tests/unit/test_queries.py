from datetime import date

from tests.support import FakeLlm
from tg_discovery.domain.enums import Flow
from tg_discovery.llm.schemas import LocalQuery
from tg_discovery.pipelines.common.queries import build_query_plan, generate_local_queries

TODAY = date(2026, 10, 8)


def _plan(flow: Flow, local: list[LocalQuery] | None = None):
    return build_query_plan(flow, city="Bangkok", country_name="Thailand", today=TODAY, local=local or [])


def test_a_venue_plan_has_twelve_english_queries_without_a_freshness_filter() -> None:
    plan = _plan(Flow.VENUE)

    assert [q.id for q in plan] == [f"v{n:02d}" for n in range(1, 13)]
    assert {q.language for q in plan} == {"en"}
    assert {q.freshness_days for q in plan} == {None}
    assert plan[0].text == "gay bars in Bangkok"
    assert plan[10].text == "best gay nightlife guide Bangkok Thailand"


def test_an_event_plan_is_dated_and_asks_for_recent_pages() -> None:
    plan = _plan(Flow.EVENT)

    assert len(plan) == 12
    assert {q.freshness_days for q in plan} == {60}
    assert "queer events Bangkok October 2026" in {q.text for q in plan}
    assert "Pride Bangkok 2026 events" in {q.text for q in plan}


def test_local_queries_replace_the_last_english_templates() -> None:
    local = [LocalQuery(text=f"query {n}", language="th") for n in range(3)]

    plan = _plan(Flow.VENUE, local)

    assert len(plan) == 12
    assert [q.id for q in plan[:9]] == [f"v{n:02d}" for n in range(1, 10)]
    assert [(q.id, q.text, q.language) for q in plan[9:]] == [
        ("vL01", "query 0", "th"),
        ("vL02", "query 1", "th"),
        ("vL03", "query 2", "th"),
    ]


def test_at_most_four_local_queries_are_used() -> None:
    local = [LocalQuery(text=f"query {n}", language="ja") for n in range(7)]

    plan = _plan(Flow.EVENT, local)

    assert len(plan) == 12
    assert sum(q.language == "ja" for q in plan) == 4
    assert plan[-1].freshness_days == 60


async def test_local_queries_are_generated_only_for_non_english_languages() -> None:
    llm = FakeLlm(
        {
            "queries": [
                {"text": "ゲイバー 新宿二丁目", "language": "ja"},
                {"text": "gay bar tokyo", "language": "en"},
                {"text": "한국어", "language": "ko"},
            ]
        }
    )

    queries = await generate_local_queries(llm, Flow.VENUE, city="Tokyo", country_name="Japan", languages=("en", "ja"))

    assert [(q.text, q.language) for q in queries] == [("ゲイバー 新宿二丁目", "ja")]
    prompt = str(llm.calls[0]["prompt"])
    assert "City: Tokyo, Japan" in prompt
    assert "Languages: ja" in prompt
    assert "gay venues" in prompt


async def test_an_english_only_city_needs_no_model_call() -> None:
    llm = FakeLlm()

    queries = await generate_local_queries(llm, Flow.EVENT, city="London", country_name="UK", languages=("en",))

    assert queries == []
    assert llm.calls == []
