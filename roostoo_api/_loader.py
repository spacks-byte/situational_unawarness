from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_ROOT = Path(__file__).resolve().parents[1]
_API_DIR = _ROOT / "crypto-roostoo-api"


def load_raw_module(module_name: str) -> ModuleType:
    """Load a production API module from the existing raw client tree."""
    module_path = _API_DIR / f"{module_name}.py"
    if not module_path.exists():
        raise FileNotFoundError(f"Roostoo API module not found: {module_path}")

    if str(_API_DIR) not in sys.path:
        sys.path.insert(0, str(_API_DIR))

    spec = importlib.util.spec_from_file_location(f"roostoo_api._raw.{module_name}", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load module {module_name!r} from {module_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
