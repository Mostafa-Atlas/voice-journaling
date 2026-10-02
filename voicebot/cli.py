from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import PROJECT_ROOT, ConfigurationError, Settings
from .database import Database
from .storage import FileStorage


def run_dashboard_standalone(host: str, port: int) -> int:
    """Serve the dashboard without requiring Discord credentials."""
    from .dashboard import resolve_dashboard_token, start_dashboard

    try:
        settings = Settings.load(require_secrets=False)
    except ConfigurationError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc
    database = Database(settings.database_path)
    database.initialize()
    storage = FileStorage(settings.data_root, settings.voice_log_dir)
    token = os.getenv("DASHBOARD_TOKEN", "").strip()
    resolved, generated = resolve_dashboard_token(token or None)
    server, _ = start_dashboard(settings, database, storage, host=host, port=port, token=resolved)
    actual_port = server.server_port
    print(f"Dashboard: http://{host}:{actual_port}?token={resolved}")
    if generated:
        print("Token was auto-generated for this run; set DASHBOARD_TOKEN for a stable one.")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
    finally:
        server.shutdown()
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="voicebot", description="Voicebot maintenance tools")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("health", help="Check database and queue health")
    subparsers.add_parser("reindex", help="Regenerate daily Markdown indexes")

    setup = subparsers.add_parser("setup", help="Interactive first-run setup wizard")
    setup.add_argument("--discord-token", help="Discord bot token (else prompted)")
    setup.add_argument("--client-id", default="", help="Discord application ID for invite URL")
    setup.add_argument("--provider", choices=["groq", "openai"], help="AI provider (else prompted)")
    setup.add_argument("--provider-key", help="API key for the provider (else prompted)")
    setup.add_argument("--owner-id", help="Your Discord user ID (else prompted)")
    setup.add_argument("--timezone", default=None, help="IANA timezone (default UTC)")
    setup.add_argument("--non-interactive", action="store_true", help="Fail instead of prompting")
    setup.add_argument("--skip-validation", action="store_true", help="Skip live API checks")

    doctor = subparsers.add_parser("doctor", help="Preflight checks for fresh installs")
    doctor.add_argument("--json", action="store_true", help="Machine-readable output")

    dashboard_cmd = subparsers.add_parser(
        "dashboard", help="Serve the web dashboard without needing Discord configured"
    )
    dashboard_cmd.add_argument("--host", default=os.getenv("DASHBOARD_HOST", "127.0.0.1"))
    dashboard_cmd.add_argument("--port", type=int, default=int(os.getenv("DASHBOARD_PORT", "8080")))

    export = subparsers.add_parser("export", help="Export memo metadata and content as JSON")
    export.add_argument("--output", type=Path, help="Write JSON to this file instead of stdout")

    prune = subparsers.add_parser("prune", help="Preview or delete old completed local memos")
    prune.add_argument("--days", type=int, help="Delete completed memos older than this")
    prune.add_argument("--execute", action="store_true", help="Delete after previewing")
    prune.add_argument("--yes", action="store_true", help="Skip interactive confirmation")
    return parser


def main(argv: list[str] | None = None) -> int:
    if os.name == "posix":
        os.umask(0o077)
    _load_dotenv_if_available()
    args = build_parser().parse_args(argv)
    if args.command == "setup":
        from .setup import run_setup

        try:
            return run_setup(
                PROJECT_ROOT,
                discord_token=args.discord_token,
                client_id=args.client_id,
                provider=args.provider,
                provider_key=args.provider_key,
                owner_id=args.owner_id,
                timezone_name=args.timezone,
                non_interactive=args.non_interactive,
                skip_validation=args.skip_validation,
            )
        except ConfigurationError as exc:
            print(f"Setup failed: {exc}")
            return 1
    if args.command == "doctor":
        from .doctor import main as doctor_main

        return doctor_main(PROJECT_ROOT, output_json=args.json)
    if args.command == "dashboard":
        return run_dashboard_standalone(args.host, args.port)
    settings = Settings.load(require_secrets=False)
    database = Database(settings.database_path)
    database.initialize()
    storage = FileStorage(settings.data_root, settings.voice_log_dir)

    if args.command == "health":
        payload = {
            "database_integrity": database.integrity_check(),
            "memo_statuses": database.status_counts(),
            "outbox_statuses": database.outbox_counts(),
            "voice_log_writable": os.access(settings.voice_log_dir, os.W_OK),
            "retention_days": settings.retention_days,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["database_integrity"] == "ok" else 1

    if args.command == "reindex":
        dates = sorted({memo.received_at[:10] for memo in database.all_memos()})
        for date_text in dates:
            storage.rebuild_daily_index(date_text, database.memos_for_date(date_text))
        print(f"Regenerated {len(dates)} daily index file(s).")
        return 0

    if args.command == "export":
        payload = [asdict(memo) for memo in database.all_memos()]
        text = json.dumps(payload, indent=2, ensure_ascii=False)
        if args.output:
            destination = args.output.resolve()
            destination.write_text(text + "\n", encoding="utf-8")
            try:
                destination.chmod(0o600)
            except OSError:
                pass
            print(f"Exported {len(payload)} memo(s) to {destination}.")
        else:
            print(text)
        return 0

    if args.command == "prune":
        days = args.days if args.days is not None else settings.retention_days
        if days <= 0:
            raise SystemExit("Choose --days N or set RETENTION_DAYS to a positive value")
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        candidates = [
            memo
            for memo in database.retention_candidates(cutoff)
            if not memo.memo_id.startswith("legacy-")
        ]
        for memo in candidates:
            print(f"{'DELETE' if args.execute else 'DRY-RUN'} {memo.memo_id} {memo.completed_at}")
        if not args.execute:
            print(f"Dry-run: {len(candidates)} memo(s). Add --execute after review.")
            return 0
        if not args.yes:
            phrase = f"DELETE {len(candidates)} MEMOS"
            if input(f"Type {phrase!r} to continue: ") != phrase:
                raise SystemExit("Confirmation did not match; nothing was deleted")
        affected_dates: set[str] = set()
        for memo in candidates:
            storage.delete_memo_files(memo)
            database.delete_memo(memo.memo_id)
            affected_dates.add(memo.received_at[:10])
        for date_text in affected_dates:
            storage.rebuild_daily_index(date_text, database.memos_for_date(date_text))
        print(f"Deleted {len(candidates)} memo(s). Obsidian copies were not deleted.")
        return 0

    return 2


def _load_dotenv_if_available() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(PROJECT_ROOT / ".env")


if __name__ == "__main__":
    raise SystemExit(main())
