"""Pre-trade and real-time guard rails for the competition bot (see docs/DASHBOARD.md)."""
from src.guard.checks import (  # noqa: F401
    CheckResult,
    Guard,
    GuardConfig,
    GuardReport,
    ProposedOrder,
    Status,
)
