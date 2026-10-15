from tg_discovery.pipelines.discovery import CollectStats, Timing


def test_a_timing_counts_the_calls_adds_their_seconds_and_remembers_the_slowest() -> None:
    timing = Timing()

    for seconds in (1.0, 4.0, 2.5):
        timing.add(seconds)

    assert (timing.calls, timing.total_s, timing.slowest_s) == (3, 7.5, 4.0)


def test_busy_lists_the_kinds_by_total_time_longest_first_in_tenths_of_a_second() -> None:
    stats = CollectStats()
    stats.note("search:exa", 0.34)
    stats.note("search:exa", 0.52)
    stats.note("extract", 12.04)
    stats.note("search:serpapi_events", 90.0)

    assert stats.busy() == {
        "search:serpapi_events": {"calls": 1, "total_s": 90.0, "slowest_s": 90.0},
        "extract": {"calls": 1, "total_s": 12.0, "slowest_s": 12.0},
        "search:exa": {"calls": 2, "total_s": 0.9, "slowest_s": 0.5},
    }


def test_nothing_noted_gives_an_empty_report() -> None:
    assert CollectStats().busy() == {}
