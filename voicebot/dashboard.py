"""Optional local web dashboard for the voice memo bot.

Same-process, stdlib-only, localhost-first. Every route except ``/healthz``
requires the dashboard token (query ``?token=...`` or
``X-Dashboard-Token`` header). Start it with::

    uv run python bot.py --dashboard

The dashboard is read-mostly: status, history with search, memo detail with
audio playback, log tail, and JSON/CSV export. The settings page can update
safe ``.env`` values; the running bot restarts itself to apply them
(watch ``voicebot.reload``), or use the Restart button.
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
from .reload import restart_process

log = logging.getLogger("voicebot.dashboard")

# Delay before a dashboard-requested restart so the HTTP response is flushed.
RESTART_DELAY_SECONDS = 0.5


def _restart_from_dashboard() -> None:
    restart_process("restart requested from dashboard")


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
:root{
  color-scheme:dark;
  --bg:#090d13; --panel:#111823; --panel-2:#151e2c; --inset:#0b1018;
  --border:#26314a; --border-soft:#1a2334;
  --text:#e9eff7; --muted:#93a1b8; --faint:#5f6f88;
  --accent:#3fb950; --accent-2:#39c5cf; --link:#6cb6ff;
  --danger:#f85149; --warn:#d29922; --info:#58a6ff; --violet:#bc8cff;
  --radius:14px; --radius-s:9px;
  --shadow:0 10px 28px rgba(0,0,0,.38);
  --font:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  --mono:ui-monospace,SFMono-Regular,"Cascadia Mono",Consolas,monospace;
}
*{box-sizing:border-box}
html{scrollbar-color:#2c3a52 var(--bg)}
body{font-family:var(--font);background:var(--bg);color:var(--text);
margin:0;padding:0 20px 64px;line-height:1.55;
background-image:radial-gradient(900px 320px at 15% -80px,rgba(63,185,80,.10),transparent 60%),
radial-gradient(800px 300px at 85% -60px,rgba(57,197,207,.08),transparent 60%);}
::selection{background:rgba(88,166,255,.35)}
a{color:var(--link);text-decoration:none}
a:hover{text-decoration:underline}
code{font-family:var(--mono);font-size:.86em;background:#1b2536;
border:1px solid var(--border-soft);border-radius:6px;padding:1px 6px}
/* ---- nav ---- */
nav{position:sticky;top:0;z-index:50;background:rgba(13,18,28,.82);
backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
border-bottom:1px solid var(--border-soft);padding:12px 20px;margin:0 -20px 28px;
display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.brand{display:flex;align-items:center;gap:10px;font-weight:700;font-size:17px;
margin-right:14px;color:var(--text)}
.brand .dot{width:11px;height:11px;border-radius:50%;
background:linear-gradient(135deg,var(--accent),var(--accent-2));
box-shadow:0 0 12px rgba(63,185,80,.7)}
.brand .ver{color:var(--faint);font-size:12px;font-weight:500}
nav a.navlink{color:var(--muted);font-size:14px;font-weight:550;padding:7px 13px;
border-radius:999px;border:1px solid transparent;transition:all .15s ease}
nav a.navlink:hover{color:var(--text);background:#1a2334;text-decoration:none}
nav a.navlink[aria-current="page"]{color:var(--text);background:#1c2942;
border-color:var(--border)}
nav .spacer{flex:1}
/* ---- layout ---- */
.wrap{max-width:1080px;margin:0 auto}
.pagehead{margin:4px 0 18px}
.pagehead h1{font-size:30px;margin:0;letter-spacing:-.02em}
.lede{color:var(--muted);margin:6px 0 0;font-size:15px}
h1{font-size:28px;letter-spacing:-.02em;margin:6px 0 4px}
h2{font-size:18px;margin:30px 0 12px;letter-spacing:-.01em}
h3{font-size:15px;margin:0 0 10px}
footer{margin-top:44px;padding-top:16px;border-top:1px solid var(--border-soft);
color:var(--faint);font-size:12.5px;display:flex;gap:8px;flex-wrap:wrap}
/* ---- cards ---- */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));
gap:14px;margin:18px 0 6px}
.card{position:relative;background:linear-gradient(180deg,var(--panel-2),var(--panel));
border:1px solid var(--border-soft);border-radius:var(--radius);padding:16px 18px;
box-shadow:var(--shadow);overflow:hidden;transition:transform .15s ease,border-color .15s ease}
.card:hover{transform:translateY(-2px);border-color:var(--border)}
.card::before{content:"";position:absolute;inset:0 0 auto 0;height:3px;
background:linear-gradient(90deg,var(--accent),var(--accent-2))}
.card--providers::before{background:linear-gradient(90deg,var(--info),var(--violet))}
.card--memos::before{background:linear-gradient(90deg,var(--warn),#f778ba)}
.card--obsidian::before{background:linear-gradient(90deg,var(--violet),var(--info))}
.card--storage::before{background:linear-gradient(90deg,var(--accent-2),var(--accent))}
.card h3{font-size:12px;color:var(--muted);text-transform:uppercase;
letter-spacing:.09em;font-weight:650}
.card .big{font-size:24px;font-weight:700;letter-spacing:-.01em}
/* ---- panels & prose ---- */
.panel{background:linear-gradient(180deg,var(--panel-2),var(--panel));
border:1px solid var(--border-soft);border-radius:var(--radius);padding:18px 20px;
margin:16px 0;box-shadow:var(--shadow)}
.panel h3{color:var(--muted);font-size:12.5px;text-transform:uppercase;letter-spacing:.08em}
.prose p{margin:8px 0 14px;font-size:15px}
.prose ul{margin:8px 0 16px;padding-left:22px}
.prose li{margin:5px 0}
.prose li::marker{color:var(--accent)}
.meta{display:flex;flex-wrap:wrap;gap:8px;align-items:center;color:var(--muted);
font-size:13.5px;margin:10px 0 4px}
.meta .chip{background:#1b2536;border:1px solid var(--border-soft);border-radius:999px;
padding:2px 11px;font-size:12.5px}
/* ---- tables ---- */
.table-wrap{overflow-x:auto;border:1px solid var(--border-soft);border-radius:var(--radius);
background:var(--panel);box-shadow:var(--shadow)}
table{width:100%;border-collapse:collapse;min-width:640px}
th,td{text-align:left;padding:10px 14px;border-bottom:1px solid var(--border-soft);
font-size:14px;vertical-align:top}
thead th,table tr:first-child th{position:sticky;top:0;background:#182134;color:var(--muted);
font-weight:650;font-size:12px;text-transform:uppercase;letter-spacing:.07em;white-space:nowrap}
tbody tr,table tr:last-child td{border-bottom:0}
tbody tr:hover,table tr:hover td{background:rgba(88,166,255,.05)}
td.mono{font-family:var(--mono);font-size:12.5px}
/* ---- badges ---- */
.badge{display:inline-flex;align-items:center;gap:6px;padding:3px 11px 3px 9px;
border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap;
background:#212b3d;color:#c6d2e3;border:1px solid var(--border)}
.badge::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor}
.badge.completed{background:rgba(63,185,80,.13);color:#56d364;border-color:rgba(63,185,80,.4)}
.badge.failed{background:rgba(248,81,73,.13);color:#ff7b72;border-color:rgba(248,81,73,.4)}
.badge.received{background:rgba(88,166,255,.13);color:#79c0ff;border-color:rgba(88,166,255,.4)}
.badge.audio_saved{background:rgba(188,140,255,.14);color:#d2a8ff;border-color:rgba(188,140,255,.4)}
.badge.transcribed,.badge.summarized{background:rgba(210,153,34,.15);color:#e3b341;
border-color:rgba(210,153,34,.45)}
.badge.artifacts_written{background:rgba(57,197,207,.13);color:#56d4dd;
border-color:rgba(57,197,207,.4)}
/* ---- banners ---- */
.banner{background:#1c1a12;border:1px solid #6b5518;border-left:4px solid var(--warn);
border-radius:var(--radius-s);padding:12px 16px;margin:14px 0;font-size:14px}
.banner.ok{background:#0d2117;border-color:#1f6f43;border-left-color:var(--accent)}
.banner.err{background:#2a1215;border-color:#8c1d1d;border-left-color:var(--danger)}
/* ---- forms ---- */
form.inline{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0;align-items:center}
input[type=text],input[type=number],select,textarea{background:var(--inset);color:var(--text);
border:1px solid var(--border);border-radius:var(--radius-s);padding:9px 12px;font-size:14px;
font-family:inherit;transition:border-color .15s ease,box-shadow .15s ease;max-width:100%}
input[type=text]{min-width:min(280px,100%)}
input:focus-visible,select:focus-visible,textarea:focus-visible,button:focus-visible,
a:focus-visible{outline:2px solid var(--link);outline-offset:2px}
input:hover,select:hover,textarea:hover{border-color:#35456a}
select{appearance:none;-webkit-appearance:none;padding-right:32px;cursor:pointer;
background-image:url("data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M1 1l4 4 4-4' stroke='%2393a1b8' fill='none' stroke-width='1.6' stroke-linecap='round'/%3E%3C/svg%3E");
background-repeat:no-repeat;background-position:right 12px center}
button{background:linear-gradient(180deg,#2ea043,#238636);color:#fff;border:1px solid #2ea043;
border-radius:var(--radius-s);padding:9px 18px;font-size:14px;font-weight:600;cursor:pointer;
box-shadow:0 2px 10px rgba(46,160,67,.35);transition:transform .1s ease,box-shadow .15s ease,filter .15s ease}
button:hover{filter:brightness(1.1);box-shadow:0 4px 16px rgba(46,160,67,.45)}
button:active{transform:translateY(1px)}
.set-grid{display:grid;grid-template-columns:1fr 1fr;gap:4px 20px;margin:6px 0 8px}
label.set{display:block;margin:12px 0}
label.set>span.lbl{display:block;font-weight:600;font-size:14px}
label.set small{color:var(--muted);display:block;margin:2px 0 7px;font-family:var(--mono);font-size:12px}
label.set input,label.set select{width:100%}
/* ---- misc ---- */
pre{background:var(--inset);border:1px solid var(--border-soft);border-radius:var(--radius);
padding:14px 16px;overflow:auto;font-size:13px;line-height:1.6;white-space:pre-wrap;
font-family:var(--mono)}
pre.logs{max-height:62vh}
.bars{display:flex;align-items:flex-end;gap:8px;height:130px;margin:10px 0 30px;
padding:14px 16px 30px;background:var(--panel);border:1px solid var(--border-soft);
border-radius:var(--radius)}
.bar{flex:1;background:linear-gradient(180deg,#3fb950,#1f6f43);border-radius:5px 5px 2px 2px;
min-height:5px;position:relative;min-width:8px;transition:filter .15s ease}
.bar:hover{filter:brightness(1.25)}
.bar span{position:absolute;bottom:-24px;left:50%;transform:translateX(-50%);font-size:10.5px;
color:var(--faint);white-space:nowrap}
.muted{color:var(--muted);font-size:13px}
audio{width:100%;margin:10px 0 4px;accent-color:var(--accent);border-radius:var(--radius-s)}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:#2c3a52;border-radius:8px;border:2px solid var(--bg)}
::-webkit-scrollbar-track{background:transparent}
@media(max-width:860px){.set-grid{grid-template-columns:1fr}}
@media(max-width:640px){
  body{padding:0 12px 48px}
  nav{margin:0 -12px 20px;padding:10px 12px}
  .pagehead h1,h1{font-size:23px}
  .card .big{font-size:20px}
  input[type=text]{min-width:0;flex:1}
}
@media(prefers-reduced-motion:reduce){*{transition:none !important}}
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
                f"config ({len(restart)} key(s)). The bot restarts itself on "
                "`.env` changes — or apply now:"
                f"{self._restart_button(query)}</div>"
            )
        elif query.get("saved"):
            banner = (
                '<div class="banner ok">Settings saved. The bot restarts itself to '
                "apply them — or apply now:"
                f"{self._restart_button(query)}</div>"
            )
        if query.get("error"):
            banner += f'<div class="banner err">{esc(query["error"])}</div>'
        qs = self._token_qs(query)
        current = {
            "Status": "/",
            "Memo": "/history",
            "History": "/history",
            "Settings": "/settings",
            "Logs": "/logs",
        }.get(title, "/")
        links = []
        for label, href in (
            ("Status", "/"),
            ("History", "/history"),
            ("Settings", "/settings"),
            ("Logs", "/logs"),
        ):
            mark = ' aria-current="page"' if href == current else ""
            links.append(f'<a class="navlink" href="{href}?{qs}"{mark}>{label}</a>')
        navlinks = "".join(links)
        return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · voicebot</title><style>{CSS}</style></head><body>
<nav aria-label="Dashboard"><span class="brand"><span class="dot"></span>voicebot
<span class="ver">v{esc(__version__)}</span></span>{navlinks}<span class="spacer"></span>
<a class="navlink" href="/export?format=json&{qs}">Export JSON</a>
<a class="navlink" href="/export?format=csv&{qs}">Export CSV</a></nav>
<div class="wrap">{banner}{body}
<footer><span>voicebot v{esc(__version__)}</span><span>·</span>
<span>local-only dashboard — your audio never leaves this machine except to your AI provider</span></footer></div></body></html>"""

    def _restart_button(self, query: dict[str, str]) -> str:
        return (
            f"""<form method="post" action="/restart?{self._token_qs(query)}"
 style="display:inline;margin-left:8px">"""
            f"""<input type="hidden" name="token" value="{esc(query.get("token", self.token))}">"""
            """<button type="submit">Restart bot now</button></form>"""
        )

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
            f'<div class="bar" style="height:{100 * count // peak if peak else 4}%"'
            f' title="{esc(day)}: {count} memo(s)">'
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
                '<h2>Recent failures</h2><div class="table-wrap"><table>'
                "<tr><th>Memo</th><th>Received</th>"
                f"<th>Stage</th></tr>{fail_rows}</table></div>"
                '<p class="muted">Resume from Discord with '
                "<code>!retry &lt;memo-id&gt;</code>.</p>"
            )
        else:
            fail_html = "<h2>Recent failures</h2><p class='muted'>None. All clear.</p>"
        return f"""
<div class="pagehead"><h1>Status</h1>
<p class="lede">Live health of your voice journal.</p></div>
<div class="cards">
<div class="card card--bot"><h3>Bot</h3><div class="big">{hours}h {minutes}m</div>
<div class="muted">uptime · in flight: {stats["in_flight"]} · DB: {esc(stats["integrity"])}</div></div>
<div class="card card--providers"><h3>Providers</h3><div class="big">{esc(settings.stt_provider)} / {esc(settings.summary_provider)}</div>
<div class="muted">STT: {esc(settings.transcription_model)}<br>Summary: {esc(settings.summary_model)}</div></div>
<div class="card card--memos"><h3>Memos</h3><div class="big">{completed} ✓ · {failed} ✗</div>
<div class="muted">total: {stats["total"]} · last completed: {last_text}</div></div>
<div class="card card--obsidian"><h3>Obsidian</h3><div>{obsidian_line}</div></div>
<div class="card card--storage"><h3>Storage</h3><div class="big">{stats["audio_mb"]:.1f} MB</div>
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
<div class="pagehead"><h1>History</h1>
<p class="lede">Search across every transcript and summary.</p></div>
<form class="inline" method="get" action="/history">
<input type="hidden" name="token" value="{esc(query.get("token", self.token))}">
<input type="text" name="q" placeholder="Search transcripts & summaries…" value="{esc(term)}">
<select name="status" aria-label="Filter by status"><option value="">all statuses</option>
{"".join(f'<option value="{s}"{" selected" if status == s else ""}>{s}</option>' for s in ("completed", "failed", "received", "audio_saved", "transcribed", "summarized", "artifacts_written"))}
</select><button type="submit">Search</button></form>
<h2>{heading}</h2>
<div class="table-wrap"><table><tr><th>Memo</th><th>Received</th><th>User</th><th>File</th><th>Status</th><th>Teaser</th></tr>
{rows or '<tr><td colspan="6" class="muted">No memos found.</td></tr>'}</table></div>"""

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
            parts = [f'<section class="panel prose"><h3>Summary</h3><p>{esc(summary.summary)}</p>']
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
            parts.append("</section>")
            summary_html = "".join(parts)
        else:
            summary_html = "<p class='muted'>No summary yet.</p>"
        audio_html = ""
        if memo.audio_path:
            try:
                dash.storage.resolve(memo.audio_path)
                audio_html = (
                    '<section class="panel"><h3>Audio</h3><audio controls preload="none" '
                    f'src="/audio?id={esc(memo.memo_id)}&{qs}"></audio></section>'
                )
            except ValueError:
                audio_html = "<p class='muted'>Audio file is missing.</p>"
        sync = dash.database.outbox_status(memo.memo_id)
        chips = "".join(
            f'<span class="chip">{esc(text)}</span>'
            for text in (
                memo.received_at[:19],
                memo.username,
                Path(memo.original_filename).name,
                f"{memo.attempt_count} attempt(s)",
                f"obsidian: {sync or 'n/a'}",
            )
        )
        return f"""
<div class="pagehead"><p><span class="badge {esc(memo.status)}">{esc(memo.status)}</span></p>
<h1 class="memoid">{esc(memo.memo_id)}</h1></div>
<div class="meta">{chips}</div>
{audio_html}{summary_html}
<section class="panel"><h3>Transcript</h3>
<pre>{esc(memo.transcript or "No transcript yet.")}</pre></section>"""

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
                control = f'<input type="text" name="{key}" value="{esc(current)}">'
            rows += (
                f'<label class="set"><span class="lbl">{esc(label)}</span>'
                f"<small>{esc(key)}</small>{control}</label>"
            )
        secrets = "".join(
            f"<tr><td><code>{key}</code></td><td>"
            f"{'•••••• (set)' if getattr(dash.settings, key.lower()) else '(not set)'}</td></tr>"
            for key in SECRET_KEYS
        )
        return f"""
<div class="pagehead"><h1>Settings</h1>
<p class="lede">Tune the bot. Changes are validated, saved to <code>.env</code>,
and applied on restart.</p></div>
<p class="muted">Secrets are managed in <code>.env</code> and never shown here.</p>
<form method="post" action="/settings?{self._token_qs(query)}">
<input type="hidden" name="token" value="{esc(query.get("token", self.token))}">
<div class="set-grid">{rows}</div><button type="submit">Save settings</button></form>
<h2>Restart</h2><p class="muted">Apply the current <code>.env</code> now
(the bot also restarts itself when it changes).</p>
{self._restart_button(query)}
<h2>Secrets (masked)</h2>
<div class="table-wrap"><table><tr><th>Key</th><th>State</th></tr>{secrets}</table></div>"""

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
            '<div class="pagehead"><h1>Logs</h1>'
            '<p class="lede">Recent bot activity for quick triage.</p></div>'
            "<p class='muted'>Last "
            f"{len(lines)} lines of <code>{esc(path.name)}</code>.</p>"
            f"<pre class='logs'>{esc(''.join(lines))}</pre>"
        )

    # -- routing ---------------------------------------------------------
    def do_GET(self) -> None:
        query = self._query()
        path = urlsplit(self.path).path
        if path == "/favicon.ico":
            self._send(204, "")
            return
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
        if urlsplit(self.path).path == "/restart":
            self._send(
                200,
                "<h1>Restarting…</h1><p class='muted'>The bot is restarting to "
                "pick up the current <code>.env</code>. This page will be back "
                "in a few seconds.</p>",
            )
            timer = threading.Timer(RESTART_DELAY_SECONDS, _restart_from_dashboard)
            timer.daemon = True
            timer.start()
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
