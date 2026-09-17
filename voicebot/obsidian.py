from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path

from .config import Settings
from .database import Database
from .models import Memo, SummaryData, escape_markdown
from .storage import atomic_write, interprocess_file_lock


log = logging.getLogger("voicebot.obsidian")


class ObsidianUnavailable(OSError):
    pass


class ObsidianSync:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database
        self._flush_lock = asyncio.Lock()
        self._worker_id = uuid.uuid4().hex

    def available(self) -> bool:
        path = self.settings.obsidian_vault_path
        if not path.exists():
            return False
        if self.settings.obsidian_require_mount and not os.path.ismount(path):
            return False
        return os.access(path, os.W_OK)

    def enqueue(self, memo_id: str) -> None:
        self.database.enqueue_outbox(memo_id)

    async def flush(self, limit: int = 100) -> int:
        if not await asyncio.to_thread(self.available):
            return 0
        delivered = 0
        async with self._flush_lock:
            for _ in range(limit):
                row = await asyncio.to_thread(
                    self.database.claim_outbox, self._worker_id
                )
                if row is None:
                    break
                memo = await asyncio.to_thread(
                    self.database.get_memo, row["memo_id"]
                )
                if memo is None:
                    await asyncio.to_thread(
                        self.database.mark_outbox_delivered,
                        row["id"],
                        self._worker_id,
                    )
                    continue
                try:
                    await asyncio.to_thread(self._deliver, memo)
                except OSError as exc:
                    attempts = int(row["attempts"]) + 1
                    await asyncio.to_thread(
                        self.database.mark_outbox_error,
                        row["id"],
                        attempts,
                        type(exc).__name__,
                        self._worker_id,
                    )
                    log.warning(
                        "obsidian delivery failed memo_id=%s error_type=%s",
                        memo.memo_id,
                        type(exc).__name__,
                    )
                    break
                except Exception as exc:
                    attempts = int(row["attempts"]) + 1
                    await asyncio.to_thread(
                        self.database.mark_outbox_failed,
                        row["id"],
                        attempts,
                        type(exc).__name__,
                        self._worker_id,
                    )
                    log.error(
                        "obsidian item dead-lettered memo_id=%s error_type=%s",
                        memo.memo_id,
                        type(exc).__name__,
                    )
                    continue
                await asyncio.to_thread(
                    self.database.mark_outbox_delivered,
                    row["id"],
                    self._worker_id,
                )
                delivered += 1
                log.info("obsidian delivered memo_id=%s", memo.memo_id)
        return delivered

    def _deliver(self, memo: Memo) -> None:
        if not self.available():
            raise ObsidianUnavailable("Obsidian vault is unavailable")
        if not memo.summary_json or not memo.transcript:
            raise ValueError("memo is missing summary or transcript")
        date_text = memo.received_at[:10]
        note_dir = self.settings.obsidian_dir.resolve()
        note_dir.mkdir(parents=True, exist_ok=True)
        note_path = (note_dir / f"{date_text}.md").resolve()
        try:
            note_path.relative_to(note_dir)
        except ValueError as exc:
            raise OSError("invalid Obsidian note path") from exc

        lock_path = note_dir / f".{date_text}.voicebot.lock"
        with interprocess_file_lock(lock_path):
            marker = f"<!-- voice-memo:{memo.memo_id} -->"
            existing = note_path.read_text(encoding="utf-8") if note_path.exists() else ""
            if marker in existing:
                return
            if not existing:
                existing = (
                    "---\n"
                    "tags: [journal, voice-memo]\n"
                    f"date: {date_text}\n"
                    "---\n\n"
                    f"# {date_text}\n"
                )
            summary = SummaryData.from_json(memo.summary_json)
            transcript_lines = "\n".join(
                f"> {escape_markdown(line)}" for line in memo.transcript.strip().splitlines()
            )
            safe_filename = " ".join(Path(memo.original_filename).name.split()).replace("`", "ˋ")
            entry = (
                f"\n{marker}\n"
                f"### {_display_time(memo.received_at)}\n\n"
                f"Memo: `{memo.memo_id}` · Audio: `{safe_filename}`\n\n"
                f"{summary.to_markdown()}\n"
                "> [!quote]- Raw transcript\n"
                f"{transcript_lines}\n\n"
                f"<!-- /voice-memo:{memo.memo_id} -->\n\n"
                "---\n"
            )
            atomic_write(note_path, existing.rstrip() + "\n" + entry)


def _display_time(timestamp: str) -> str:
    try:
        return datetime.fromisoformat(timestamp).strftime("%I:%M %p").lstrip("0")
    except (ValueError, TypeError):
        return timestamp[11:19] if len(timestamp) >= 19 else timestamp
