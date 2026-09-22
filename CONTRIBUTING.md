# Contributing

Thanks for helping out. This project keeps a small, reviewable surface on purpose:
private voice data flows through it, so correctness and secrecy matter more than speed.

## Ground rules

- Never commit real credentials, voice audio, transcripts, databases (`*.db`),
  `voice_logs/`, `logs/`, `exports/`, or `.env` files. Use placeholders only.
- If a secret ever touches a commit, log, screenshot, or backup: **rotate it
  immediately**. Deleting the visible copy is not enough.
- Keep PRs small and focused. One behavior change per PR.
- Add or update tests for behavior changes. No network calls in tests —
  use the fakes in `tests/` (see `test_ai.py`, `test_service.py`).

## Local setup (uv)

```bash
uv sync
cp .env.example .env   # Windows: copy .env.example .env
```

Fill in `.env` (see README for every option). Then:

```bash
uv run python scripts/scan_secrets.py
uv run python -m unittest discover -s tests -v
uvx ruff@0.14.13 check --select F,I,UP,B bot.py delete_messages.py voicebot tests scripts
```

## Running the bot

```bash
uv run python bot.py
```

Maintenance CLI:

```bash
uv run voicebot health
uv run voicebot reindex
uv run voicebot export --output exports/memos.json
uv run voicebot prune --days 90             # dry-run
uv run voicebot prune --days 90 --execute   # requires confirmation
```

## Before you push

```bash
uv run python scripts/scan_secrets.py
git status --ignored
git diff --cached --name-only
git grep -n -I -E 'mfa\.|gsk_|sk-proj-|sk-|DISCORD_TOKEN\s*=.+|GROQ_API_KEY\s*=.+|OPENAI_API_KEY\s*=.+'
```

Only placeholder values in `.env.example` should appear. The repo has no
`LICENSE`-incompatible dependencies; keep it that way (MIT-compatible only).

## PR checklist

- [ ] Tests added/updated, full suite green
- [ ] Secret scan green, no new tracked private files
- [ ] `.env.example` / README updated if config changed
- [ ] No provider API keys or Discord tokens in code, logs, or fixtures
