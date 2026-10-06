"""
Exclusive, cross-platform account lock: at most one bot process per exchange account on a host.

The lock file lives at an absolute per-user location keyed to the account identity
(sha256 of base URL + API key, truncated), so starting a bot from a different working directory
cannot bypass it and no credential appears in a file name or on disk:

    Windows        %LOCALAPPDATA%\\tradebot\\locks\\account-<id>.lock
    Linux / macOS  $XDG_STATE_HOME/tradebot/locks (default ~/.local/state/tradebot/locks)
    override       $TRADEBOT_LOCK_DIR

The OS releases the lock when the process dies (fcntl.flock / msvcrt.locking), so a crash never
leaves a stale lock. A sidecar `<lock>.holder.json` records who holds it (pid, host, start time,
state dir) for the error message.

Limits: a local lock cannot coordinate processes on different hosts, and a bot built before this
lock existed cannot honour it. Deployment must stop such processes first (docs/MARKET_MAKING.md).
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class AccountLockError(RuntimeError):
    """Another process on this host holds the account lock."""


def account_id(base_url: str, api_key: str | None) -> str:
    """Stable, non-reversible account identity for lock names (never the key itself)."""
    raw = f"{(base_url or '').rstrip('/').lower()}|{api_key or ''}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def default_lock_dir() -> Path:
    override = os.environ.get("TRADEBOT_LOCK_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return (base / "tradebot" / "locks").resolve()


def default_lock_path(base_url: str, api_key: str | None) -> Path:
    return default_lock_dir() / f"account-{account_id(base_url, api_key)}.lock"


class AccountLock:
    """Hold an exclusive OS lock on `path` until close() or process exit."""

    def __init__(self, path: str | os.PathLike, *, state_dir: str | os.PathLike | None = None) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.holder_path = self.path.with_name(self.path.name + ".holder.json")
        self.handle = open(self.path, "a+")
        try:
            self._acquire()
        except OSError:
            self.handle.close()
            raise AccountLockError(f"account coordinator already running ({self._holder()}): {self.path}") from None
        self._write_holder(state_dir)

    def _acquire(self) -> None:
        if os.name == "nt":
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)   # raises OSError if held
        else:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)    # raises BlockingIOError if held

    def _release(self) -> None:
        if os.name == "nt":
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(self.handle, fcntl.LOCK_UN)

    def _holder(self) -> str:
        try:
            h = json.loads(self.holder_path.read_text(encoding="utf-8"))
            return f"pid {h.get('pid')} on {h.get('host')} since {h.get('started')}, state {h.get('state_dir')}"
        except (OSError, ValueError):
            return "holder unknown"

    def _write_holder(self, state_dir) -> None:
        info = {"pid": os.getpid(), "host": socket.gethostname(),
                "started": datetime.now(timezone.utc).isoformat(),
                "state_dir": str(Path(state_dir).resolve()) if state_dir else None}
        try:
            self.holder_path.write_text(json.dumps(info), encoding="utf-8")
        except OSError:
            pass   # informational only

    def close(self) -> None:
        if self.handle.closed:
            return
        try:
            self._release()
        finally:
            self.handle.close()


def lock_holder(path: str | os.PathLike) -> dict | None:
    """Who holds the account lock at `path` (None if nobody). Read-only probe: a missing lock file is
    not created, and a free lock is released immediately without rewriting the holder record."""
    path = Path(path).expanduser().resolve()
    if not path.exists():
        return None
    with open(path, "a+") as handle:
        try:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle, fcntl.LOCK_UN)
            return None
        except OSError:
            pass
    try:
        return json.loads(path.with_name(path.name + ".holder.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"holder": "unknown"}


def resolve_lock_path(explicit: str | None, base_url: str, api_key: str | None) -> Path:
    """An explicitly configured path (made absolute), else the per-account default location."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    return default_lock_path(base_url, api_key)
