from pathlib import Path


def test_no_direct_time_calls_in_engine():
    """The engine must route all time through tradebot.core.clock so live and simulated runs share logic."""
    repo_root = Path(__file__).resolve().parents[1]
    engine_root = repo_root / "src" / "tradebot" / "engine"
    bad_patterns = ["time.sleep", "time.time", "time.monotonic", "datetime.now"]

    offenders = []
    for path in engine_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pattern in bad_patterns:
            if pattern in text:
                offenders.append(f"{path.relative_to(repo_root)} uses {pattern}")

    assert engine_root.exists()
    assert not offenders, "Direct time calls outside the clock layer are forbidden:\n" + "\n".join(offenders)
