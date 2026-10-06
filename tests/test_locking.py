"""Portable account lock: Windows/Linux/macOS, independent of the working directory."""
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from tradebot.core.locking import (AccountLock, AccountLockError, account_id, default_lock_path,
                                   resolve_lock_path)

SRC = str(Path(__file__).resolve().parents[1] / "src")
HOLD = textwrap.dedent("""
    import sys, time
    from tradebot.core.locking import AccountLock, default_lock_path
    lock = AccountLock(default_lock_path("https://mock-api.roostoo.com", "key-A"))   # keep a reference
    print("held", flush=True)
    time.sleep(60)
""")


def _env(lock_dir):
    return {**os.environ, "TRADEBOT_LOCK_DIR": str(lock_dir), "PYTHONPATH": SRC}


def test_second_holder_is_refused_and_lock_frees_on_close(tmp_path):
    first = AccountLock(tmp_path / "a.lock", state_dir=tmp_path / "state")
    with pytest.raises(AccountLockError, match="already running.*pid"):
        AccountLock(tmp_path / "a.lock")
    first.close()
    AccountLock(tmp_path / "a.lock").close()


def test_lock_identity_hides_credentials_and_separates_accounts(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADEBOT_LOCK_DIR", str(tmp_path))
    path = default_lock_path("https://mock-api.roostoo.com/", "secret-key-123")
    assert path.is_absolute() and "secret-key-123" not in str(path)
    assert path == default_lock_path("https://MOCK-API.roostoo.com", "secret-key-123")   # normalised URL
    assert path != default_lock_path("https://mock-api.roostoo.com", "other-key")
    lock = AccountLock(path)
    holder = Path(str(path) + ".holder.json").read_text()
    assert "secret-key-123" not in holder and str(os.getpid()) in holder
    lock.close()
    assert len(account_id("u", "k")) == 16


def test_relative_explicit_path_is_made_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert resolve_lock_path("var/x.lock", "u", "k") == (tmp_path / "var" / "x.lock").resolve()
    assert resolve_lock_path("", "u", "k").is_absolute()


def test_different_working_directories_cannot_bypass_the_lock(tmp_path):
    """Two bot processes started from different folders contend for the same account lock."""
    (tmp_path / "cwd1").mkdir()
    (tmp_path / "cwd2").mkdir()
    lock_dir = tmp_path / "locks"
    holder = subprocess.Popen([sys.executable, "-c", HOLD], cwd=tmp_path / "cwd1", env=_env(lock_dir),
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        second = subprocess.run([sys.executable, "-c", HOLD], cwd=tmp_path / "cwd2", env=_env(lock_dir),
                                capture_output=True, text=True, timeout=60)
        assert second.returncode != 0 and "already running" in second.stderr
    finally:
        holder.kill()
        holder.wait()
    # The OS released the lock when the holder died: no stale lock after a crash
    time.sleep(0.2)
    AccountLock(lock_dir / f"account-{account_id('https://mock-api.roostoo.com', 'key-A')}.lock").close()


def test_standalone_rxm_takes_the_same_lock_as_the_coordinator(tmp_path, monkeypatch):
    """A live-port LiveRunner (standalone RXM) holds the per-account lock: a second one is refused."""
    from tradebot.core.config import Settings
    from tradebot.live.runner import LiveRunner

    monkeypatch.setenv("TRADEBOT_LOCK_DIR", str(tmp_path / "locks"))

    class LivePort:                      # stands in for RoostooExchangePort; no network is used
        is_live = True

        class client:
            base_url, api_key = "https://mock-api.roostoo.com", "key-B"

    settings = Settings.load("config/competition.yaml")
    settings = settings.model_copy(update={"live": settings.live.model_copy(update={"state_dir": str(tmp_path / "s1")})})
    first = LiveRunner(settings, mode="dry-run", port=LivePort())
    with pytest.raises(RuntimeError, match="already running"):
        LiveRunner(settings.model_copy(update={"live": settings.live.model_copy(update={"state_dir": str(tmp_path / "s2")})}),
                   mode="dry-run", port=LivePort())
    first.account_lock.close()
    first.engine.close()
