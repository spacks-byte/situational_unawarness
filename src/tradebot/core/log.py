"""Logging setup shared by every entry point. Library code only calls logging.getLogger(__name__)."""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(level: str = "INFO", log_file: Optional[str | Path] = None) -> None:
    """Configure the root logger: console, plus a rotating file when log_file is given."""
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(path, maxBytes=10_000_000, backupCount=5, encoding="utf-8"))
    logging.basicConfig(level=level.upper(), format=LOG_FORMAT, handlers=handlers, force=True)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
