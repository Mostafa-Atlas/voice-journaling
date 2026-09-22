from __future__ import annotations

import html
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from pathlib import Path

from .models import Memo, SummaryData, escape_markdown

SUPPORTED_EXTENSIONS = frozenset(
    {".ogg", ".mp3", ".wav", ".m4a", ".webm", ".flac", ".mp4", ".mpeg", ".mpga"}
)


class UnsafePathError(ValueError):
    pass


class FileStorage:
    def __init__(self, data_root: Path, voice_log_dir: Path):
        self.data_root = data_root.resolve()
        self.voice_log_dir = voice_log_dir.resolve()
        self.daily_dir = self.voice_log_dir / "daily"
        self._assert_inside(self.voice_log_dir, self.data_root)
        self._ensure_directory(self.voice_log_dir)
        self._ensure_directory(self.daily_dir)

    def validate_extension(self, filename: str) -> str:
        suffix = Path(filename).suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            accepted = ", ".join(sorted(SUPPORTED_EXTENSIONS))
            raise ValueError(f"unsupported audio type; accepted: {accepted}")
        return suffix

    def memo_directory(self, memo_id: str, received_at: str) -> Path:
        path = self._memo_directory_path(memo_id, received_at)
        self._ensure_directory(path)
        return path

    def _memo_directory_path(self, memo_id: str, received_at: str) -> Path:
        safe_id = safe_component(memo_id, fallback="memo")
        date_text = (
            received_at[:10]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", received_at[:10])
            else "unknown-date"
        )
        path = (self.voice_log_dir / date_text / safe_id).resolve()
        self._assert_inside(path, self.voice_log_dir)
        return path

    def audio_paths(self, memo: Memo) -> tuple[Path, Path]:
        suffix = self.validate_extension(memo.original_filename)
        directory = self.memo_directory(memo.memo_id, memo.received_at)
        final = directory / f"audio{suffix}"
        temporary = directory / f".audio{suffix}.part"
        return temporary, final

    def finalize_audio(self, temporary: Path, final: Path, expected_size: int) -> None:
        self._assert_inside(temporary.resolve(), self.voice_log_dir)
        self._assert_inside(final.resolve(), self.voice_log_dir)
        if not temporary.is_file():
            raise FileNotFoundError(temporary)
        actual_size = temporary.stat().st_size
        if actual_size != expected_size:
            temporary.unlink(missing_ok=True)
            raise OSError(
                f"audio download size mismatch: expected {expected_size} bytes, got {actual_size}"
            )
        with temporary.open("rb") as handle:
            try:
                os.fsync(handle.fileno())
            except OSError:
                # Windows disallows fsync on read-only handles; the data was
                # already flushed by the writer. Durability still holds after
                # os.replace below + directory sync on POSIX.
                pass
        os.replace(temporary, final)
        _private_file(final)

    def write_artifacts(self, memo: Memo, summary: SummaryData) -> tuple[str, str]:
        directory = self.memo_directory(memo.memo_id, memo.received_at)
        transcript_path = directory / "transcript.md"
        summary_path = directory / "summary.md"
        daily_relative = (
            Path("..").joinpath("..", "daily", f"{memo.received_at[:10]}.md").as_posix()
        )
        transcript = memo.transcript or ""
        transcript_lines = "\n".join(
            f"> {escape_markdown(line)}" for line in transcript.splitlines()
        )
        transcript_content = (
            "# Voice Transcript\n\n"
            f"[← Back to {memo.received_at[:10]}]({daily_relative})\n\n"
            f"- **Memo ID:** `{_inline_code(memo.memo_id)}`\n"
            f"- **User:** {escape_markdown(memo.username)}\n"
            f"- **Discord ID:** `{_inline_code(memo.discord_id)}`\n"
            f"- **Received:** {html.escape(memo.received_at)}\n"
            f"- **Original audio:** `{_inline_code(Path(memo.original_filename).name)}`\n\n"
            "## Transcript\n\n"
            "> [!quote] Raw transcript\n"
            f"{transcript_lines}\n"
        )
        summary_content = (
            "# Voice Summary\n\n"
            f"[← Back to {memo.received_at[:10]}]({daily_relative})\n\n"
            f"- **Memo ID:** `{_inline_code(memo.memo_id)}`\n"
            f"- **User:** {escape_markdown(memo.username)}\n"
            f"- **Received:** {html.escape(memo.received_at)}\n\n"
            f"{summary.to_markdown()}"
        )
        atomic_write(transcript_path, transcript_content)
        atomic_write(summary_path, summary_content)
        return self.relative(transcript_path), self.relative(summary_path)

    def rebuild_daily_index(
        self,
        date_text: str,
        memos: Iterable[Memo] | Callable[[], Iterable[Memo]],
    ) -> str:
        index_path = self.daily_dir / f"{date_text}.md"
        lock_path = self.daily_dir / f".{date_text}.lock"
        with interprocess_file_lock(lock_path):
            current_memos = list(memos() if callable(memos) else memos)
            return self._write_daily_index(index_path, date_text, current_memos)

    def _write_daily_index(self, index_path: Path, date_text: str, memos: Iterable[Memo]) -> str:
        sections = [
            "---",
            "generated: true",
            f"date: {date_text}",
            "---",
            "",
            f"# Voice Memos — {date_text}",
            "",
            "> This index is generated from SQLite. Edit individual memo files instead.",
            "",
        ]
        for memo in memos:
            transcript_path = self.resolve(memo.transcript_path) if memo.transcript_path else None
            summary_path = self.resolve(memo.summary_path) if memo.summary_path else None
            if not transcript_path or not summary_path:
                continue
            transcript_link = Path(os.path.relpath(transcript_path, index_path.parent)).as_posix()
            summary_link = Path(os.path.relpath(summary_path, index_path.parent)).as_posix()
            time_text = _display_time(memo.received_at)
            teaser = _memo_teaser(memo)
            sections.extend(
                [
                    f"<!-- voice-memo:{memo.memo_id} -->",
                    f"## {time_text} — {escape_markdown(_display_text(Path(memo.original_filename).name))}",
                    "",
                    teaser,
                    "",
                    f"[Full transcript]({transcript_link}) · [Full summary]({summary_link})",
                    "",
                    f"Status: `{memo.status}` · Memo: `{_inline_code(memo.memo_id)}`",
                    "",
                    "---",
                    "",
                ]
            )
        atomic_write(index_path, "\n".join(sections).rstrip() + "\n")
        return self.relative(index_path)

    def relative(self, path: Path) -> str:
        resolved = path.resolve()
        self._assert_inside(resolved, self.data_root)
        return resolved.relative_to(self.data_root).as_posix()

    def resolve(self, stored_path: str | None) -> Path:
        if not stored_path:
            raise ValueError("stored path is empty")
        path = Path(stored_path)
        resolved = (path if path.is_absolute() else self.data_root / path).resolve()
        self._assert_inside(resolved, self.data_root)
        return resolved

    def delete_memo_files(self, memo: Memo) -> None:
        expected = self._memo_directory_path(memo.memo_id, memo.received_at)
        candidates = [memo.audio_path, memo.transcript_path, memo.summary_path]
        for stored in candidates:
            if not stored:
                continue
            path = self.resolve(stored)
            self._assert_inside(path, self.voice_log_dir)
            if path.parent != expected:
                raise UnsafePathError(f"stored memo path is outside its expected directory: {path}")
        if expected.exists():
            self._assert_inside(expected, self.voice_log_dir)
            shutil.rmtree(expected)

    @staticmethod
    def _assert_inside(path: Path, root: Path) -> None:
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise UnsafePathError(f"path escapes configured root: {path}") from exc

    @staticmethod
    def _ensure_directory(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        try:
            path.chmod(0o700)
        except OSError:
            pass


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _private_file(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


@contextmanager
def interprocess_file_lock(path: Path):
    """Cross-platform advisory lock for read-modify-replace note operations."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def safe_component(value: str, *, fallback: str = "item", max_length: int = 120) -> str:
    value = Path(value).name
    value = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "_", value)
    value = re.sub(r"\s+", "_", value).strip(" ._")
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if value.upper() in reserved:
        value = f"_{value}"
    return value[:max_length].rstrip(" .") or fallback


def _display_time(timestamp: str) -> str:
    try:
        from datetime import datetime

        parsed = datetime.fromisoformat(timestamp)
        return parsed.strftime("%I:%M %p").lstrip("0")
    except (ValueError, TypeError):
        return timestamp[11:19] if len(timestamp) >= 19 else timestamp


def _memo_teaser(memo: Memo) -> str:
    if memo.summary_json:
        try:
            return escape_markdown(SummaryData.from_json(memo.summary_json).teaser())
        except (ValueError, TypeError):
            pass
    if memo.summary_text:
        return escape_markdown(" ".join(memo.summary_text.split())[:220])
    return "Summary unavailable."


def _inline_code(value: str) -> str:
    return _display_text(value).replace("`", "ˋ")


def _display_text(value: str) -> str:
    return " ".join(value.split())


def _private_file(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass
