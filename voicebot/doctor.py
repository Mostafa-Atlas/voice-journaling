"""Preflight checks for the voice memo bot.

Verifies everything a fresh Ubuntu install needs before the bot starts:
interpreter, dependencies, ``.env``, config validity, credential validity,
and writable runtime paths. Every failure prints the exact fix.

Run it::

    uv run voicebot doctor
    uv run voicebot doctor --json   # machine-readable output
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import ConfigurationError, Settings
from .dashboard import merged_disk_env
from .setup import DEFAULT_PROBES, Probe

log = logging.getLogger("voicebot.doctor")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fix: str = ""


@dataclass
class DoctorReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(check.ok for check in self.checks)

    def add(self, name: str, ok: bool, detail: str = "", fix: str = "") -> None:
        self.checks.append(Check(name, ok, detail, fix))

    def human(self) -> str:
        lines = []
        for check in self.checks:
            mark = "ok " if check.ok else "!! "
            lines.append(f"[{mark}] {check.name}" + (f" - {check.detail}" if check.detail else ""))
            if not check.ok and check.fix:
                lines.append(f"      fix: {check.fix}")
        lines.append("All checks passed." if self.ok else "Some checks failed (see fixes above).")
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps({"ok": self.ok, "checks": [asdict(c) for c in self.checks]}, indent=2)


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return os.access(path, os.W_OK)


def run_doctor(
    project_root: Path,
    *,
    probes: dict[str, Probe] | None = None,
    env: dict[str, str] | None = None,
) -> DoctorReport:
    """Run all checks. ``env`` overrides the merged disk env (used by tests)."""
    active_probes = probes or DEFAULT_PROBES
    report = DoctorReport()

    if sys.version_info < (3, 11):  # noqa: UP036 - reachable when run under system Python
        report.add(
            "python version",
            False,
            f"found {sys.version.split()[0]}, need 3.11+",
            fix="install Python 3.11+ or run ./scripts/bootstrap-ubuntu.sh",
        )
    else:
        report.add("python version", True, sys.version.split()[0])

    missing_deps = []
    for module in ("discord", "groq", "dotenv"):
        try:
            __import__(module)
        except ImportError:
            missing_deps.append(module)
    if missing_deps:
        report.add(
            "dependencies",
            False,
            f"missing: {', '.join(missing_deps)}",
            fix="run: uv sync",
        )
    else:
        report.add("dependencies", True, "discord, groq, dotenv importable")

    env_file = project_root / ".env"
    if not env_file.is_file():
        report.add(
            ".env file",
            False,
            "not found",
            fix="run: uv run voicebot setup",
        )
        return report
    report.add(".env file", True, str(env_file))

    merged = dict(env) if env is not None else merged_disk_env(project_root)
    try:
        settings = Settings.load(merged, project_root=project_root)
    except ConfigurationError as exc:
        report.add(
            "configuration",
            False,
            str(exc),
            fix="run: uv run voicebot setup (or edit .env by hand)",
        )
        return report
    report.add(
        "configuration",
        True,
        f"providers {settings.stt_provider}/{settings.summary_provider}, "
        f"{len(settings.allowed_users)} owner(s)",
    )

    ok, detail = _safe_probe(active_probes["discord"], settings.discord_token)
    report.add(
        "discord token",
        ok,
        detail,
        fix="" if ok else "paste a fresh token via: uv run voicebot setup",
    )

    for provider in dict.fromkeys((settings.stt_provider, settings.summary_provider)):
        key = settings.groq_api_key if provider == "groq" else settings.openai_api_key
        key_name = "GROQ_API_KEY" if provider == "groq" else "OPENAI_API_KEY"
        if not key:
            report.add(
                f"{provider} key",
                False,
                f"{key_name} is empty",
                fix="run: uv run voicebot setup",
            )
            continue
        ok, detail = _safe_probe(active_probes["provider"], provider, key)
        report.add(
            f"{provider} key",
            ok,
            detail,
            fix="" if ok else "paste a fresh key via: uv run voicebot setup",
        )

    try:
        from .database import Database

        database = Database(settings.database_path)
        database.initialize()
        integrity = database.integrity_check()
        report.add(
            "database",
            integrity == "ok",
            f"{settings.database_path} ({integrity})",
            fix="" if integrity == "ok" else "back up the file, delete it, and restart",
        )
    except OSError as exc:
        report.add(
            "database",
            False,
            f"{type(exc).__name__}",
            fix="check DATA_ROOT/DATABASE_PATH permissions",
        )

    for label, path in (("voice logs", settings.voice_log_dir), ("logs", settings.log_dir)):
        if _writable(path):
            report.add(label, True, str(path))
        else:
            report.add(
                label,
                False,
                f"{path} is not writable",
                fix="chown it to your user or pick another DATA_ROOT",
            )

    if settings.obsidian_enabled:
        vault = settings.obsidian_vault_path
        if vault is not None and _writable(vault):
            report.add("obsidian vault", True, str(vault))
        else:
            report.add(
                "obsidian vault",
                False,
                f"{vault} is not writable",
                fix="check OBSIDIAN_VAULT_PATH or set OBSIDIAN_ENABLED=false",
            )
    else:
        report.add("obsidian vault", True, "disabled — nothing to check")
    return report


def _safe_probe(probe: Callable, *args: object) -> tuple[bool, str]:
    try:
        result = probe(*args)
    except ConfigurationError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 - probes hit the network
        return False, f"check failed: {type(exc).__name__}"
    return bool(result[0]), str(result[1])


def main(
    project_root: Path,
    *,
    output_json: bool = False,
    probes: dict[str, Probe] | None = None,
) -> int:
    report = run_doctor(project_root, probes=probes)
    print(report.to_json() if output_json else report.human())
    return 0 if report.ok else 1
