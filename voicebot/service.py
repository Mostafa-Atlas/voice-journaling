from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import Settings
from .database import Database
from .models import Memo, MemoStatus
from .obsidian import ObsidianSync
from .storage import FileStorage, safe_component


log = logging.getLogger("voicebot.service")
SaveAttachment = Callable[[Path], Awaitable[None]]
ProgressCallback = Callable[[str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class IncomingMemo:
    message_id: str
    attachment_id: str
    discord_id: str
    username: str
    filename: str
    content_type: str | None
    size_bytes: int
    received_at: datetime

    @property
    def memo_id(self) -> str:
        return safe_component(
            f"discord-{self.message_id}-{self.attachment_id}",
            fallback="discord-memo",
        )


@dataclass(frozen=True, slots=True)
class ProcessingResult:
    memo: Memo
    created: bool
    obsidian_synced: bool


class MemoService:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        storage: FileStorage,
        gateway: object,
        obsidian: ObsidianSync,
    ):
        self.settings = settings
        self.database = database
        self.storage = storage
        self.gateway = gateway
        self.obsidian = obsidian
        self._processing_semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)
        self._memo_locks: dict[str, tuple[asyncio.Lock, int]] = {}
        self._memo_locks_guard = asyncio.Lock()
        self._in_flight: set[str] = set()

    @property
    def in_flight_count(self) -> int:
        return len(self._in_flight)

    def validate(self, incoming: IncomingMemo) -> None:
        suffix = self.storage.validate_extension(incoming.filename)
        if incoming.size_bytes <= 0:
            raise ValueError("audio attachment is empty")
        if incoming.size_bytes > self.settings.max_file_size_bytes:
            limit = self.settings.max_file_size_bytes // (1024 * 1024)
            actual = incoming.size_bytes / (1024 * 1024)
            raise ValueError(f"audio is {actual:.1f} MB; the configured limit is {limit} MB")
        if incoming.content_type:
            content_type = incoming.content_type.lower().split(";", 1)[0]
            video_audio_containers = {".mp4", ".webm"}
            if not content_type.startswith("audio/") and not (
                suffix in video_audio_containers and content_type.startswith("video/")
            ):
                raise ValueError(f"attachment content type is not audio: {content_type}")

    async def accept(
        self,
        incoming: IncomingMemo,
        save_attachment: SaveAttachment,
        progress: ProgressCallback | None = None,
    ) -> ProcessingResult:
        self.validate(incoming)
        memo, created = await asyncio.to_thread(
            self.database.register_memo,
            memo_id=incoming.memo_id,
            message_id=incoming.message_id,
            attachment_id=incoming.attachment_id,
            discord_id=incoming.discord_id,
            username=incoming.username,
            original_filename=Path(incoming.filename).name,
            content_type=incoming.content_type,
            size_bytes=incoming.size_bytes,
            received_at=incoming.received_at.isoformat(),
        )
        if memo.status == MemoStatus.COMPLETED.value:
            sync_status = await asyncio.to_thread(
                self.database.outbox_status, memo.memo_id
            )
            return ProcessingResult(
                memo=memo,
                created=False,
                obsidian_synced=sync_status == "delivered",
            )
        return await self._process(
            memo.memo_id,
            save_attachment=save_attachment,
            progress=progress,
            created=created,
        )

    async def retry(
        self, memo_id: str, *, progress: ProgressCallback | None = None
    ) -> ProcessingResult:
        memo = await asyncio.to_thread(self.database.get_memo, memo_id)
        if memo is None:
            raise KeyError(memo_id)
        if not memo.audio_path:
            raise ValueError("audio was never saved; resend the original attachment")
        return await self._process(memo_id, progress=progress, created=False)

    async def resume_incomplete(self) -> int:
        resumed = 0
        await asyncio.to_thread(self.database.mark_orphaned_received)
        for memo in await asyncio.to_thread(self.database.incomplete_memos):
            try:
                await self.retry(memo.memo_id)
                resumed += 1
            except Exception:
                log.exception("automatic resume failed memo_id=%s", memo.memo_id)
        return resumed

    async def _process(
        self,
        memo_id: str,
        *,
        save_attachment: SaveAttachment | None = None,
        progress: ProgressCallback | None = None,
        created: bool,
    ) -> ProcessingResult:
        async with self._memo_lock(memo_id), self._processing_semaphore:
            self._in_flight.add(memo_id)
            started = time.perf_counter()
            stage = "received"
            try:
                memo = await asyncio.to_thread(self.database.increment_attempt, memo_id)
                if memo.status == MemoStatus.COMPLETED.value:
                    sync_status = await asyncio.to_thread(
                        self.database.outbox_status, memo_id
                    )
                    return ProcessingResult(
                        memo=memo,
                        created=False,
                        obsidian_synced=sync_status == "delivered",
                    )

                audio_path: Path | None = None
                if memo.audio_path:
                    audio_path = self.storage.resolve(memo.audio_path)
                    if not audio_path.is_file():
                        audio_path = None
                if audio_path is None:
                    stage = "audio_save"
                    if save_attachment is None:
                        raise FileNotFoundError("saved audio is missing; resend the attachment")
                    await _progress(progress, "saving audio")
                    temporary, final = self.storage.audio_paths(memo)
                    temporary.unlink(missing_ok=True)
                    await save_attachment(temporary)
                    await asyncio.to_thread(
                        self.storage.finalize_audio,
                        temporary,
                        final,
                        memo.size_bytes,
                    )
                    memo = await asyncio.to_thread(
                        self.database.update_memo,
                        memo_id,
                        status=MemoStatus.AUDIO_SAVED.value,
                        audio_path=self.storage.relative(final),
                        error_stage=None,
                        error_message=None,
                    )
                    audio_path = final
                    self._log_stage(memo_id, stage, started)

                if not memo.transcript:
                    stage = "transcription"
                    await _progress(progress, "transcribing")
                    transcript = await self.gateway.transcribe(audio_path)
                    memo = await asyncio.to_thread(
                        self.database.update_memo,
                        memo_id,
                        status=MemoStatus.TRANSCRIBED.value,
                        transcript=transcript,
                        error_stage=None,
                        error_message=None,
                    )
                    self._log_stage(memo_id, stage, started)

                if not memo.summary_json:
                    stage = "summary"
                    await _progress(progress, "summarizing")
                    summary = await self.gateway.summarize(memo.transcript or "")
                    memo = await asyncio.to_thread(
                        self.database.update_memo,
                        memo_id,
                        status=MemoStatus.SUMMARIZED.value,
                        summary_json=summary.to_json(),
                        summary_text=summary.summary,
                        error_stage=None,
                        error_message=None,
                    )
                    self._log_stage(memo_id, stage, started)
                else:
                    summary = memo.summary
                    if summary is None:
                        raise ValueError("stored structured summary is invalid")

                stage = "artifact_write"
                await _progress(progress, "writing notes")
                transcript_path, summary_path = await asyncio.to_thread(
                    self.storage.write_artifacts, memo, summary
                )
                memo = await asyncio.to_thread(
                    self.database.update_memo,
                    memo_id,
                    status=MemoStatus.ARTIFACTS_WRITTEN.value,
                    transcript_path=transcript_path,
                    summary_path=summary_path,
                )
                date_text = memo.received_at[:10]
                daily_path = await asyncio.to_thread(
                    self.storage.rebuild_daily_index,
                    date_text,
                    lambda: self.database.memos_for_date(date_text),
                )
                elapsed_ms = int((time.perf_counter() - started) * 1_000)
                await asyncio.to_thread(self.obsidian.enqueue, memo_id)
                memo = await asyncio.to_thread(
                    self.database.update_memo,
                    memo_id,
                    status=MemoStatus.COMPLETED.value,
                    daily_index_path=daily_path,
                    completed_at=datetime.now(UTC).isoformat(),
                    processing_ms=elapsed_ms,
                    error_stage=None,
                    error_message=None,
                )
                self._log_stage(memo_id, "completed", started)

                await _progress(progress, "syncing Obsidian")
                synced = False
                try:
                    await self.obsidian.flush()
                    sync_status = await asyncio.to_thread(
                        self.database.outbox_status, memo_id
                    )
                    synced = sync_status == "delivered"
                except Exception as exc:
                    log.error(
                        "post-completion sync failed memo_id=%s error_type=%s",
                        memo_id,
                        type(exc).__name__,
                    )
                return ProcessingResult(memo=memo, created=created, obsidian_synced=synced)
            except Exception as exc:
                await asyncio.to_thread(
                    self.database.record_failure,
                    memo_id,
                    stage,
                    f"{type(exc).__name__}: processing failed",
                )
                log.error(
                    "memo processing failed memo_id=%s stage=%s error_type=%s",
                    memo_id,
                    stage,
                    type(exc).__name__,
                )
                raise
            finally:
                self._in_flight.discard(memo_id)

    @staticmethod
    def _log_stage(memo_id: str, stage: str, started: float) -> None:
        elapsed_ms = int((time.perf_counter() - started) * 1_000)
        log.info("memo stage memo_id=%s stage=%s elapsed_ms=%d", memo_id, stage, elapsed_ms)

    @asynccontextmanager
    async def _memo_lock(self, memo_id: str):
        async with self._memo_locks_guard:
            lock, users = self._memo_locks.get(memo_id, (asyncio.Lock(), 0))
            self._memo_locks[memo_id] = (lock, users + 1)
        acquired = False
        try:
            await lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                lock.release()
            async with self._memo_locks_guard:
                current_lock, current_users = self._memo_locks[memo_id]
                if current_users <= 1:
                    self._memo_locks.pop(memo_id, None)
                else:
                    self._memo_locks[memo_id] = (current_lock, current_users - 1)


async def _progress(callback: ProgressCallback | None, stage: str) -> None:
    if callback is not None:
        try:
            await callback(stage)
        except Exception:
            log.warning("progress callback failed stage=%s", stage, exc_info=True)
