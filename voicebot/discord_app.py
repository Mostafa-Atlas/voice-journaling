from __future__ import annotations

import asyncio
import logging
import time
from .config import Settings
from .database import Database
from .models import Memo, MemoStatus
from .obsidian import ObsidianSync
from .service import IncomingMemo, MemoService
from .storage import SUPPORTED_EXTENSIONS


log = logging.getLogger("voicebot.discord")


def create_bot(
    settings: Settings,
    database: Database,
    service: MemoService,
    obsidian: ObsidianSync,
):
    import discord
    from discord.ext import commands, tasks

    intents = discord.Intents.none()
    intents.message_content = True
    intents.dm_messages = True
    bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
    started = time.monotonic()
    resume_started = False

    async def queue_tick() -> None:
        try:
            delivered = await obsidian.flush()
            if delivered:
                log.info("background sync delivered count=%d", delivered)
        except Exception as exc:
            log.error("background sync failed error_type=%s", type(exc).__name__)

    queue_loop = tasks.loop(seconds=settings.obsidian_queue_check_seconds)(queue_tick)

    @bot.event
    async def on_ready():
        nonlocal resume_started
        log.info("bot ready user_id=%s config=%s", bot.user.id, settings.redacted_summary())
        if not queue_loop.is_running():
            queue_loop.start()
        if not resume_started:
            resume_started = True
            asyncio.create_task(service.resume_incomplete(), name="resume-incomplete-memos")

    @bot.check
    async def authorized_dm(ctx) -> bool:
        return (
            isinstance(ctx.channel, discord.DMChannel)
            and ctx.author.id in settings.allowed_users
        )

    @bot.event
    async def on_message(message):
        if message.author.bot:
            return
        if message.author.id not in settings.allowed_users:
            log.warning("ignored unauthorized message user_id=%s", message.author.id)
            return
        if not isinstance(message.channel, discord.DMChannel):
            return

        pending = []
        for attachment in message.attachments:
            incoming = IncomingMemo(
                message_id=str(message.id),
                attachment_id=str(attachment.id),
                discord_id=str(message.author.id),
                username=message.author.name,
                filename=attachment.filename,
                content_type=attachment.content_type,
                size_bytes=attachment.size,
                received_at=message.created_at.astimezone(settings.timezone),
            )
            try:
                service.validate(incoming)
            except ValueError as exc:
                await message.reply(f"⚠️ `{_safe_inline(attachment.filename)}`: {exc}")
                continue

            status_message = await message.reply(
                f"⏳ Received `{_safe_inline(attachment.filename)}`\n"
                f"Memo: `{incoming.memo_id}` · Stage: queued"
            )
            pending.append((incoming, attachment, status_message))

        async def handle_attachment(incoming, attachment, status_message):
            async def save_attachment(path):
                item = attachment
                await item.save(path)

            async def progress(stage: str):
                await _safe_edit(
                    status_message,
                    f"⏳ Memo `{incoming.memo_id}` · Stage: {stage}",
                )

            try:
                result = await service.accept(incoming, save_attachment, progress)
            except Exception as exc:
                log.warning(
                    "attachment failed memo_id=%s error_type=%s",
                    incoming.memo_id,
                    type(exc).__name__,
                )
                memo = await asyncio.to_thread(database.get_memo, incoming.memo_id)
                if memo and memo.audio_path:
                    guidance = f"Use `!retry {incoming.memo_id}`."
                else:
                    guidance = "The audio was not saved; resend the original attachment."
                await _safe_edit(
                    status_message,
                    f"❌ Memo `{incoming.memo_id}` failed safely.\n{guidance}",
                )
                return
            sync_text = "synced to Obsidian" if result.obsidian_synced else "queued for Obsidian"
            duplicate = " · already processed" if not result.created else ""
            await _safe_edit(
                status_message,
                (
                    f"✅ Memo `{result.memo.memo_id}` saved{duplicate}\n"
                    f"{_teaser(result.memo)}\n"
                    f"Status: {sync_text}"
                ),
            )

        if pending:
            outcomes = await asyncio.gather(
                *(handle_attachment(*item) for item in pending),
                return_exceptions=True,
            )
            for outcome in outcomes:
                if isinstance(outcome, Exception):
                    log.error(
                        "attachment task escaped handler error_type=%s",
                        type(outcome).__name__,
                    )

        await bot.process_commands(message)

    @bot.command(name="help")
    async def help_command(ctx):
        extensions = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        limit = settings.max_file_size_bytes // (1024 * 1024)
        await ctx.reply(
            "**Voice Memo Bot**\n"
            "Send an audio attachment in this DM to transcribe and summarize it.\n\n"
            f"Formats: `{extensions}` · Maximum: `{limit} MB`\n\n"
            "Commands:\n"
            "`!last [1-20]` — recent memos\n"
            "`!search <words>` — search your transcripts and summaries\n"
            "`!retry <memo-id>` — resume a failed memo\n"
            "`!sync` — retry the Obsidian queue\n"
            "`!status` — bot and queue health\n"
            "`!help` — this message"
        )

    @bot.command(name="last")
    async def last_command(ctx, count: int = 5):
        memos = await asyncio.to_thread(
            database.recent_for_user, str(ctx.author.id), count
        )
        if not memos:
            await ctx.reply("No saved voice memos yet.")
            return
        lines = ["🗂️ **Recent voice memos**"]
        for memo in memos:
            lines.append(
                f"- `{memo.memo_id}` · `{memo.status}` · {memo.received_at[:19]}\n"
                f"  {_teaser(memo)}"
            )
        await _reply_chunks(ctx, "\n".join(lines))

    @bot.command(name="search")
    async def search_command(ctx, *, query: str = ""):
        if not query.strip():
            await ctx.reply("Usage: `!search words to find`")
            return
        memos = await asyncio.to_thread(
            database.search_for_user, str(ctx.author.id), query, 10
        )
        if not memos:
            await ctx.reply("No matching voice memos.")
            return
        lines = [f"🔎 **Matches for:** {_safe_text(query[:100])}"]
        for memo in memos:
            lines.append(f"- `{memo.memo_id}` · {_teaser(memo)}")
        await _reply_chunks(ctx, "\n".join(lines))

    @bot.command(name="retry")
    async def retry_command(ctx, memo_id: str = ""):
        if not memo_id:
            await ctx.reply("Usage: `!retry <memo-id>`")
            return
        memo = await asyncio.to_thread(database.get_memo, memo_id)
        if memo is None or memo.discord_id != str(ctx.author.id):
            await ctx.reply("That memo was not found.")
            return
        if memo.status == MemoStatus.COMPLETED.value:
            await ctx.reply(f"Memo `{memo_id}` is already complete.")
            return
        status = await ctx.reply(f"⏳ Retrying `{memo_id}`…")

        async def progress(stage: str):
            await status.edit(content=f"⏳ Memo `{memo_id}` · Stage: {stage}")

        try:
            result = await service.retry(memo_id, progress=progress)
        except Exception:
            await status.edit(
                content=f"❌ Retry failed safely for `{memo_id}`. Check `!status` or logs."
            )
            return
        await status.edit(content=f"✅ Memo `{result.memo.memo_id}` completed. {_teaser(result.memo)}")

    @bot.command(name="sync")
    async def sync_command(ctx):
        delivered = await obsidian.flush()
        counts = await asyncio.to_thread(database.outbox_counts, str(ctx.author.id))
        pending = counts.get("pending", 0) + counts.get("delivering", 0)
        if delivered:
            await ctx.reply(f"✅ Synced {delivered} memo(s). Pending: {pending}.")
        elif pending:
            await ctx.reply(f"⏳ Obsidian is unavailable or waiting to retry. Pending: {pending}.")
        else:
            await ctx.reply("✅ The Obsidian queue is empty.")

    @bot.command(name="status")
    async def status_command(ctx):
        user_id = str(ctx.author.id)
        memo_counts, outbox_counts, last = await asyncio.gather(
            asyncio.to_thread(database.status_counts, user_id),
            asyncio.to_thread(database.outbox_counts, user_id),
            asyncio.to_thread(database.last_completed, user_id),
        )
        hours, remainder = divmod(int(time.monotonic() - started), 3_600)
        minutes = remainder // 60
        vault = "available" if await asyncio.to_thread(obsidian.available) else "unavailable"
        last_text = last.received_at[:19] if last else "none"
        await ctx.reply(
            "**Bot status**\n"
            f"Uptime: {hours}h {minutes}m\n"
            f"In flight: {service.in_flight_count}\n"
            f"Completed: {memo_counts.get('completed', 0)} · Failed: {memo_counts.get('failed', 0)}\n"
            f"Obsidian: {vault} · Pending sync: {outbox_counts.get('pending', 0)}\n"
            f"Last completed: {last_text}"
        )

    @bot.event
    async def on_command_error(ctx, error):
        if isinstance(error, commands.CheckFailure):
            return
        if isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
            await ctx.reply("Invalid command arguments. Use `!help` for examples.")
            return
        if isinstance(error, commands.CommandNotFound):
            await ctx.reply("Unknown command. Use `!help`.")
            return
        log.error(
            "command failed command=%s error_type=%s",
            ctx.command,
            type(error).__name__,
        )
        await ctx.reply("❌ The command failed. No data was deleted; check the bot logs.")

    return bot


async def _reply_chunks(ctx, text: str, limit: int = 1_900) -> None:
    chunk = ""
    for line in text.splitlines(keepends=True):
        if len(chunk) + len(line) > limit and chunk:
            await ctx.reply(chunk.rstrip())
            chunk = ""
        while len(line) > limit:
            await ctx.reply(line[:limit])
            line = line[limit:]
        chunk += line
    if chunk:
        await ctx.reply(chunk.rstrip())


async def _safe_edit(message, content: str) -> None:
    try:
        await message.edit(content=content)
    except Exception as exc:
        log.warning("Discord status edit failed error_type=%s", type(exc).__name__)


def _teaser(memo: Memo) -> str:
    if memo.summary:
        return _safe_text(memo.summary.teaser())
    if memo.summary_text:
        return _safe_text(" ".join(memo.summary_text.split())[:220])
    if memo.transcript:
        return _safe_text(" ".join(memo.transcript.split())[:220])
    return "No summary yet."


def _safe_text(value: str) -> str:
    value = " ".join(value.split())
    for character in ("\\", "`", "*", "_", "~", "|", ">", "#", "[", "]"):
        value = value.replace(character, f"\\{character}")
    return (
        value.replace("@", "＠")
        .replace("http://", "h\u200bttp://")
        .replace("https://", "h\u200bttps://")
    )


def _safe_inline(value: str) -> str:
    return _safe_text(value.replace("\n", " ").replace("\r", " "))
