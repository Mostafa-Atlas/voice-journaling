"""Safely preview or delete this bot's messages in one DM conversation."""

from __future__ import annotations

import argparse
import datetime as dt
import os


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-user", type=int, required=True, help="Exact Discord user ID")
    parser.add_argument(
        "--limit", type=int, default=100, help="Maximum messages to inspect (1-5000)"
    )
    parser.add_argument(
        "--before-days", type=int, help="Only inspect messages older than this many days"
    )
    parser.add_argument(
        "--execute", action="store_true", help="Actually delete; default is dry-run"
    )
    parser.add_argument("--yes", action="store_true", help="Skip the interactive confirmation")
    return parser


async def run_cleanup(args: argparse.Namespace, token: str) -> int:
    import discord

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)
    result = {"matched": 0, "deleted": 0, "failed": 0}

    @client.event
    async def on_ready():
        try:
            user = await client.fetch_user(args.target_user)
            channel = await user.create_dm()
            before = None
            if args.before_days is not None:
                before = dt.datetime.now(dt.UTC) - dt.timedelta(days=args.before_days)
            async for message in channel.history(limit=args.limit, before=before):
                if message.author != client.user:
                    continue
                result["matched"] += 1
                if not args.execute:
                    print(
                        f"DRY-RUN message_id={message.id} created_at={message.created_at.isoformat()}"
                    )
                    continue
                try:
                    await message.delete()
                    result["deleted"] += 1
                    print(f"DELETED message_id={message.id}")
                except (discord.Forbidden, discord.HTTPException) as exc:
                    result["failed"] += 1
                    print(f"FAILED message_id={message.id} error_type={type(exc).__name__}")
        finally:
            await client.close()

    await client.start(token)
    print(
        f"matched={result['matched']} deleted={result['deleted']} "
        f"failed={result['failed']} dry_run={not args.execute}"
    )
    return 1 if result["failed"] else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.limit <= 5_000:
        raise SystemExit("--limit must be between 1 and 5000")
    if args.before_days is not None and args.before_days < 0:
        raise SystemExit("--before-days must not be negative")
    token = os.getenv("DISCORD_CLEANUP_TOKEN", os.getenv("DISCORD_TOKEN", "")).strip()
    if not token:
        raise SystemExit("Set DISCORD_CLEANUP_TOKEN (or DISCORD_TOKEN) in the environment")
    if args.execute and not args.yes:
        phrase = f"DELETE {args.target_user}"
        entered = input(f"Type {phrase!r} to delete up to {args.limit} bot messages: ")
        if entered != phrase:
            raise SystemExit("Confirmation did not match; nothing was deleted")
    if not args.execute:
        print("Dry-run only. Re-run with --execute after reviewing the message IDs.")

    import asyncio

    return asyncio.run(run_cleanup(args, token))


if __name__ == "__main__":
    raise SystemExit(main())
