from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Settings:
    project_root: Path
    data_root: Path
    voice_log_dir: Path
    database_path: Path
    log_dir: Path
    discord_token: str
    groq_api_key: str
    allowed_users: frozenset[int]
    summary_model: str
    summary_fallback_model: str
    transcription_model: str
    transcription_language: str | None
    max_file_size_bytes: int
    max_concurrent_jobs: int
    groq_timeout_seconds: float
    groq_max_retries: int
    obsidian_vault_path: Path
    obsidian_subfolder: str
    obsidian_queue_check_seconds: int
    obsidian_require_mount: bool
    retention_days: int
    timezone: ZoneInfo
    log_level: str

    @classmethod
    def load(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        project_root: Path | None = None,
        require_secrets: bool = True,
    ) -> "Settings":
        env = os.environ if environ is None else environ
        root = (project_root or PROJECT_ROOT).resolve()
        data_root = _path(env.get("DATA_ROOT", str(root)), root)
        voice_log_dir = _path(env.get("VOICE_LOG_DIR", "voice_logs"), data_root)
        database_path = _path(env.get("DATABASE_PATH", "transcripts.db"), data_root)
        log_dir = _path(env.get("LOG_DIR", "logs"), data_root)

        discord_token = env.get("DISCORD_TOKEN", "").strip()
        groq_api_key = env.get("GROQ_API_KEY", "").strip()
        if require_secrets and not discord_token:
            raise ConfigurationError("DISCORD_TOKEN is required")
        if require_secrets and not groq_api_key:
            raise ConfigurationError("GROQ_API_KEY is required")

        allowed_raw = env.get("ALLOWED_USER_IDS", env.get("ALLOWED_USERS", ""))
        allowed_users = _parse_user_ids(allowed_raw)
        if require_secrets and not allowed_users:
            raise ConfigurationError("ALLOWED_USER_IDS must contain at least one Discord user ID")

        timezone_name = env.get("TIMEZONE", "Africa/Cairo")
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ConfigurationError(f"TIMEZONE is not available: {timezone_name}") from exc

        max_size_mb = _integer(env, "MAX_FILE_SIZE_MB", 25, minimum=1, maximum=100)
        obsidian_subfolder = _relative_subfolder(
            env.get("OBSIDIAN_SUBFOLDER", "Voice Journal")
        )
        return cls(
            project_root=root,
            data_root=data_root,
            voice_log_dir=voice_log_dir,
            database_path=database_path,
            log_dir=log_dir,
            discord_token=discord_token,
            groq_api_key=groq_api_key,
            allowed_users=allowed_users,
            summary_model=env.get("SUMMARY_MODEL", "openai/gpt-oss-120b").strip(),
            summary_fallback_model=env.get("SUMMARY_MODEL_FALLBACK", "openai/gpt-oss-20b").strip(),
            transcription_model=env.get("TRANSCRIPTION_MODEL", "whisper-large-v3").strip(),
            transcription_language=env.get("TRANSCRIPTION_LANGUAGE", "").strip() or None,
            max_file_size_bytes=max_size_mb * 1024 * 1024,
            max_concurrent_jobs=_integer(env, "MAX_CONCURRENT_JOBS", 2, minimum=1, maximum=10),
            groq_timeout_seconds=_number(env, "GROQ_TIMEOUT_SECONDS", 90.0, minimum=5.0, maximum=600.0),
            groq_max_retries=_integer(env, "GROQ_MAX_RETRIES", 2, minimum=0, maximum=8),
            obsidian_vault_path=_path(
                env.get("OBSIDIAN_VAULT_PATH", "/mnt/obsidian-vault"), root
            ),
            obsidian_subfolder=obsidian_subfolder,
            obsidian_queue_check_seconds=_integer(
                env, "OBSIDIAN_QUEUE_CHECK_SECONDS", 300, minimum=30, maximum=86_400
            ),
            obsidian_require_mount=_boolean(env.get("OBSIDIAN_REQUIRE_MOUNT", "true")),
            retention_days=_integer(env, "RETENTION_DAYS", 0, minimum=0, maximum=36_500),
            timezone=timezone,
            log_level=env.get("LOG_LEVEL", "INFO").upper(),
        )

    @property
    def obsidian_dir(self) -> Path:
        return self.obsidian_vault_path / self.obsidian_subfolder

    def redacted_summary(self) -> dict[str, object]:
        return {
            "allowed_user_count": len(self.allowed_users),
            "summary_model": self.summary_model,
            "summary_fallback_model": self.summary_fallback_model,
            "transcription_model": self.transcription_model,
            "max_file_size_mb": self.max_file_size_bytes // (1024 * 1024),
            "max_concurrent_jobs": self.max_concurrent_jobs,
            "retention_days": self.retention_days,
            "timezone": str(self.timezone),
        }


def _path(value: str, root: Path) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else root / path).resolve()


def _parse_user_ids(value: str) -> frozenset[int]:
    result: set[int] = set()
    for raw in value.replace(";", ",").split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            user_id = int(raw)
        except ValueError as exc:
            raise ConfigurationError(f"invalid Discord user ID: {raw!r}") from exc
        if user_id <= 0:
            raise ConfigurationError("Discord user IDs must be positive")
        result.add(user_id)
    return frozenset(result)


def _integer(
    env: Mapping[str, str], name: str, default: int, *, minimum: int, maximum: int
) -> int:
    raw = env.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _number(
    env: Mapping[str, str], name: str, default: float, *, minimum: float, maximum: float
) -> float:
    raw = env.get(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum:g} and {maximum:g}")
    return value


def _boolean(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"invalid boolean value: {value!r}")


def _relative_subfolder(value: str) -> str:
    cleaned = value.strip()
    path = Path(cleaned)
    if not cleaned or path.is_absolute() or ".." in path.parts:
        raise ConfigurationError("OBSIDIAN_SUBFOLDER must be a safe relative path")
    return path.as_posix()
