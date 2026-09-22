from __future__ import annotations

import gc
from pathlib import Path

from voicebot.config import Settings


def make_settings(root: Path, **overrides: str) -> Settings:
    vault = root / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    env = {
        "DISCORD_TOKEN": "test-discord-token",
        "GROQ_API_KEY": "test-groq-key",
        "OPENAI_API_KEY": "test-openai-key",
        "STT_PROVIDER": "groq",
        "SUMMARY_PROVIDER": "groq",
        "ALLOWED_USER_IDS": "123,456",
        "DATA_ROOT": str(root),
        "VOICE_LOG_DIR": "voice_logs",
        "DATABASE_PATH": "transcripts.db",
        "LOG_DIR": "logs",
        "OBSIDIAN_ENABLED": "false",
        "OBSIDIAN_VAULT_PATH": str(vault),
        "OBSIDIAN_SUBFOLDER": "Voice Journal",
        "OBSIDIAN_REQUIRE_MOUNT": "false",
        "TIMEZONE": "UTC",
    }
    env.update(overrides)
    return Settings.load(env, project_root=root)


def dispose_database(database) -> None:
    """Release SQLite handles so Windows can delete TemporaryDirectory."""
    try:
        checkpoint = getattr(database, "checkpoint", None)
        if callable(checkpoint):
            checkpoint()
    except Exception:
        pass
    gc.collect()
