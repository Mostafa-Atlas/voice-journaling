# Private Voice Memo Bot

A private Discord bot that turns audio attachments into searchable transcripts,
structured summaries, local Markdown notes, and idempotent Obsidian journal entries.

## What changed in version 2

- Resumable processing checkpoints: received, audio saved, transcribed, summarized,
  artifacts written, and completed.
- Stable memo IDs derived from Discord message and attachment IDs, preventing duplicate
  API calls and same-second filename collisions.
- Atomic private file writes and paths rooted at a configured data directory.
- SQLite WAL mode, busy timeouts, integrity checks, full-text search, and additive
  migration of the original `transcripts` table.
- Strict JSON-schema summaries with deterministic, Markdown-safe rendering.
- A durable Obsidian outbox with duplicate-proof memo markers and retry backoff.
- Immediate Discord acknowledgement plus `!last`, `!search`, `!retry`, `!sync`,
  `!status`, and `!help` commands.
- Rotating, credential-redacted logs and bounded processing concurrency.
- Dry-run-first retention and DM-cleanup tools.

## Processing flow

```text
Discord DM attachment
  -> durable SQLite registration
  -> private audio file
  -> Groq Whisper transcription
  -> structured Groq summary
  -> transcript/summary/daily Markdown
  -> local completion
  -> durable Obsidian outbox -> SMB-mounted vault
```

Obsidian availability never determines whether the local memo succeeds. If the mount is
down, the entry stays queued and is retried in the background.

## Security first

The original cleanup script contained a Discord token. **Revoke that Discord token and
issue a new one before running or pushing this project.** Rotate the Groq API key as a
precaution too. Removing a token from a file does not revoke copies in backups.

Never commit `.env`, `transcripts.db`, `voice_logs/`, logs, exports, or a virtual
environment. They are ignored by Git, and CI also runs a source secret scan.

## Setup

The project uses [`uv`](https://docs.astral.sh/uv/) for Python installation,
dependency locking, and execution. The lockfile targets Python 3.11 or newer.

```bash
uv sync --locked
cp .env.example .env               # Windows: copy .env.example .env
```

Fill in `.env`. In the Discord Developer Portal, enable the privileged **Message Content
Intent** for the bot. Invite it only where appropriate; application commands and memo
processing are intentionally restricted to authorized direct messages.

Run it:

```bash
uv run python bot.py
```

`uv run` verifies that `.venv` matches `uv.lock` before starting the bot. To update
dependencies deliberately, edit `pyproject.toml`, run `uv lock --upgrade`, run the test
suite, and commit the resulting lockfile.

## Configuration

| Variable | Purpose | Default |
|---|---|---|
| `DISCORD_TOKEN` | Discord bot credential | required |
| `GROQ_API_KEY` | Groq credential | required |
| `ALLOWED_USER_IDS` | Comma-separated authorized Discord IDs | required |
| `DATA_ROOT` | Root for private runtime data | project directory |
| `VOICE_LOG_DIR` | Audio and Markdown directory | `voice_logs` |
| `DATABASE_PATH` | SQLite database | `transcripts.db` |
| `SUMMARY_MODEL` | Primary structured-summary model | `openai/gpt-oss-120b` |
| `SUMMARY_MODEL_FALLBACK` | Summary fallback | `openai/gpt-oss-20b` |
| `TRANSCRIPTION_MODEL` | Speech-to-text model | `whisper-large-v3` |
| `MAX_FILE_SIZE_MB` | Upload limit, from 1 to 100 | `25` |
| `MAX_CONCURRENT_JOBS` | End-to-end processing limit | `2` |
| `OBSIDIAN_VAULT_PATH` | Existing SMB mount point | `/mnt/obsidian-vault` |
| `OBSIDIAN_QUEUE_CHECK_SECONDS` | Background retry interval | `300` |
| `TIMEZONE` | IANA timezone used for notes | `Africa/Cairo` |
| `RETENTION_DAYS` | Suggested cleanup age; zero disables | `0` |

The checked-in `.env.example` documents every option.

## Discord commands

- `!last [1-20]` — show recent memo IDs, status, and real summary teasers.
- `!search <words>` — owner-scoped full-text search.
- `!retry <memo-id>` — resume from the last durable stage.
- `!sync` — manually retry pending Obsidian entries.
- `!status` — show uptime, in-flight work, failures, queue depth, and vault state.
- `!help` — show usage and accepted formats.

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

Pruning protects failed and not-yet-synced memos and never removes Obsidian copies.
Legacy rows are also protected automatically. Review every dry run before deletion.

The DM cleanup utility is separately protected:

```bash
uv run python delete_messages.py --target-user 123 --limit 100              # dry-run
uv run python delete_messages.py --target-user 123 --limit 100 --execute    # confirmation
```

It deletes only messages authored by the bot and never prints message contents.

## Existing database migration

Startup preserves the original `transcripts` and `obsidian_queue` tables. Existing
transcript rows are copied once into the new `memos` table with `legacy-*` IDs, while the
old tables remain available for rollback. Back up the private database before a production
upgrade.

## Tests and pre-push checks

The test suite uses temporary directories, fake AI clients, and no network calls. CI checks
syntax, tests, and tracked-source secrets. Before the first push, also inspect:

```bash
git status --ignored
git diff --cached --name-only
git grep -n -I -E 'mfa\.|gsk_|DISCORD_TOKEN\s*=.+|GROQ_API_KEY\s*=.+'
```

Only placeholder values in `.env.example` should appear.

## Production deployment

The sample systemd unit in `deploy/voicebot.service` assumes:

- application code at `/opt/voice-to-text`;
- a locked environment created with `uv sync --locked` at `/opt/voice-to-text/.venv`;
- secrets at `/etc/voicebot/voicebot.env` with mode `0640`;
- runtime data at `/var/lib/voicebot` with mode `0700`;
- a separately managed SMB mount at `/mnt/obsidian-vault`.

Use a dedicated unprivileged account. Keep SMB credentials in a root-owned credentials file,
not in this repository. See [SECURITY.md](SECURITY.md) for the data and threat model.
