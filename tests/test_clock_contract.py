from pathlib import Path


def test_no_direct_time_calls_outside_clock_layer():
    repo_root = Path(__file__).resolve().parents[1]
    engine_root = repo_root / "src" / "engine"
    allowed_files = {
        repo_root / "src" / "engine" / "clock" / "base.py",
        repo_root / "src" / "engine" / "clock" / "__init__.py",
    }

    bad_patterns = [
        "time.sleep",
        "time.time",
        "time.monotonic",
        "datetime.now",
    ]

    offenders = []
    for path in engine_root.rglob("*.py"):
        if path in allowed_files:
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in bad_patterns:
            if pattern in text:
                offenders.append(f"{path.relative_to(repo_root)} uses {pattern}")

    assert not offenders, "Direct time calls outside the clock layer are forbidden:\n" + "\n".join(offenders)
