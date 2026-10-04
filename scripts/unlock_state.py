"""Undo a FALSE lock-in persisted in <state_dir>/strategy_state.json (2026-10-04 incident).

Run on the server, with the bot STOPPED and the snapshot fix already deployed (otherwise the old
code reads ~$114k again and re-locks within two polls). Dry run by default; --apply writes.

    python scripts/unlock_state.py --state-dir var/live_comp            # check only
    python scripts/unlock_state.py --state-dir var/live_comp --apply    # back up + unlock

It never touches engine_state.db (the order journal), the exchange, or any other file.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

LOCK_KEYS = ("lock_time", "lock_equity", "lock_return")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-dir", default="var/live_comp")
    ap.add_argument("--expected-start", type=float, default=100_000.0)
    ap.add_argument("--tolerance", type=float, default=0.02)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--force", action="store_true", help="skip the 'bot looks alive' check")
    a = ap.parse_args()
    d = Path(a.state_dir)
    path = d / "strategy_state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    print("current strategy_state.json:", json.dumps(state, indent=2, sort_keys=True))

    problems = []
    start = float(state.get("start_equity") or 0)
    if abs(start / a.expected_start - 1) > a.tolerance:
        problems.append(f"start_equity {start:,.2f} is not within {a.tolerance:.0%} of {a.expected_start:,.0f}: "
                        "do not unlock blindly, find out where it came from")
    if not state.get("locked"):
        print("not locked: nothing to do")
        return 0
    lock_ret = float(state.get("lock_return") or 0)
    print(f"lock_return {lock_ret:+.2%}, lock_equity {state.get('lock_equity')}, lock_time {state.get('lock_time')}")
    if lock_ret < 0.15:
        print("WARNING: lock_return below +15%: this may be a GENUINE +6% lock-in. Check the UI equity at lock_time.")

    status = d / "status.json"
    if status.exists() and not a.force:
        st = json.loads(status.read_text(encoding="utf-8"))
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(st["updated"])).total_seconds()
        if st.get("state") not in ("stopped",) and age < 300:
            problems.append(f"status.json says state={st.get('state')} updated {age:.0f}s ago: stop the bot first")

    snap_file = d / "latest_snapshot.json"
    if snap_file.exists():
        snap = json.loads(snap_file.read_text(encoding="utf-8")).get("snapshot", {})
        lock = snap.get("usd_lock", snap.get("cash_usd", 0) - snap.get("cash_free_usd", 0))
        coll = sum((snap.get("shorts") or {}).values())
        print(f"latest snapshot: equity_usd {snap.get('equity_usd'):,.2f}  cash_usd {snap.get('cash_usd'):,.2f}  "
              f"Free {snap.get('cash_free_usd'):,.2f}  Lock {lock:,.2f}  open-short collateral {coll:,.2f}  "
              f"pending orders {len(snap.get('pending_orders') or [])}")
        if "lock_short_collateral_usd" not in snap:
            print("  (snapshot written by the OLD code: if Lock ~= collateral and no orders were pending, the "
                  "collateral was double counted; equity_usd - collateral should equal the UI equity)")
        elif snap.get("lock_unexplained_usd", 0) > 0.01 * snap.get("equity_usd", 1):
            problems.append(f"new code still sees unexplained USD Lock {snap['lock_unexplained_usd']:,.2f}")

    if problems:
        print("\nREFUSING:\n  - " + "\n  - ".join(problems))
        return 2
    new = {k: v for k, v in state.items() if k not in LOCK_KEYS}
    new["locked"] = False
    new["unlock_note"] = (f"false lock-in (open-short collateral double counted) undone "
                          f"{datetime.now(timezone.utc).isoformat()}; was {json.dumps({k: state.get(k) for k in LOCK_KEYS})}")
    print("\nnew strategy_state.json:", json.dumps(new, indent=2, sort_keys=True))
    if not a.apply:
        print("\ndry run: nothing written (add --apply)")
        return 0
    backup = path.with_name(f"strategy_state.json.bak-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")
    shutil.copy2(path, backup)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(new, indent=2, sort_keys=True, default=str), encoding="utf-8")
    os.replace(tmp, path)
    print(f"\nbacked up to {backup}; unlocked {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
