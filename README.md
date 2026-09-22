# Voice Memo Bot

A Discord bot that turns voice-message attachments into searchable transcripts,
structured summaries, and local Markdown notes — with an optional Obsidian sync.

Send an audio file in a DM and the bot replies with a memo ID, transcript-backed
summary, and local note files. Everything is resumable: each memo checkpoints
through `received → audio saved → transcribed → summarized → artifacts written →
completed`, so `!retry` picks up exactly where a failure left off.

## Features

- **Voice → text → summary**: speech-to-text plus a strict JSON-schema summary
  (summary, key points, decisions, action items, mentioned names).
- **Two AI providers**: Groq and OpenAI, mixable per task (`STT_PROVIDER`,
  `SUMMARY_PROVIDER`). Only the keys for enabled providers are required.
- **Resumable pipeline**: stable memo IDs (`discord-<message>-<attachment>`),
  idempotent registration, per-memo locks, bounded concurrency.
- **Local-first notes**: private audio + `transcript.md` / `summary.md` per memo,
  daily index pages, SQLite with WAL + full-text search.
- **Optional Obsidian sync** (off by default): durable outbox with duplicate-proof
  markers and background retry. No SMB mount required — any writable directory works.
- **Discord commands**: `!last`, `!search`, `!retry`, `!sync`, `!status`, `!help`.
- **Safety built in**: credential-redacted logs, dry-run-first retention and
  DM-cleanup tools, source secret scan in CI. See [SECURITY.md](SECURITY.md).

## Requirements

- Python 3.11+ and [`uv`](https://docs.astral.sh/uv/)
- A Discord application with a bot token and the **Message Content Intent** enabled
- An API key for each enabled provider:
  - Groq (`GROQ_API_KEY`) — transcription default `whisper-large-v3`,
    summary defaults `openai/gpt-oss-120b` → `openai/gpt-oss-20b`
  - OpenAI (`OPENAI_API_KEY`) — transcription default `whisper-1`,
    summary default `gpt-4o-mini`
- Your Discord user ID(s) for `ALLOWED_USER_IDS` (right-click yourself → Copy User ID
  with Developer Mode on)

> **Privacy note:** audio and transcripts are sent to your configured AI
> provider(s) for transcription and summarization. Local copies live in SQLite +
> Markdown (and your Obsidian vault only if you enable it). Decide your retention
> and backup policy for all three. See [SECURITY.md](SECURITY.md).

## Quickstart

```bash
uv sync
cp .env.example .env               # Windows: copy .env.example .env
```

Edit `.env`:

```ini
DISCORD_TOKEN=<paste>
GROQ_API_KEY=<paste>               # only if STT_PROVIDER or SUMMARY_PROVIDER=groq
ALLOWED_USER_IDS=123456789
```

Minimal local-only run (no Obsidian):

```ini
STT_PROVIDER=groq
SUMMARY_PROVIDER=groq
OBSIDIAN_ENABLED=false
TIMEZONE=UTC
```

Start the bot:

```bash
uv run python bot.py
```

DM the bot an audio file (`.ogg .mp3 .wav .m4a .webm .flac .mp4 .mpeg .mpga`,
default max 25 MB). It replies `⏳ Received…`, edits the message through each
stage, then `✅ Memo … saved` with a summary teaser.

Verify it works:

```bash
uv run voicebot health
```

## Configuration

All options live in `.env` (see [`.env.example`](.env.example)). Environment
variables always override `.env` values.

| Variable | Purpose | Default |
|---|---|---|
| `DISCORD_TOKEN` | Discord bot credential | required |
| `GROQ_API_KEY` | Groq credential (if Groq enabled) | required if used |
| `OPENAI_API_KEY` | OpenAI credential (if OpenAI enabled) | required if used |
| `STT_PROVIDER` | `groq` or `openai` transcription | `groq` |
| `SUMMARY_PROVIDER` | `groq` or `openai` summarization | `groq` |
| `ALLOWED_USER_IDS` | Comma-separated authorized Discord IDs | required |
| `DATA_ROOT` | Root for runtime data | project dir |
| `VOICE_LOG_DIR` | Audio + Markdown dir | `voice_logs` |
| `DATABASE_PATH` | SQLite database | `transcripts.db` |
| `LOG_DIR` | Log directory | `logs` |
| `SUMMARY_MODEL` | Primary summary model (explicit override) | provider default |
| `SUMMARY_MODEL_FALLBACK` | Summary fallback model | provider default |
| `OPENAI_SUMMARY_MODEL` | Default when `SUMMARY_PROVIDER=openai` | `gpt-4o-mini` |
| `TRANSCRIPTION_MODEL` | STT model (explicit override) | provider default |
| `TRANSCRIPTION_LANGUAGE` | ISO-639-1 hint (`en`, `ar`…), empty = auto | empty |
| `MAX_FILE_SIZE_MB` | Upload limit, 1–100 | `25` |
| `MAX_CONCURRENT_JOBS` | End-to-end processing limit, 1–10 | `2` |
| `GROQ_TIMEOUT_SECONDS` / `GROQ_MAX_RETRIES` | Groq client tuning | `90` / `2` |
| `OPENAI_TIMEOUT_SECONDS` / `OPENAI_MAX_RETRIES` | OpenAI client tuning | `90` / `2` |
| `TIMEZONE` | IANA timezone for notes | `UTC` |
| `OBSIDIAN_ENABLED` | Turn on vault sync | `false` |
| `OBSIDIAN_VAULT_PATH` | Vault directory (any writable path) | empty |
| `OBSIDIAN_SUBFOLDER` | Subfolder inside vault | `Voice Journal` |
| `OBSIDIAN_REQUIRE_MOUNT` | Require path to be a mount point | `false` |
| `OBSIDIAN_QUEUE_CHECK_SECONDS` | Background retry interval, 30–86400 | `300` |
| `RETENTION_DAYS` | Suggested cleanup age; `0` disables | `0` |
| `LOG_LEVEL` | Log verbosity | `INFO` |

Provider/model notes:

- `STT_PROVIDER` and `SUMMARY_PROVIDER` are independent — e.g. Groq STT +
  OpenAI summaries works. `TRANSCRIPTION_MODEL` always wins over the default
  when set; otherwise the default follows `STT_PROVIDER`.
- Model names go stale fast. If your provider renames models, set
  `SUMMARY_MODEL` / `TRANSCRIPTION_MODEL` explicitly and restart.

## Discord commands

All commands work in DMs from authorized users only.

- `!last [1-20]` — recent memo IDs, status, summary teasers.
- `!search <words>` — owner-scoped full-text search over transcripts + summaries.
- `!retry <memo-id>` — resume from the last durable stage.
- `!sync` — retry the Obsidian outbox (says "disabled" when off).
- `!status` — uptime, in-flight work, failures, queue depth, vault state.
- `!help` — usage and accepted formats.

## How it works

```text
Discord DM attachment
  -> SQLite registration (idempotent memo_id)
  -> private audio file (atomic write, size-checked)
  -> STT provider transcription
  -> summary provider structured JSON
  -> transcript.md / summary.md / daily index
  -> COMPLETED
  -> Obsidian outbox (only if OBSIDIAN_ENABLED=true)
```

Obsidian availability never decides whether the local memo succeeds. When
enabled and the vault is down, entries stay queued and retry in the background
(`!sync` forces a retry). Daily vault notes are append-only with
`<!-- voice-memo:<id> -->` markers, so redelivery never duplicates.

## Enabling Obsidian (optional)

```ini
OBSIDIAN_ENABLED=true
OBSIDIAN_VAULT_PATH=/path/to/your/vault
OBSIDIAN_SUBFOLDER=Voice Journal
```

Any writable directory works — local folder, synced folder, or a mount. Only
set `OBSIDIAN_REQUIRE_MOUNT=true` if you specifically want to refuse syncing
when the path is not a mount point. Restart the bot after changing these.

## Maintenance

```bash
uv run voicebot health
uv run voicebot reindex
uv run voicebot export --output exports/memos.json
uv run voicebot prune --days 90             # dry-run
uv run voicebot prune --days 90 --execute   # requires confirmation
uv run python scripts/scan_secrets.py
uv run python -m unittest discover -s tests -v
```

Pruning protects failed and not-yet-synced memos and never deletes Obsidian
copies. Review every dry run before deleting. `uv run` verifies `.venv`
matches `uv.lock`; to upgrade deps deliberately, edit `pyproject.toml`, run
`uv lock`, run the suite, commit the lockfile.

DM cleanup (bot's own messages only, dry-run first):

```bash
uv run python delete_messages.py --target-user 123 --limit 100
uv run python delete_messages.py --target-user 123 --limit 100 --execute
```

`DISCORD_CLEANUP_TOKEN` (or `DISCORD_TOKEN`) must be in the environment.
It never prints message contents.

## Deployment

Sample systemd unit: [`deploy/voicebot.service`](deploy/voicebot.service).
It assumes code at `/opt/voice-to-text`, a locked env from `uv sync --locked`,
secrets at `/etc/voicebot/voicebot.env` (mode `0640`), runtime data at
`/var/lib/voicebot` (mode `0700`), and a dedicated unprivileged account.
Only add a vault `ReadWritePaths=` entry if you enable Obsidian.
See [SECURITY.md](SECURITY.md) for the threat model and
[ROADMAP.md](ROADMAP.md) for Docker/multi-user plans.

## Project map

| Path | What lives there |
|---|---|
| `bot.py` | Entrypoint: loads config, wires gateway + service + Discord bot |
| `voicebot/config.py` | `Settings.load()` — validation, provider routing, secret checks |
| `voicebot/ai.py` | `GroqGateway`, `OpenAIGateway`, `HybridGateway`, `build_gateway()` |
| `voicebot/service.py` | Memo pipeline, checkpoints, retries, concurrency |
| `voicebot/database.py` | SQLite (WAL, FTS, migrations, outbox) |
| `voicebot/storage.py` | Atomic file writes, daily indexes |
| `voicebot/obsidian.py` | Optional vault sync (disabled by default) |
| `voicebot/discord_app.py` | Discord event handlers + commands |
| `voicebot/cli.py` | `voicebot health/reindex/export/prune` |
| `delete_messages.py` | DM cleanup utility |
| `scripts/scan_secrets.py` | Tracked-source secret scanner |
| `tests/` | Offline suite (fake AI clients, temp dirs, no network) |

## Contributing, security, license

- [CONTRIBUTING.md](CONTRIBUTING.md) — setup, checks, PR checklist
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) — participation standards
- [SECURITY.md](SECURITY.md) — credential handling, data boundaries, private reporting
- [ROADMAP.md](ROADMAP.md) — Docker, multi-user, and other planned work
- [LICENSE](LICENSE) — MIT
