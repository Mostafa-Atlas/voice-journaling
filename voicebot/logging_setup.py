from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler

from .config import Settings


SECRET_PATTERNS = (
    re.compile(r"\b(?:gsk|sk)_[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"(?i)(authorization:\s*bearer\s+)[^\s]+"),
)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        for pattern in SECRET_PATTERNS:
            message = pattern.sub(lambda match: _replacement(match), message)
        return message


def configure_logging(settings: Settings) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    try:
        settings.log_dir.chmod(0o700)
    except OSError:
        pass
    level = getattr(logging, settings.log_level, logging.INFO)
    formatter = RedactingFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        settings.log_dir / "voicebot.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    try:
        (settings.log_dir / "voicebot.log").chmod(0o600)
    except OSError:
        pass
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(stderr)
    root.addHandler(file_handler)


def _replacement(match: re.Match[str]) -> str:
    text = match.group(0)
    if text.lower().startswith("authorization:"):
        return match.group(1) + "<redacted>"
    return "<redacted>"
