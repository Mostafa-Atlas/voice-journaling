from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SUPPORTED_PROVIDERS = ("groq", "openai")

# Obvious non-secret placeholders that must never be accepted as real secrets.
_PLACEHOLDER_MARKERS = (
    "<",
    ">",
    "replace-me",
    "your-",
    "your_",
    "change-me",
    "changeme",
    "xxx",
    "example",
)


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
    openai_api_key: str
    stt_provider: str
    summary_provider: str
    allowed_users: frozenset[int]
    summary_model: str
    summary_fallback_model: str
    transcription_model: str
    transcription_language: str | None
    max_file_size_bytes: int
    max_concurrent_jobs: int
    groq_timeout_seconds: float
    groq_max_retries: int
    openai_timeout_seconds: float
    openai_max_retries: int
    obsidian_enabled: bool
    obsidian_vault_path: Path | None
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
    ) -> Settings:
        env = os.environ if environ is None else environ
        # When `environ` is an explicit mapping we only look at that mapping,
        # so tests never accidentally pick up real process secrets.
        get = env.get if isinstance(env, Mapping) else os.environ.get
        root = (project_root or PROJECT_ROOT).resolve()
        data_root = _path(_raw(get, "DATA_ROOT", str(root)), root)
        voice_log_dir = _path(_raw(get, "VOICE_LOG_DIR", "voice_logs"), data_root)
        database_path = _path(_raw(get, "DATABASE_PATH", "transcripts.db"), data_root)
        log_dir = _path(_raw(get, "LOG_DIR", "logs"), data_root)

        stt_provider = _provider(get, "STT_PROVIDER", "groq")
        summary_provider = _provider(get, "SUMMARY_PROVIDER", "groq")

        discord_token = _secret(get, "DISCORD_TOKEN", required=require_secrets)
        groq_api_key = _secret(
            get,
            "GROQ_API_KEY",
            required=require_secrets and ("groq" in (stt_provider, summary_provider)),
        )
        openai_api_key = _secret(
            get,
            "OPENAI_API_KEY",
            required=require_secrets and ("openai" in (stt_provider, summary_provider)),
        )

        allowed_raw = _raw(get, "ALLOWED_USER_IDS", _raw(get, "ALLOWED_USERS", ""))
        allowed_users = _parse_user_ids(allowed_raw)
        if require_secrets and not allowed_users:
            raise ConfigurationError("ALLOWED_USER_IDS must contain at least one Discord user ID")

        timezone_name = _raw(get, "TIMEZONE", "UTC")
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ConfigurationError(f"TIMEZONE is not available: {timezone_name}") from exc

        max_size_mb = _integer(env, "MAX_FILE_SIZE_MB", 25, minimum=1, maximum=100)
        obsidian_enabled = _boolean(_raw(get, "OBSIDIAN_ENABLED", "false"))
        vault_raw = _raw(get, "OBSIDIAN_VAULT_PATH", "").strip()
        obsidian_vault_path: Path | None = None
        if vault_raw:
            obsidian_vault_path = _path(vault_raw, root)
        elif obsidian_enabled:
            raise ConfigurationError("OBSIDIAN_ENABLED=true requires OBSIDIAN_VAULT_PATH")
        obsidian_subfolder = _relative_subfolder(_raw(get, "OBSIDIAN_SUBFOLDER", "Voice Journal"))
        return cls(
            project_root=root,
            data_root=data_root,
            voice_log_dir=voice_log_dir,
            database_path=database_path,
            log_dir=log_dir,
            discord_token=discord_token,
            groq_api_key=groq_api_key,
            openai_api_key=openai_api_key,
            stt_provider=stt_provider,
            summary_provider=summary_provider,
            allowed_users=allowed_users,
            summary_model=_summary_model(get, summary_provider),
            summary_fallback_model=_summary_fallback_model(get, summary_provider),
            transcription_model=_transcription_model(get, stt_provider),
            transcription_language=_raw(get, "TRANSCRIPTION_LANGUAGE", "").strip() or None,
            max_file_size_bytes=max_size_mb * 1024 * 1024,
            max_concurrent_jobs=_integer(env, "MAX_CONCURRENT_JOBS", 2, minimum=1, maximum=10),
            groq_timeout_seconds=_number(
                env, "GROQ_TIMEOUT_SECONDS", 90.0, minimum=5.0, maximum=600.0
            ),
            groq_max_retries=_integer(env, "GROQ_MAX_RETRIES", 2, minimum=0, maximum=8),
            openai_timeout_seconds=_number(
                env, "OPENAI_TIMEOUT_SECONDS", 90.0, minimum=5.0, maximum=600.0
            ),
            openai_max_retries=_integer(env, "OPENAI_MAX_RETRIES", 2, minimum=0, maximum=8),
            obsidian_enabled=obsidian_enabled,
            obsidian_vault_path=obsidian_vault_path,
            obsidian_subfolder=obsidian_subfolder,
            obsidian_queue_check_seconds=_integer(
                env, "OBSIDIAN_QUEUE_CHECK_SECONDS", 300, minimum=30, maximum=86_400
            ),
            obsidian_require_mount=_boolean(_raw(get, "OBSIDIAN_REQUIRE_MOUNT", "false")),
            retention_days=_integer(env, "RETENTION_DAYS", 0, minimum=0, maximum=36_500),
            timezone=timezone,
            log_level=_raw(get, "LOG_LEVEL", "INFO").upper(),
        )

    @property
    def obsidian_dir(self) -> Path | None:
        if not self.obsidian_enabled or self.obsidian_vault_path is None:
            return None
        return self.obsidian_vault_path / self.obsidian_subfolder

    def redacted_summary(self) -> dict[str, object]:
        # Never include secret values or lengths here; logs are often shared.
        return {
            "allowed_user_count": len(self.allowed_users),
            "stt_provider": self.stt_provider,
            "summary_provider": self.summary_provider,
            "summary_model": self.summary_model,
            "summary_fallback_model": self.summary_fallback_model,
            "transcription_model": self.transcription_model,
            "max_file_size_mb": self.max_file_size_bytes // (1024 * 1024),
            "max_concurrent_jobs": self.max_concurrent_jobs,
            "obsidian_enabled": self.obsidian_enabled,
            "retention_days": self.retention_days,
            "timezone": str(self.timezone),
        }


def _raw(getter: object, name: str, default: str) -> str:  # type: ignore[type-arg]
    try:
        value = getter(name, default)  # type: ignore[operator]
    except Exception:
        return default
    if value is None:
        return default
    return str(value)


def _path(value: str, root: Path) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else root / path).resolve()


def _provider(getter: object, name: str, default: str) -> str:  # type: ignore[type-arg]
    value = _raw(getter, name, default).strip().lower()
    if value not in SUPPORTED_PROVIDERS:
        raise ConfigurationError(f"{name} must be one of: {', '.join(SUPPORTED_PROVIDERS)}")
    return value


def _secret(getter: object, name: str, *, required: bool) -> str:  # type: ignore[type-arg]
    value = _raw(getter, name, "").strip().strip("'\"")
    if not value:
        if required:
            raise ConfigurationError(f"{name} is required")
        return ""
    lowered = value.lower()
    if any(marker in lowered for marker in _PLACEHOLDER_MARKERS):
        raise ConfigurationError(f"{name} looks like a placeholder; set a real value")
    if any(char.isspace() for char in value):
        raise ConfigurationError(f"{name} must not contain whitespace")
    return value


def _summary_model(getter: object, provider: str) -> str:  # type: ignore[type-arg]
    # Provider-aware defaults: only fall back to the provider default when the
    # user did not explicitly set SUMMARY_MODEL. This keeps existing Groq
    # deployments stable while giving OpenAI users a working default.
    explicit = _raw(getter, "SUMMARY_MODEL", "").strip()
    if explicit:
        return explicit
    if provider == "openai":
        return _raw(getter, "OPENAI_SUMMARY_MODEL", "gpt-4o-mini")
    return "openai/gpt-oss-120b"


def _summary_fallback_model(getter: object, provider: str) -> str:  # type: ignore[type-arg]
    explicit = _raw(getter, "SUMMARY_MODEL_FALLBACK", "").strip()
    if explicit:
        return explicit
    if provider == "openai":
        return _raw(getter, "OPENAI_SUMMARY_FALLBACK_MODEL", "gpt-4o-mini")
    return "openai/gpt-oss-20b"


def _transcription_model(getter: object, provider: str) -> str:  # type: ignore[type-arg]
    explicit = _raw(getter, "TRANSCRIPTION_MODEL", "").strip()
    if explicit:
        return explicit
    if provider == "openai":
        return "whisper-1"
    return "whisper-large-v3"


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


def _integer(env: Mapping[str, str], name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = env.get(name, str(default)) if isinstance(env, Mapping) else str(default)
    try:
        value = int(str(raw))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _number(
    env: Mapping[str, str], name: str, default: float, *, minimum: float, maximum: float
) -> float:
    raw = env.get(name, str(default)) if isinstance(env, Mapping) else str(default)
    try:
        value = float(str(raw))
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
