"""Spend control for a run: reserve before a call, settle with the real cost afterwards."""

from collections import defaultdict

from tg_discovery.providers.base import BudgetExceededError, Reservation

HARD_CAP_USD = 50.0
_EPSILON = 1e-9


class Budget:
    """USD budget for one run. A call is refused up front when it could push the run over the limit.

    Reserving the worst-case cost before a call and settling with the real cost afterwards keeps concurrent calls from
    jointly overshooting. ``hard_cap_usd`` stops a typo in the limit from becoming a large bill.
    """

    def __init__(self, limit_usd: float, *, hard_cap_usd: float = HARD_CAP_USD) -> None:
        if limit_usd <= 0:
            msg = f"budget must be positive, got {limit_usd}"
            raise ValueError(msg)
        if limit_usd > hard_cap_usd:
            msg = f"budget {limit_usd} exceeds the hard cap of {hard_cap_usd} USD"
            raise ValueError(msg)
        self.limit_usd = limit_usd
        self._spent: defaultdict[str, float] = defaultdict(float)
        self._reserved = 0.0

    @property
    def spent_usd(self) -> float:
        return sum(self._spent.values())

    @property
    def reserved_usd(self) -> float:
        return self._reserved

    @property
    def remaining_usd(self) -> float:
        return self.limit_usd - self.spent_usd - self._reserved

    def spent_by_provider(self) -> dict[str, float]:
        return dict(self._spent)

    def reserve(self, provider: str, amount_usd: float) -> Reservation:
        """Set aside ``amount_usd`` for a call, or raise ``BudgetExceededError`` when it does not fit."""
        if amount_usd > self.remaining_usd + _EPSILON:
            msg = (
                f"{provider}: a call of up to {amount_usd:.4f} USD does not fit "
                f"in the remaining {self.remaining_usd:.4f} USD"
            )
            raise BudgetExceededError(msg)
        self._reserved += amount_usd
        return Reservation(provider=provider, amount_usd=amount_usd)

    def settle(self, reservation: Reservation, actual_usd: float) -> None:
        """Replace a reservation with what the call really cost (which may be zero)."""
        self._reserved = max(self._reserved - reservation.amount_usd, 0.0)
        self._spent[reservation.provider] += actual_usd

    def release(self, reservation: Reservation) -> None:
        """Give a reservation back without spending anything."""
        self.settle(reservation, 0.0)
