# Security and privacy

This application processes private voice recordings, transcripts, Discord identifiers,
and AI-generated summaries. Treat its database, data directory, Obsidian notes, logs,
environment files, and backups as sensitive personal data.

## Credential handling

- Never commit real Discord, Groq, or SMB credentials.
- If a credential appears in source, logs, chat, or a broad backup, revoke it; deleting the
  visible copy is not sufficient.
- Prefer an OS-managed environment file or secret store in production.
- The source scanner is a safety net, not a substitute for credential rotation.

## Data boundaries

Audio and transcript content is sent to Groq for transcription and summarization. Local
copies are stored in SQLite and Markdown, and may be copied into an SMB-mounted Obsidian
vault. Decide and document the retention and backup policies for all three locations.

The bot accepts only configured Discord users in direct messages. Commands never return
machine paths or another user's results. Filenames, transcripts, and model output are
treated as untrusted input and rendered without active Markdown links or HTML.

## Recommended deployment controls

- Dedicated unprivileged service account.
- Runtime directories mode `0700`; private files mode `0600`.
- Environment file owned by root and readable only by the service group.
- SMB mount options such as `nosuid,nodev,noexec` and encryption where supported.
- One bot process per database unless an external job coordinator is added.
- Review dry-run output before retention or message cleanup operations.

The systemd template enables `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`,
`ProtectHome`, a restrictive umask, and an empty capability set. Adjust only the explicit
read/write paths needed by your deployment.

## Reporting a vulnerability

Do not place credentials, recordings, or transcripts in a public issue. Send a minimal
reproduction privately to the repository owner and rotate any possibly affected credential.
