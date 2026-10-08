import pytest

from tg_discovery.pipelines.common.budget import HARD_CAP_USD, Budget
from tg_discovery.providers.base import BudgetExceededError


def test_reserve_and_settle_with_the_real_cost() -> None:
    budget = Budget(1.0)

    reservation = budget.reserve("exa", 0.30)
    assert budget.remaining_usd == pytest.approx(0.70)
    assert budget.reserved_usd == pytest.approx(0.30)

    budget.settle(reservation, 0.007)
    assert budget.remaining_usd == pytest.approx(0.993)
    assert budget.spent_usd == pytest.approx(0.007)
    assert budget.reserved_usd == 0


def test_a_call_that_does_not_fit_is_refused() -> None:
    budget = Budget(0.01)
    budget.reserve("exa", 0.008)

    with pytest.raises(BudgetExceededError, match="does not fit"):
        budget.reserve("tavily", 0.005)


def test_a_call_that_exactly_fits_is_allowed() -> None:
    budget = Budget(0.01)

    budget.reserve("exa", 0.01)


def test_concurrent_reservations_count_against_each_other() -> None:
    budget = Budget(0.02)
    budget.reserve("exa", 0.01)
    budget.reserve("brave", 0.01)

    with pytest.raises(BudgetExceededError):
        budget.reserve("tavily", 0.001)


def test_release_gives_the_reservation_back() -> None:
    budget = Budget(0.01)
    reservation = budget.reserve("exa", 0.01)

    budget.release(reservation)

    assert budget.remaining_usd == pytest.approx(0.01)
    assert budget.spent_usd == 0


def test_spend_is_tracked_per_provider() -> None:
    budget = Budget(1.0)
    budget.settle(budget.reserve("exa", 0.1), 0.05)
    budget.settle(budget.reserve("exa", 0.1), 0.05)
    budget.settle(budget.reserve("brave", 0.1), 0.02)

    assert budget.spent_by_provider() == pytest.approx({"exa": 0.10, "brave": 0.02})


@pytest.mark.parametrize("limit", [0, -1])
def test_the_limit_must_be_positive(limit: float) -> None:
    with pytest.raises(ValueError, match="positive"):
        Budget(limit)


def test_the_hard_cap_cannot_be_exceeded() -> None:
    assert HARD_CAP_USD == 50.0
    with pytest.raises(ValueError, match="hard cap"):
        Budget(50.01)
    Budget(50.0)
