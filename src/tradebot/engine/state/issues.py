"""Persistent advisory reconciliation signals. These never gate trading.

TODO: surface these signals on the dashboard after the execution fix and server
deployment. See root AGENTS.md; the flag UI is deliberately deferred.
"""
from copy import deepcopy
import math


def evidence_value(value):
    """Malformed numeric evidence must itself remain serializable in the journal."""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {str(k): evidence_value(v) for k,v in value.items()}
    if isinstance(value, (list,tuple)):
        return [evidence_value(v) for v in value]
    return value


class ReconciliationIssues:
    def __init__(self, state, clear_after=2, clock=None):
        self.items = state.setdefault('issues', {})
        for scope, old in state.pop('restrictions', {}).items():
            self.items.setdefault(scope, dict(old, scope=scope, code=scope.split(':')[0],
                                             status='open', blocking=False))
        self.clear_after, self.clock = clear_after, clock
        self._seen = set()

    def begin(self):
        self._seen = set()

    def flag(self, scope, reason, severity='material', *, code=None, order_id=None,
             observed=None, expected=None):
        now = self.clock.now().isoformat() if self.clock else None
        key = f'{scope}:order:{order_id}' if order_id is not None else scope
        old = self.items.get(key, {})
        self.items[key] = dict(scope=scope, code=code or scope.split(':')[0],
            order_id=order_id, reason=reason, severity=severity, blocking=False,
            observed=evidence_value(observed), expected=evidence_value(expected), status='open',
            since=old.get('since', now), last=now, seen=old.get('seen', 0)+1, clean=0)
        self._seen.add(key)

    def end(self):
        resolved = []
        for key, item in self.items.items():
            if key in self._seen or item.get('status') == 'resolved':
                continue
            item['clean'] = item.get('clean', 0)+1
            if item['clean'] >= self.clear_after:
                item['status'] = 'resolved'
                item['resolved_at'] = self.clock.now().isoformat() if self.clock else None
                resolved.append(key)
        return resolved

    def report(self):
        return deepcopy({k: v for k, v in sorted(self.items.items()) if v.get('status') != 'resolved'})
