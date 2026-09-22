# Roadmap

Where this project is going. Items are ordered roughly by priority; nothing
here is promised on a date. PRs and discussion welcome — please open an issue
first for anything marked `needs-design`.

## Next: packaging & onboarding

- [ ] `Dockerfile` + `docker-compose.yml` (`needs-design`)
  - One-command run: `docker compose up --build` with env file support.
  - Non-root image user, persistent volumes for data/logs/vault, healthcheck
    against `voicebot health`, pinned base image + lockfile build.
- [ ] `docker-compose.obsidian.yml` overlay: optional vault volume profile so the
  base compose stays local-only.
- [ ] Quickstart polish: `uv run python -m voicebot doctor` preflight check
  (token validity without side effects, provider key shape, vault writability,
  Discord intent hint). Fails with actionable messages.

## Multi-user support (`needs-design`)

> Deliberately not started yet. Single-owner allowlist is the current model.

- [ ] Decide identity model: per-user allowlist roles (owner vs member) vs open
  guild with per-user data scoping.
- [ ] Per-user data isolation: every query already scopes by `discord_id`
  (`recent_for_user`, `search_for_user`); audit all new queries for the same.
- [ ] Per-user provider keys or shared bot keys + per-user rate limits and
  concurrency fairness (replace global semaphore with per-user buckets).
- [ ] Server/channel support: currently DM-only by design (`authorized_dm`).
  Needs spam/abuse controls before opening up.
- [ ] Privacy UX: per-user retention controls, export-my-data / delete-my-data
  commands, audit logging for admin actions.

## Providers & models

- [ ] More summarization providers (Anthropic, local Ollama/vLLM endpoint).
  `HybridGateway` in `voicebot/ai.py` is the seam — add a gateway class plus
  `Settings` provider wiring and fake-client tests.
- [ ] Local-only STT option (faster-whisper sidecar) for zero-cloud setups.
- [ ] Model auto-discovery / config validation at startup with a clear error
  when a model name is retired (Groq/OpenAI rename models often).
- [ ] Cost/latency observability: per-memo provider, model, tokens, seconds in
  `!status` and `voicebot export`.

## Sync & storage

- [ ] Additional sync targets behind the outbox pattern (local folder mirror,
  Nextcloud/WebDAV). Obsidian stays one destination among many.
- [ ] Postgres option for multi-instance deployments (today: one bot process
  per SQLite file).
- [ ] Encrypted-at-rest option for audio/transcripts.

## Quality of life

- [ ] Real-time progress: edit Discord status message with % / ETA, not just stage.
- [ ] Language per memo (`!lang ar` override) instead of only global hint.
- [ ] Speaker diarization flag when providers support it well.
- [ ] `!export` to DM a memo as Markdown/PDF.
- [ ] CHANGELOG.md (Keep a Changelog) + GitHub Releases automation.
- [ ] Badges + demo GIF in README once packaging lands.

## Non-goals (for now)

- Public hosted instance — this is self-host software; you bring your own
  Discord app and API keys.
- Replacing Obsidian — the vault is an optional mirror, local Markdown stays
  the source of truth.
