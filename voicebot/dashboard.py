"""Optional local web dashboard for the voice memo bot.

Same-process, stdlib-only, localhost-first. Every route except ``/healthz``
requires the dashboard token (query ``?token=...`` or
``X-Dashboard-Token`` header). Start it with::

    uv run python bot.py --dashboard

The dashboard is read-mostly: status, history with search, memo detail with
audio playback, log tail, and JSON/CSV export. The settings page can update
safe ``.env`` values; changes take effect after a bot restart.
"""

from __future__ import annotations

import csv
import hmac
import html
import io
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import __version__
from .config import ConfigurationError, Settings

log = logging.getLogger("voicebot.dashboard")

AUDIO_CONTENT_TYPES = {
    ".ogg": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".webm": "audio/webm",
    ".flac": "audio/flac",
    ".mp4": "audio/mp4",
    ".mpeg": "audio/mpeg",
    ".mpga": "audio/mpeg",
}

# key, label, kind. Kinds: provider, model, int, bool, text, timezone, path.
EDITABLE_SETTINGS: tuple[tuple[str, str, str], ...] = (
    ("STT_PROVIDER", "Speech-to-text provider (groq/openai)", "provider"),
    ("SUMMARY_PROVIDER", "Summary provider (groq/openai)", "provider"),
    ("SUMMARY_MODEL", "Primary summary model (empty = provider default)", "model"),
    ("SUMMARY_MODEL_FALLBACK", "Fallback summary model", "model"),
    ("TRANSCRIPTION_MODEL", "Transcription model (empty = provider default)", "model"),
    ("TRANSCRIPTION_LANGUAGE", "Transcription language hint (empty = auto)", "text"),
    ("MAX_FILE_SIZE_MB", "Max upload size in MB (1-100)", "int"),
    ("MAX_CONCURRENT_JOBS", "Max concurrent jobs (1-10)", "int"),
    ("TIMEZONE", "IANA timezone for notes", "timezone"),
    ("OBSIDIAN_ENABLED", "Enable Obsidian sync (true/false)", "bool"),
    ("OBSIDIAN_VAULT_PATH", "Obsidian vault directory", "path"),
    ("OBSIDIAN_SUBFOLDER", "Subfolder inside the vault", "text"),
    ("OBSIDIAN_REQUIRE_MOUNT", "Require vault path to be a mount (true/false)", "bool"),
    ("OBSIDIAN_QUEUE_CHECK_SECONDS", "Obsidian retry interval in seconds", "int"),
    ("RETENTION_DAYS", "Suggested cleanup age, 0 disables", "int"),
    ("LOG_LEVEL", "Log verbosity (DEBUG/INFO/WARNING/ERROR)", "text"),
    ("GROQ_TIMEOUT_SECONDS", "Groq timeout in seconds", "int"),
    ("GROQ_MAX_RETRIES", "Groq max retries", "int"),
    ("OPENAI_TIMEOUT_SECONDS", "OpenAI timeout in seconds", "int"),
    ("OPENAI_MAX_RETRIES", "OpenAI max retries", "int"),
)

SECRET_KEYS = ("DISCORD_TOKEN", "GROQ_API_KEY", "OPENAI_API_KEY")

CSS = """
:root{color-scheme:dark}*{box-sizing:border-box}
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#0d1117;
color:#e6edf3;margin:0;padding:0 16px 48px}
a{color:#58a6ff}nav{background:#161b22;border-bottom:1px solid #30363d;padding:12px 16px;
margin:0 -16px 20px;display:flex;gap:16px;flex-wrap:wrap;align-items:center}
nav b{font-size:17px}nav span.ver{color:#8b949e;font-size:12px}
.wrap{max-width:1080px;margin:0 auto}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;margin:16px 0}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px 14px}
.card h3{margin:0 0 8px;font-size:14px;color:#8b949e;text-transform:uppercase;letter-spacing:.04em}
.card .big{font-size:22px;font-weight:650}
table{width:100%;border-collapse:collapse;background:#161b22;border:1px solid #30363d;border-radius:8px;overflow:hidden}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #21262d;font-size:14px;vertical-align:top}
th{color:#8b949e;font-weight:600}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;background:#21262d}
.badge.completed{background:#1a472a}.badge.failed{background:#5c1a1a}
.badge.transcribed,.badge.summarized,.badge.audio_saved{background:#3b2f0b}
.banner{background:#3b2f0b;border:1px solid #9e6a03;border-radius:8px;padding:10px 14px;margin:12px 0}
.banner.ok{background:#0f2f1c;border-color:#1f6f43}
.banner.err{background:#3d1414;border-color:#8c1d1d}
form.inline{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}
input[type=text],input[type=number],select,textarea{background:#0d1117;color:#e6edf3;
border:1px solid #30363d;border-radius:6px;padding:8px 10px;font-size:14px}
input[type=text]{min-width:280px}button{background:#238636;color:#fff;border:0;border-radius:6px;
padding:8px 14px;font-size:14px;cursor:pointer}button:hover{background:#2ea043}
pre{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px;overflow:auto;
font-size:13px;white-space:pre-wrap}
.bars{display:flex;align-items:flex-end;gap:6px;height:110px;margin:8px 0}
.bar{flex:1;background:#1f6f43;border-radius:4px 4px 0 0;min-height:4px;position:relative}
.bar span{position:absolute;bottom:-20px;left:50%;transform:translateX(-50%);font-size:10px;color:#8b949e}
.muted{color:#8b949e;font-size:13px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:760px){.grid2{grid-template-columns:1fr}}
label.set{display:block;margin:10px 0}label.set small{color:#8b949e;display:block}
audio{width:100%;margin:8px 0}
"""


def resolve_dashboard_token(explicit: str | None) -> tuple[str, bool]:
    """Return (token, was_generated). Explicit flag wins, then env, then random."""
    if explicit and explicit.strip():
        return explicit.strip(), False
    from_env = os.getenv("DASHBOARD_TOKEN", "").strip()
    if from_env:
        return from_env, False
    return secrets.token_urlsafe(32), True


def read_env_file(path: Path) -> tuple[list[str], dict[str, str]]:
    """Parse a .env file, returning (lines, values). Missing file -> ([], {})."""
    lines: list[str] = []
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return lines, values
    for line in text.splitlines():
        lines.append(line)
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :]
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key.isupper():
            values.setdefault(key, value)
    return lines, values


def write_env_updates(path: Path, updates: dict[str, str]) -> None:
    """Update keys in a .env file, preserving comments and unknown lines."""
    lines, _ = read_env_file(path)
    remaining = dict(updates)
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        candidate = stripped[7:] if stripped.startswith("export ") else stripped
        key, sep, _ = candidate.partition("=")
        key = key.strip()
        if sep and key in remaining:
            output.append(f"{key}={remaining.pop(key)}")
        else:
            output.append(line)
    for key, value in remaining.items():
        output.append(f"{key}={value}")
    text = "\n".join(output).rstrip() + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def merged_disk_env(project_root: Path) -> dict[str, str]:
    """os.environ over .env file values, for validation and diffing."""
    _, file_values = read_env_file(project_root / ".env")
    merged = dict(file_values)
    merged.update(os.environ)
    return merged


class Dashboard:
    def __init__(
        self,
        settings: Settings,
        database,  # Database (untyped to avoid import cycles in tests)
        storage,  # FileStorage
        *,
        service=None,
        obsidian=None,
        start_time: float | None = None,
    ):
        self.settings = settings
        self.database = database
        self.storage = storage
        self.service = service
        self.obsidian = obsidian
        self.start_time = start_time if start_time is not None else time.monotonic()

    # -- data -----------------------------------------------------------
    def stats(self) -> dict:
        counts = self.database.status_counts()
        outbox = self.database.outbox_counts()
        last = self.database.last_completed()
        total_audio_bytes = 0
        audio_files = 0
        try:
            for path in self.storage.voice_log_dir.rglob("*"):
                if path.is_file() and path.suffix.lower() in AUDIO_CONTENT_TYPES:
                    audio_files += 1
                    try:
                        total_audio_bytes += path.stat().st_size
                    except OSError:
                        pass
        except OSError:
            pass
        try:
            db_bytes = self.database.path.stat().st_size
        except OSError:
            db_bytes = 0
        uptime = int(time.monotonic() - self.start_time)
        obsidian_available = None
        if self.settings.obsidian_enabled and self.obsidian is not None:
            try:
                obsidian_available = bool(self.obsidian.available())
            except OSError:
                obsidian_available = False
        return {
            "counts": counts,
            "total": sum(counts.values()),
            "outbox": outbox,
            "last_completed": last,
            "integrity": self.database.integrity_check(),
            "in_flight": self.service.in_flight_count if self.service else 0,
            "uptime_seconds": uptime,
            "audio_files": audio_files,
            "audio_mb": total_audio_bytes / (1024 * 1024),
            "db_kb": db_bytes / 1024,
            "obsidian_available": obsidian_available,
            "per_day": self.database.memo_day_counts(14),
        }

    def restart_needed(self) -> list[str]:
        """Editable keys whose on-disk values differ from the running config."""
        try:
            disk_settings = Settings.load(merged_disk_env(self.settings.project_root))
        except (ConfigurationError, ValueError):
            return []
        running = self._comparable(self.settings)
        on_disk = self._comparable(disk_settings)
        return sorted(key for key in running if running[key] != on_disk.get(key))

    @staticmethod
    def _comparable(settings: Settings) -> dict[str, str]:
        vault = settings.obsidian_vault_path
        return {
            "STT_PROVIDER": settings.stt_provider,
            "SUMMARY_PROVIDER": settings.summary_provider,
            "SUMMARY_MODEL": settings.summary_model,
            "SUMMARY_MODEL_FALLBACK": settings.summary_fallback_model,
            "TRANSCRIPTION_MODEL": settings.transcription_model,
            "TRANSCRIPTION_LANGUAGE": settings.transcription_language or "",
            "MAX_FILE_SIZE_MB": str(settings.max_file_size_bytes // (1024 * 1024)),
            "MAX_CONCURRENT_JOBS": str(settings.max_concurrent_jobs),
            "TIMEZONE": str(settings.timezone),
            "OBSIDIAN_ENABLED": str(settings.obsidian_enabled).lower(),
            "OBSIDIAN_VAULT_PATH": str(vault) if vault else "",
            "OBSIDIAN_SUBFOLDER": settings.obsidian_subfolder,
            "OBSIDIAN_REQUIRE_MOUNT": str(settings.obsidian_require_mount).lower(),
            "OBSIDIAN_QUEUE_CHECK_SECONDS": str(settings.obsidian_queue_check_seconds),
            "RETENTION_DAYS": str(settings.retention_days),
            "LOG_LEVEL": settings.log_level,
            "GROQ_TIMEOUT_SECONDS": str(settings.groq_timeout_seconds),
            "GROQ_MAX_RETRIES": str(settings.groq_max_retries),
            "OPENAI_TIMEOUT_SECONDS": str(settings.openai_timeout_seconds),
            "OPENAI_MAX_RETRIES": str(settings.openai_max_retries),
        }

    def disk_values(self) -> dict[str, str]:
        """Current on-disk values for the settings form (env over .env)."""
        merged = merged_disk_env(self.settings.project_root)
        defaults = self._comparable(self.settings)
        return {key: merged.get(key, defaults[key]) for key in defaults}

    def apply_settings(self, form: dict[str, str]) -> None:
        """Validate posted settings and persist them to .env (restart to apply)."""
        updates: dict[str, str] = {}
        editable = {key for key, _, _ in EDITABLE_SETTINGS}
        for key, raw in form.items():
            if key in ("token", "submit") or key not in editable:
                continue
            value = raw.strip()
            kind = next(kind for ekey, _, kind in EDITABLE_SETTINGS if ekey == key)
            updates[key] = self._validate_field(key, value, kind)
        if not updates:
            raise ConfigurationError("no editable settings in request")
        merged = merged_disk_env(self.settings.project_root)
        merged.update(updates)
        # Full validation before touching the file.
        Settings.load(merged, project_root=self.settings.project_root)
        write_env_updates(self.settings.project_root / ".env", updates)

    @staticmethod
    def _validate_field(key: str, value: str, kind: str) -> str:
        if kind == "provider":
            normalized = value.lower()
            if normalized not in ("groq", "openai"):
                raise ConfigurationError(f"{key} must be groq or openai")
            return normalized
        if kind == "bool":
            normalized = value.lower()
            if normalized not in ("true", "false", "1", "0", "yes", "no", "on", "off"):
                raise ConfigurationError(f"{key} must be true or false")
            return "true" if normalized in ("true", "1", "yes", "on") else "false"
        if kind == "int":
            try:
                number = int(value)
            except ValueError as exc:
                raise ConfigurationError(f"{key} must be an integer") from exc
            if key == "MAX_FILE_SIZE_MB" and not 1 <= number <= 100:
                raise ConfigurationError(f"{key} must be between 1 and 100")
            if key == "MAX_CONCURRENT_JOBS" and not 1 <= number <= 10:
                raise ConfigurationError(f"{key} must be between 1 and 10")
            if key == "OBSIDIAN_QUEUE_CHECK_SECONDS" and not 30 <= number <= 86_400:
                raise ConfigurationError(f"{key} must be between 30 and 86400")
            if key == "RETENTION_DAYS" and not 0 <= number <= 36_500:
                raise ConfigurationError(f"{key} must be between 0 and 36500")
            if number < 0:
                raise ConfigurationError(f"{key} must not be negative")
            return str(number)
        if kind == "timezone":
            try:
                ZoneInfo(value)
            except ZoneInfoNotFoundError as exc:
                raise ConfigurationError(f"{key} is not a known timezone: {value}") from exc
            return value
        if kind == "path":
            if ".." in Path(value).parts:
                raise ConfigurationError(f"{key} must not contain '..'")
            return value
        return value


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


class DashboardHandler(BaseHTTPRequestHandler):
    dashboard: Dashboard
    token: str = ""
    server_version = "VoicebotDashboard/1.0"

    # -- helpers --------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:
        log.info("dashboard %s", fmt % args)

    def _query(self) -> dict[str, str]:
        return {
            key: values[0] for key, values in parse_qs(urlsplit(self.path).query).items() if values
        }

    def _authorized(self, query: dict[str, str]) -> bool:
        if self.path == "/healthz" or urlsplit(self.path).path == "/healthz":
            return True
        candidate = self.headers.get("X-Dashboard-Token", "") or query.get("token", "") or ""
        return bool(self.token) and hmac.compare_digest(candidate, self.token)

    def _send(self, status: int, body: str, content_type: str = "text/html; charset=utf-8") -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _token_qs(self, query: dict[str, str]) -> str:
        return urlencode({"token": query.get("token", self.token)})

    # -- pages ----------------------------------------------------------
    def _page(self, title: str, body: str, query: dict[str, str]) -> str:
        dash = self.dashboard
        restart = dash.restart_needed()
        banner = ""
        if restart:
            banner = (
                '<div class="banner">Settings changed on disk differ from the running '
                f"config ({len(restart)} key(s)). Restart the bot to apply.</div>"
            )
        elif query.get("saved"):
            banner = '<div class="banner ok">Settings saved. Restart the bot to apply.</div>'
        if query.get("error"):
            banner += f'<div class="banner err">{esc(query["error"])}</div>'
        qs = self._token_qs(query)
        return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · voicebot</title><style>{CSS}</style></head><body>
<nav><b>voicebot</b><span class="ver">v{esc(__version__)}</span>
<a href="/?{qs}">Status</a><a href="/history?{qs}">History</a>
<a href="/settings?{qs}">Settings</a><a href="/logs?{qs}">Logs</a>
<a href="/export?format=json&{qs}">Export JSON</a>
<a href="/export?format=csv&{qs}">Export CSV</a></nav>
<div class="wrap">{banner}{body}</div></body></html>"""

    def _overview(self, query: dict[str, str]) -> str:
        dash = self.dashboard
        stats = dash.stats()
        settings = dash.settings
        hours, remainder = divmod(stats["uptime_seconds"], 3600)
        minutes = remainder // 60
        if settings.obsidian_enabled:
            if stats["obsidian_available"]:
                vault_state = "available"
            elif stats["obsidian_available"] is False:
                vault_state = "unavailable"
            else:
                vault_state = "unknown"
            obsidian_line = (
                f"Enabled · {esc(vault_state)} · pending sync: {stats['outbox'].get('pending', 0)}"
            )
        else:
            obsidian_line = "Disabled — memos are saved locally."
        counts = stats["counts"]
        completed = counts.get("completed", 0)
        failed = counts.get("failed", 0)
        last = stats["last_completed"]
        last_text = esc(last.received_at[:19]) if last else "none"
        days = stats["per_day"][::-1]
        peak = max((count for _, count in days), default=0)
        bars = "".join(
            f'<div class="bar" style="height:{100 * count // peak if peak else 4}%">'
            f"<span>{esc(day[5:])}</span></div>"
            for day, count in days
        )
        failures = dash.database.recent_memos(5, status="failed")
        if failures:
            fail_rows = "".join(
                f'<tr><td><a href="/memo?id={esc(m.memo_id)}&{self._token_qs(query)}">'
                f"{esc(m.memo_id)}</a></td><td>{esc(m.received_at[:16])}</td>"
                f"<td>{esc(m.error_stage or '')}</td></tr>"
                for m in failures
            )
            fail_html = (
                "<h2>Recent failures</h2><table><tr><th>Memo</th><th>Received</th>"
                f"<th>Stage</th></tr>{fail_rows}</table>"
                '<p class="muted">Resume from Discord with '
                "<code>!retry &lt;memo-id&gt;</code>.</p>"
            )
        else:
            fail_html = "<h2>Recent failures</h2><p class='muted'>None. All clear.</p>"
        return f"""
<h1>Status</h1>
<div class="cards">
<div class="card"><h3>Bot</h3><div class="big">{hours}h {minutes}m</div>
<div class="muted">uptime · in flight: {stats["in_flight"]} · DB: {esc(stats["integrity"])}</div></div>
<div class="card"><h3>Providers</h3><div class="big">{esc(settings.stt_provider)} / {esc(settings.summary_provider)}</div>
<div class="muted">STT: {esc(settings.transcription_model)}<br>Summary: {esc(settings.summary_model)}</div></div>
<div class="card"><h3>Memos</h3><div class="big">{completed} ✓ · {failed} ✗</div>
<div class="muted">total: {stats["total"]} · last completed: {last_text}</div></div>
<div class="card"><h3>Obsidian</h3><div>{obsidian_line}</div></div>
<div class="card"><h3>Storage</h3><div class="big">{stats["audio_mb"]:.1f} MB</div>
<div class="muted">{stats["audio_files"]} audio files · DB {stats["db_kb"]:.0f} KB</div></div>
</div>
<h2>Memos per day (14 days)</h2><div class="bars">{bars or "<span class=muted>No memos yet.</span>"}</div>
{fail_html}"""

    def _history(self, query: dict[str, str]) -> str:
        dash = self.dashboard
        term = query.get("q", "").strip()
        status = query.get("status", "").strip()
        if term:
            memos = dash.database.search_all(term)
            heading = f"Search results for “{esc(term)}” ({len(memos)})"
        else:
            memos = dash.database.recent_memos(100, status or None)
            heading = "Recent memos" + (f" · status={esc(status)}" if status else "")
        qs = self._token_qs(query)
        rows = "".join(
            "<tr>"
            f'<td><a href="/memo?id={esc(m.memo_id)}&{qs}">{esc(m.memo_id)}</a></td>'
            f"<td>{esc(m.received_at[:16])}</td>"
            f"<td>{esc(m.username)}</td>"
            f"<td>{esc(Path(m.original_filename).name)}</td>"
            f'<td><span class="badge {esc(m.status)}">{esc(m.status)}</span></td>'
            f"<td>{esc(_teaser(m)[:140])}</td></tr>"
            for m in memos
        )
        return f"""
<h1>History</h1>
<form class="inline" method="get" action="/history">
<input type="hidden" name="token" value="{esc(query.get("token", self.token))}">
<input type="text" name="q" placeholder="Search transcripts & summaries…" value="{esc(term)}">
<select name="status"><option value="">all statuses</option>
{"".join(f'<option value="{s}"{" selected" if status == s else ""}>{s}</option>' for s in ("completed", "failed", "received", "audio_saved", "transcribed", "summarized", "artifacts_written"))}
</select><button type="submit">Search</button></form>
<h2>{heading}</h2>
<table><tr><th>Memo</th><th>Received</th><th>User</th><th>File</th><th>Status</th><th>Teaser</th></tr>
{rows or '<tr><td colspan="6" class="muted">No memos found.</td></tr>'}</table>"""

    def _memo_detail(self, query: dict[str, str]) -> str:
        dash = self.dashboard
        memo_id = query.get("id", "")
        memo = dash.database.get_memo(memo_id) if memo_id else None
        if memo is None:
            return "<h1>Memo not found</h1><p class='muted'>Check the memo id.</p>"
        qs = self._token_qs(query)
        try:
            summary = memo.summary
        except ValueError:
            summary = None
        if summary is not None:
            parts = [f"<h3>Summary</h3><p>{esc(summary.summary)}</p>"]
            for title, values in (
                ("Key points", summary.key_points),
                ("Decisions", summary.decisions),
                ("Mentioned", summary.mentioned),
            ):
                if values:
                    items = "".join(f"<li>{esc(v)}</li>" for v in values)
                    parts.append(f"<h3>{title}</h3><ul>{items}</ul>")
            if summary.action_items:
                items = "".join(
                    f"<li>{esc(i.task)}"
                    f"{' · owner: ' + esc(i.owner) if i.owner else ''}"
                    f"{' · deadline: ' + esc(i.deadline) if i.deadline else ''}</li>"
                    for i in summary.action_items
                )
                parts.append(f"<h3>Action items</h3><ul>{items}</ul>")
            summary_html = "".join(parts)
        else:
            summary_html = "<p class='muted'>No summary yet.</p>"
        audio_html = ""
        if memo.audio_path:
            try:
                dash.storage.resolve(memo.audio_path)
                audio_html = (
                    '<h3>Audio</h3><audio controls preload="none" '
                    f'src="/audio?id={esc(memo.memo_id)}&{qs}"></audio>'
                )
            except ValueError:
                audio_html = "<p class='muted'>Audio file is missing.</p>"
        sync = dash.database.outbox_status(memo.memo_id)
        return f"""
<h1><span class="badge {esc(memo.status)}">{esc(memo.status)}</span> {esc(memo.memo_id)}</h1>
<p class="muted">{esc(memo.received_at)} · {esc(memo.username)} ·
{esc(Path(memo.original_filename).name)} ·
attempts: {memo.attempt_count} · obsidian: {esc(sync or "n/a")}</p>
{audio_html}{summary_html}
<h3>Transcript</h3><pre>{esc(memo.transcript or "No transcript yet.")}</pre>"""

    def _settings(self, query: dict[str, str]) -> str:
        dash = self.dashboard
        values = dash.disk_values()
        rows = ""
        for key, label, kind in EDITABLE_SETTINGS:
            current = values.get(key, "")
            if kind == "provider":
                options = "".join(
                    f"<option{' selected' if current == p else ''}>{p}</option>"
                    for p in ("groq", "openai")
                )
                control = f'<select name="{key}">{options}</select>'
            elif kind == "bool":
                options = "".join(
                    f'<option value="{v}"{" selected" if current == v else ""}>{v}</option>'
                    for v in ("true", "false")
                )
                control = f'<select name="{key}">{options}</select>'
            else:
                control = (
                    f'<input type="text" name="{key}" value="{esc(current)}" '
                    'style="min-width:340px">'
                )
            rows += f'<label class="set">{esc(label)}<small>{esc(key)}</small>{control}</label>'
        secrets = "".join(
            f"<tr><td><code>{key}</code></td><td>"
            f"{'•••••• (set)' if getattr(dash.settings, key.lower()) else '(not set)'}</td></tr>"
            for key in SECRET_KEYS
        )
        return f"""
<h1>Settings</h1>
<p class="muted">Secrets are managed in <code>.env</code> and never shown here.
Edits below are validated and written to <code>.env</code>; restart the bot to apply them.</p>
<form method="post" action="/settings?{self._token_qs(query)}">
<input type="hidden" name="token" value="{esc(query.get("token", self.token))}">
{rows}<button type="submit">Save settings</button></form>
<h2>Secrets (masked)</h2><table><tr><th>Key</th><th>State</th></tr>{secrets}</table>"""

    def _logs(self, query: dict[str, str]) -> str:
        dash = self.dashboard
        try:
            count = max(10, min(int(query.get("n", "200")), 2000))
        except ValueError:
            count = 200
        path = dash.settings.log_dir / "voicebot.log"
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()[-count:]
        except OSError:
            return "<h1>Logs</h1><p class='muted'>No log file yet.</p>"
        return (
            "<h1>Logs</h1><p class='muted'>Last "
            f"{len(lines)} lines of <code>{esc(path.name)}</code>.</p>"
            f"<pre>{esc(''.join(lines))}</pre>"
        )

    # -- routing ---------------------------------------------------------
    def do_GET(self) -> None:
        query = self._query()
        if not self._authorized(query):
            self._send(403, "<h1>Forbidden</h1><p>Valid dashboard token required.</p>")
            return
        path = urlsplit(self.path).path
        try:
            if path == "/healthz":
                self._send(
                    200, json.dumps({"status": "ok", "version": __version__}), "application/json"
                )
            elif path == "/":
                self._send(200, self._page("Status", self._overview(query), query))
            elif path == "/history":
                self._send(200, self._page("History", self._history(query), query))
            elif path == "/memo":
                self._send(200, self._page("Memo", self._memo_detail(query), query))
            elif path == "/audio":
                self._serve_audio(query)
            elif path == "/settings":
                self._send(200, self._page("Settings", self._settings(query), query))
            elif path == "/logs":
                self._send(200, self._page("Logs", self._logs(query), query))
            elif path == "/export":
                self._serve_export(query)
            else:
                self._send(404, "<h1>Not found</h1>")
        except BrokenPipeError:
            pass
        except Exception:  # noqa: BLE001 - never leak tracebacks with private data
            log.exception("dashboard request failed")
            try:
                self._send(500, "<h1>Something went wrong</h1><p>Check the bot logs.</p>")
            except BrokenPipeError:
                pass

    def do_POST(self) -> None:
        query = self._query()
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 32 * 1024:
            self._send(413, "<h1>Request too large</h1>")
            return
        raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        form = {key: values[0] for key, values in parse_qs(raw).items() if values}
        form_token = form.get("token", query.get("token", ""))
        if not (self.token and hmac.compare_digest(form_token, self.token)):
            self._send(403, "<h1>Forbidden</h1><p>Valid dashboard token required.</p>")
            return
        if urlsplit(self.path).path != "/settings":
            self._send(404, "<h1>Not found</h1>")
            return
        try:
            self.dashboard.apply_settings(form)
        except (ConfigurationError, ValueError) as exc:
            location = f"/settings?{self._token_qs(query)}&" + urlencode({"error": str(exc)})
            self._redirect(location)
            return
        except OSError as exc:
            location = f"/settings?{self._token_qs(query)}&" + urlencode(
                {"error": f"could not write .env: {type(exc).__name__}"}
            )
            self._redirect(location)
            return
        self._redirect(f"/settings?{self._token_qs(query)}&saved=1")

    def _serve_audio(self, query: dict[str, str]) -> None:
        dash = self.dashboard
        memo = dash.database.get_memo(query.get("id", ""))
        if memo is None or not memo.audio_path:
            self._send(404, "<h1>Audio not found</h1>")
            return
        try:
            path = dash.storage.resolve(memo.audio_path)
            data = path.read_bytes()
        except (ValueError, OSError):
            self._send(404, "<h1>Audio not found</h1>")
            return
        content_type = AUDIO_CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_export(self, query: dict[str, str]) -> None:
        dash = self.dashboard
        memos = dash.database.recent_memos(500)
        fmt = query.get("format", "json")
        if fmt == "csv":
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(
                [
                    "memo_id",
                    "received_at",
                    "status",
                    "username",
                    "filename",
                    "transcript",
                    "summary",
                ]
            )
            for memo in memos:
                writer.writerow(
                    [
                        memo.memo_id,
                        memo.received_at,
                        memo.status,
                        memo.username,
                        memo.original_filename,
                        memo.transcript or "",
                        memo.summary_text or "",
                    ]
                )
            body = buffer.getvalue().encode("utf-8")
            filename = "memos.csv"
            content_type = "text/csv; charset=utf-8"
        else:
            body = json.dumps([asdict(m) for m in memos], ensure_ascii=False, indent=2)
            body = (body + "\n").encode("utf-8")
            filename = "memos.json"
            content_type = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def _teaser(memo) -> str:
    try:
        summary = memo.summary
    except ValueError:
        summary = None
    if summary is not None:
        return summary.teaser()
    if memo.summary_text:
        return " ".join(memo.summary_text.split())[:220]
    if memo.transcript:
        return " ".join(memo.transcript.split())[:220]
    return "No summary yet."


def sanitize_log_path(path: str) -> str:
    """Strip any token from a URL path for logging."""
    try:
        parts = urlsplit(path)
        params = parse_qs(parts.query)
        params.pop("token", None)
        redacted = urlencode({k: v[0] for k, v in params.items() if v})
        return parts.path + (f"?{redacted}" if redacted else "")
    except ValueError:
        return "<unparseable-path>"


class TokenRedactingHandler(DashboardHandler):
    def log_message(self, fmt: str, *args) -> None:
        message = fmt % args
        # BaseHTTPRequestHandler logs the raw request line; redact any token.
        if "token=" in message:
            message = message.split("token=")[0] + "token=<redacted>"
        log.info("dashboard %s", message)


def start_dashboard(
    settings: Settings,
    database,
    storage,
    *,
    service=None,
    obsidian=None,
    host: str = "127.0.0.1",
    port: int = 8080,
    token: str = "",
    handler: type[DashboardHandler] = TokenRedactingHandler,
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    """Serve the dashboard in a background thread. Returns (server, thread)."""
    dashboard = Dashboard(
        settings,
        database,
        storage,
        service=service,
        obsidian=obsidian,
        start_time=time.monotonic(),
    )
    handler_cls = type(
        "BoundDashboardHandler",
        (handler,),
        {"dashboard": dashboard, "token": token},
    )
    server = ThreadingHTTPServer((host, port), handler_cls)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="voicebot-dashboard", daemon=True)
    thread.start()
    if host not in ("127.0.0.1", "localhost", "::1"):
        log.warning(
            "dashboard listening on non-local address host=%s; "
            "keep the token secret and prefer a reverse proxy with TLS",
            host,
        )
    log.info("dashboard listening host=%s port=%d", host, server.server_port)
    return server, thread
