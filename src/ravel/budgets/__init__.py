"""Budget promises: the account, the reservation, and its outcome.

A budget in RAVEL is not a number in a prompt. It is an account an Owner has
explicitly configured, and a reservation written before an external submission.
The rule that makes the account meaningful is the one that is easiest to get
wrong: **absence means zero**, not unlimited. A project that has never
configured spending can still run free work, because zero is a limit and an
estimate of zero fits it, but it cannot promise a positive amount to anybody.

`BudgetReservation` has an outcome as well as an amount. A cancellation or a
known rejection releases it; an unknown external result moves it to `UNKNOWN`
and leaves the money and resource hours held. Settling an overrun records the
actual amount and pauses the account rather than editing the estimate to fit —
the estimate is what a human approved, and changing it after the fact would
erase the reason the overrun exists.
"""

from ravel.budgets.service import BudgetService

__all__ = ["BudgetService"]
