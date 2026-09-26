"""Executable entrypoint for the voice memo bot."""

from __future__ import annotations

import argparse
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Voice memo Discord bot")
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="Also serve the local web dashboard in this process",
    )
    parser.add_argument(
        "--dashboard-host",
        default=os.getenv("DASHBOARD_HOST", "127.0.0.1"),
        help="Dashboard bind address (default 127.0.0.1; keep local)",
    )
    parser.add_argument(
        "--dashboard-port",
        type=int,
        default=int(os.getenv("DASHBOARD_PORT", "8080")),
        help="Dashboard port (default 8080)",
    )
    parser.add_argument(
        "--dashboard-token",
        default=None,
        help="Dashboard token (default: DASHBOARD_TOKEN env, else generated)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if os.name == "posix":
        os.umask(0o077)
    args = build_parser().parse_args(argv)
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
    if args.dashboard:
        from voicebot.dashboard import resolve_dashboard_token, start_dashboard

        token, generated = resolve_dashboard_token(args.dashboard_token)
        start_dashboard(
            settings,
            database,
            storage,
            service=service,
            obsidian=obsidian,
            host=args.dashboard_host,
            port=args.dashboard_port,
            token=token,
        )
        if generated:
            log.warning(
                "dashboard token was auto-generated for this run; "
                "set DASHBOARD_TOKEN for a stable token"
            )
        log.info(
            "dashboard ready url=http://%s:%d?token=<redacted>",
            args.dashboard_host,
            args.dashboard_port,
        )
        print(f"Dashboard: http://{args.dashboard_host}:{args.dashboard_port}?token={token}")
    log.info("starting voicebot config=%s", json.dumps(settings.redacted_summary()))
    bot.run(settings.discord_token, log_handler=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
