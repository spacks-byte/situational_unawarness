"""
Scoped trading restrictions for the shared account (reconciliation without a global halt).

A reconciliation finding restricts only what it can be traced to. Market data, account sync,
order monitoring, cancellation and recovery always continue; restrictions only refuse NEW orders:

    scope            refuses                                         typical cause
    reads            every new order (this sync only)                an account read failed
    coin:<C>         new orders on coin C, both strategies           inventory mismatch, unknown or
                                                                     unresolved order on C
    cash:account     new BUY / SHORT_OPEN (cash-spending), all        untraceable cash or USD-Lock gap
    cash:<strategy>  new BUY / SHORT_OPEN of that strategy            cash gap traced to its fills
    short:<PAIR>     new short opens and close_qty closes on PAIR     short-position mismatch
                     (a full close_pct=100 is still allowed)
    strategy:<S>     every new order of strategy S                    its book is inconsistent

Sells, short covers and cancels on unrestricted scopes keep working, so open risk can always be
reduced. A restriction lifts after `clear_after` consecutive syncs without the finding; each
finding and lift is journaled by the caller.
"""
from __future__ import annotations

from typing import Any

CASH_SPENDING = {"BUY", "SHORT_OPEN"}
UNRESOLVED_AFTER = 5     # consecutive syncs with the same finding before it is labelled "unresolved"


class Restrictions:
    """Restriction set persisted inside the account state dict (state["restrictions"])."""

    def __init__(self, state: dict[str, Any], clear_after: int = 2, clock: Any = None) -> None:
        self.items: dict[str, dict[str, Any]] = state.setdefault("restrictions", {})
        self.clear_after = clear_after
        self.clock = clock                      # engine time goes through the injected Clock
        self._seen: set[str] = set()

    # ------------------------------------------------------------ a sync's findings
    def begin(self) -> None:
        self._seen = set()

    def flag(self, scope: str, reason: str, severity: str = "material") -> None:
        now = self.clock.now().isoformat() if self.clock is not None else None
        item = self.items.get(scope)
        if item is None:
            self.items[scope] = {"reason": reason, "severity": severity, "since": now, "seen": 1, "clean": 0}
        else:
            seen = item["seen"] + 1
            if seen >= UNRESOLVED_AFTER and severity in {"delayed", "material"}:
                severity = "unresolved"          # persists: needs a person (account explain/resolve)
            item.update(reason=reason, severity=severity, seen=seen, clean=0, last=now)
        self._seen.add(scope)

    def end(self) -> list[str]:
        """Count clean syncs for scopes not flagged this time; return the scopes lifted now."""
        lifted = []
        for scope, item in list(self.items.items()):
            if scope in self._seen:
                continue
            item["clean"] = item.get("clean", 0) + 1
            if scope == "reads" or item["clean"] >= self.clear_after:
                lifted.append(scope)
                del self.items[scope]
        return lifted

    # ------------------------------------------------------------ order gate
    def refusal(self, strategy: str, coin: str, side: str, *, full_close: bool = False) -> str | None:
        """Reason a new order is refused, or None if it may be sent."""
        side = side.upper()
        checks = [("reads", None), (f"strategy:{strategy}", None), (f"coin:{coin}", None)]
        if side in CASH_SPENDING:
            checks += [("cash:account", None), (f"cash:{strategy}", None)]
        if side in {"SHORT_OPEN", "SHORT_CLOSE"} and not (side == "SHORT_CLOSE" and full_close):
            checks.append((f"short:{coin}/USD", None))
        for scope, _ in checks:
            item = self.items.get(scope)
            if item is not None:
                return f"restricted ({scope}): {item['reason']}"
        return None

    def report(self) -> dict[str, Any]:
        return {scope: dict(item) for scope, item in sorted(self.items.items())}
