"""Executable entrypoint for the voice memo bot."""

from __future__ import annotations

import argparse
import json
import logging
import os

from discord import LoginFailure
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
    parser.add_argument(
        "--no-auto-reload",
        action="store_true",
        help="Do not restart automatically when .env changes",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if os.name == "posix":
        os.umask(0o077)
    # Load .env BEFORE parsing args: CLI defaults (dashboard host/port) come
    # from the environment, and environment variables always win over .env.
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.load()
    except ConfigurationError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    configure_logging(settings)
    log = logging.getLogger("voicebot")
    dashboard_token = ""
    if args.dashboard:
        from voicebot.dashboard import (
            resolve_dashboard_token,
            start_dashboard,
            write_env_updates,
        )

        dashboard_token, generated = resolve_dashboard_token(args.dashboard_token)
        if generated:
            # resolve only generates when neither flag nor env provided, so the
            # token is safe to persist: it keeps the dashboard URL stable
            # across auto-reload restarts.
            write_env_updates(PROJECT_ROOT / ".env", {"DASHBOARD_TOKEN": dashboard_token})
            generated = False
            log.info("saved dashboard token to .env for stable restarts")
        dashboard_generated = generated
    if not args.no_auto_reload:
        from voicebot.reload import restart_process, watch_env_file

        watch_env_file(
            PROJECT_ROOT / ".env",
            on_change=lambda: restart_process(
                "detected .env change; restarting so new values take effect"
            ),
        )
        log.info("config auto-reload watching path=%s", PROJECT_ROOT / ".env")
    database = Database(settings.database_path)
    database.initialize()
    storage = FileStorage(settings.data_root, settings.voice_log_dir)
    gateway = build_gateway(settings)
    obsidian = ObsidianSync(settings, database)
    service = MemoService(settings, database, storage, gateway, obsidian)
    bot = create_bot(settings, database, service, obsidian)
    if args.dashboard:
        start_dashboard(
            settings,
            database,
            storage,
            service=service,
            obsidian=obsidian,
            host=args.dashboard_host,
            port=args.dashboard_port,
            token=dashboard_token,
        )
        if dashboard_generated:
            log.warning(
                "dashboard token was auto-generated for this run; "
                "set DASHBOARD_TOKEN for a stable token"
            )
        log.info(
            "dashboard ready url=http://%s:%d?token=<redacted>",
            args.dashboard_host,
            args.dashboard_port,
        )
        print(
            f"Dashboard: http://{args.dashboard_host}:{args.dashboard_port}?token={dashboard_token}"
        )
    log.info("starting voicebot config=%s", json.dumps(settings.redacted_summary()))
    try:
        bot.run(settings.discord_token, log_handler=None)
    except LoginFailure:
        log.error(
            "Discord rejected the token at login. Run `uv run voicebot setup` "
            "with a fresh Bot token (Developer Portal > Bot page, not the "
            "Client Secret), then `uv run voicebot doctor`."
        )
        return 1
    if not getattr(bot, "was_ready", True):
        # The startup watchdog closed the bot: gateway never became ready
        # (usually an invalid token). The clear error is already logged.
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
