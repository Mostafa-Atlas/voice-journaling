from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import PROJECT_ROOT, Settings
from .database import Database
from .storage import FileStorage


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="voicebot", description="Voicebot maintenance tools")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("health", help="Check database and queue health")
    subparsers.add_parser("reindex", help="Regenerate daily Markdown indexes")

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
