"""Interactive first-run setup wizard for the voice memo bot.

Walks an external user through the only three things that truly need them
(Discord token, AI provider key, owner user ID), validates each answer live,
and writes a working ``.env``. Everything else keeps safe defaults.

Run it::

    uv run voicebot setup

Non-interactive use (scripts, tests)::

    uv run voicebot setup --non-interactive --discord-token ... --provider groq \\
        --provider-key ... --owner-id 123
"""

from __future__ import annotations

import getpass
import json
import logging
import urllib.request
from collections.abc import Callable
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import SUPPORTED_PROVIDERS, ConfigurationError
from .dashboard import read_env_file, write_env_updates

log = logging.getLogger("voicebot.setup")

# View Channel + Send Messages + Read Message History + Attach Files.
INVITE_PERMISSIONS = 101376

Probe = Callable[..., tuple[bool, str]]


def build_invite_url(client_id: str) -> str:
    client_id = client_id.strip()
    if not client_id.isdigit():
        raise ConfigurationError("client ID must be the numeric application ID")
    return (
        "https://discord.com/oauth2/authorize"
        f"?client_id={client_id}&permissions={INVITE_PERMISSIONS}&scope=bot"
    )


def validate_token_shape(token: str, name: str) -> str:
    cleaned = token.strip().strip("'\"")
    if not cleaned:
        raise ConfigurationError(f"{name} must not be empty")
    lowered = cleaned.lower()
    for marker in ("<", ">", "replace-me", "your-", "change-me", "xxx", "example"):
        if marker in lowered:
            raise ConfigurationError(f"{name} looks like a placeholder; paste the real value")
    if any(char.isspace() for char in cleaned):
        raise ConfigurationError(f"{name} must not contain whitespace")
    if len(cleaned) < 8:
        raise ConfigurationError(f"{name} looks too short; paste the full value")
    return cleaned


def validate_user_id(raw: str) -> str:
    cleaned = raw.strip()
    if not cleaned.isdigit() or int(cleaned) <= 0:
        raise ConfigurationError("owner ID must be your numeric Discord user ID")
    return cleaned


def validate_timezone_name(raw: str) -> str:
    cleaned = raw.strip() or "UTC"
    try:
        ZoneInfo(cleaned)
    except ZoneInfoNotFoundError as exc:
        raise ConfigurationError(f"unknown timezone: {cleaned}") from exc
    return cleaned


def _https_get(
    url: str, token: str, timeout: float = 10.0, scheme: str = "Bearer"
) -> tuple[int, str]:
    request = urllib.request.Request(url, headers={"Authorization": f"{scheme} {token}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(2048).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except OSError as exc:
        raise ConfigurationError(f"network check failed: {type(exc).__name__}") from exc


def check_discord_token(token: str) -> tuple[bool, str]:
    """Validate a bot token against Discord. Returns (ok, detail)."""
    # Discord bot tokens use the "Bot" scheme; "Bearer" is for OAuth2 tokens.
    try:
        status, body = _https_get("https://discord.com/api/v10/users/@me", token, scheme="Bot")
    except ConfigurationError as exc:
        return False, str(exc)
    if status == 200:
        try:
            name = json.loads(body).get("username", "bot")
        except ValueError:
            name = "bot"
        return True, f"valid (bot user: {name})"
    if status in (401, 403):
        return False, "Discord rejected the token (401/403); check for typos"
    return False, f"Discord returned HTTP {status}; try again later"


def check_provider_key(provider: str, key: str) -> tuple[bool, str]:
    """Validate a provider key by listing models. Returns (ok, detail)."""
    urls = {
        "groq": "https://api.groq.com/openai/v1/models",
        "openai": "https://api.openai.com/v1/models",
    }
    try:
        status, _ = _https_get(urls[provider], key)
    except ConfigurationError as exc:
        return False, str(exc)
    except KeyError:
        return False, f"unknown provider: {provider}"
    if status == 200:
        return True, "valid"
    if status in (401, 403):
        return False, "provider rejected the key (401/403); check for typos"
    return False, f"provider returned HTTP {status}; try again later"


DEFAULT_PROBES: dict[str, Probe] = {
    "discord": check_discord_token,
    "provider": check_provider_key,
}


def _ask(
    prompt: str,
    default: str = "",
    *,
    secret: bool = False,
    non_interactive: bool = False,
    input_fn: Callable[[str], str] = input,
    getpass_fn: Callable[[str], str] = getpass.getpass,
) -> str:
    if non_interactive:
        if not default:
            raise ConfigurationError(f"missing required value for: {prompt}")
        return default
    shown_default = " (kept hidden)" if secret and default else (f" [{default}]" if default else "")
    answer = (getpass_fn if secret else input_fn)(f"{prompt}{shown_default}: ").strip()
    return answer or default


def run_setup(
    project_root: Path,
    *,
    discord_token: str | None = None,
    client_id: str = "",
    provider: str | None = None,
    provider_key: str | None = None,
    owner_id: str | None = None,
    timezone_name: str | None = None,
    non_interactive: bool = False,
    skip_validation: bool = False,
    probes: dict[str, Probe] | None = None,
    input_fn: Callable[[str], str] = input,
    getpass_fn: Callable[[str], str] = getpass.getpass,
) -> int:
    active_probes = probes or DEFAULT_PROBES
    _, existing = read_env_file(project_root / ".env")

    def ask(prompt: str, default: str = "", *, secret: bool = False) -> str:
        return _ask(
            prompt,
            default,
            secret=secret,
            non_interactive=non_interactive,
            input_fn=input_fn,
            getpass_fn=getpass_fn,
        )

    print("Voicebot setup — three things, then you're done.\n")

    # 1. Discord token.
    print("1) Discord bot token  (Developer Portal > Bot > Reset Token)")
    raw_token = (
        discord_token
        if discord_token is not None
        else ask("   Bot token", existing.get("DISCORD_TOKEN", ""), secret=True)
    )
    token = validate_token_shape(raw_token, "Discord token")
    if not skip_validation:
        ok, detail = active_probes["discord"](token)
        print(f"   Discord token: {detail}")
        if not ok:
            if non_interactive:
                raise ConfigurationError(f"Discord token invalid: {detail}")
            raw_token = ask("   Bot token (try again)", "", secret=True)
            token = validate_token_shape(raw_token, "Discord token")
            ok, detail = active_probes["discord"](token)
            print(f"   Discord token: {detail}")
            if not ok:
                raise ConfigurationError(f"Discord token invalid: {detail}")

    # 2. Provider + key.
    print("\n2) AI provider  (groq = generous free tier, openai = paid)")
    raw_provider = (
        (provider or ask("   Provider", existing.get("STT_PROVIDER", "groq"))).strip().lower()
    )
    if raw_provider not in SUPPORTED_PROVIDERS:
        raise ConfigurationError("provider must be groq or openai")
    key_name = "GROQ_API_KEY" if raw_provider == "groq" else "OPENAI_API_KEY"
    raw_key = (
        provider_key
        if provider_key is not None
        else ask(f"   {key_name}", existing.get(key_name, ""), secret=True)
    )
    key = validate_token_shape(raw_key, key_name)
    if not skip_validation:
        ok, detail = active_probes["provider"](raw_provider, key)
        print(f"   {key_name}: {detail}")
        if not ok:
            raise ConfigurationError(f"{key_name} invalid: {detail}")

    # 3. Owner user ID.
    print(
        "\n3) Owner  (Discord > Settings > Advanced > Developer Mode, "
        "then right-click yourself > Copy User ID)"
    )
    raw_owner = (
        owner_id
        if owner_id is not None
        else ask("   Your Discord user ID", existing.get("ALLOWED_USER_IDS", ""))
    )
    owner = validate_user_id(raw_owner)

    # Optional extras with safe defaults.
    print("\nOptional (Enter keeps the default):")
    tz_default = timezone_name or existing.get("TIMEZONE", "UTC")
    timezone = validate_timezone_name(
        tz_default if non_interactive else ask("   Timezone", tz_default)
    )

    updates = {
        "DISCORD_TOKEN": token,
        "STT_PROVIDER": raw_provider,
        "SUMMARY_PROVIDER": existing.get("SUMMARY_PROVIDER", raw_provider),
        key_name: key,
        "ALLOWED_USER_IDS": owner,
        "TIMEZONE": timezone,
    }
    write_env_updates(project_root / ".env", updates)
    print(f"\nWrote {(project_root / '.env')} (mode 0600 where supported).")

    if client_id.strip():
        print("\nInvite the bot with this URL:")
        print(f"  {build_invite_url(client_id)}")
    print(
        "Then enable the privileged Message Content Intent for the bot "
        "(Bot page > Privileged Gateway Intents)."
    )
    print("\nNext steps:")
    print("  uv run voicebot doctor     # verify everything")
    print("  uv run python bot.py       # start the bot")
    print("  uv run python bot.py --dashboard   # + local web UI")
    return 0
