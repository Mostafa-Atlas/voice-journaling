"""Executable entrypoint for the voice memo bot."""

from __future__ import annotations

import json
import logging
import os

from dotenv import load_dotenv

from voicebot.ai import build_gateway
from voicebot.config import PROJECT_ROOT, ConfigurationError, Settings
from voicebot.database import Database
from voicebot.discord_app import create_bot
from voicebot.logging_setup import configure_logging
from voicebot.obsidian import ObsidianSync
from voicebot.service import MemoService
from voicebot.storage import FileStorage


def main() -> int:
    if os.name == "posix":
        os.umask(0o077)
    # Environment variables always win over .env values.
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    try:
        settings = Settings.load()
    except ConfigurationError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    configure_logging(settings)
    log = logging.getLogger("voicebot")
    database = Database(settings.database_path)
    database.initialize()
    storage = FileStorage(settings.data_root, settings.voice_log_dir)
    gateway = build_gateway(settings)
    obsidian = ObsidianSync(settings, database)
    service = MemoService(settings, database, storage, gateway, obsidian)
    bot = create_bot(settings, database, service, obsidian)
    log.info("starting voicebot config=%s", json.dumps(settings.redacted_summary()))
    bot.run(settings.discord_token, log_handler=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
