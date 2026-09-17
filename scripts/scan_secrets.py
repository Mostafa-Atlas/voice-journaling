"""Dependency-free pre-commit secret scan for repository source files."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
IGNORED_PARTS = {".git", ".venv", "venv", "__pycache__", "voice_logs", "logs", "data"}
IGNORED_NAMES = {".env", "transcripts.db"}
FORBIDDEN_TRACKED_SUFFIXES = {".db", ".sqlite", ".ogg", ".mp3", ".wav", ".m4a", ".webm", ".flac"}
PATTERNS = {
    "Discord token": re.compile(r"\b(?:mfa\.[A-Za-z0-9_-]{20,}|[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,})\b"),
    "Groq/API key": re.compile(r"\b(?:gsk|sk)_[A-Za-z0-9_-]{16,}\b"),
    "Assigned secret": re.compile(
        r"^\s*(?:DISCORD_TOKEN|GROQ_API_KEY)\s*=\s*['\"]?(?!<|your-|$)[^\s'\"]{12,}"
    ),
}
URL = re.compile(r"https?://\S+")


def matching_patterns(line: str) -> list[str]:
    """Return matching detector names while ignoring token-like URL segments."""
    matches: list[str] = []
    for name, pattern in PATTERNS.items():
        candidate = URL.sub("", line) if name == "Discord token" else line
        if pattern.search(candidate):
            matches.append(name)
    return matches


def scan() -> list[str]:
    findings: list[str] = []
    tracked_mode, paths = _candidate_paths()
    for path in paths:
        relative = path.relative_to(ROOT)
        if tracked_mode and _forbidden_tracked(relative):
            findings.append(f"{relative}: private runtime file is tracked")
            continue
        if not path.is_file():
            continue
        try:
            raw = path.read_bytes()
            if len(raw) > 5 * 1024 * 1024 or b"\x00" in raw:
                continue
            text = raw.decode("utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            if path.name == ".env.example" and "<replace-me>" in line:
                continue
            for name in matching_patterns(line):
                findings.append(f"{path.relative_to(ROOT)}:{line_number}: {name}")
    return findings


def _candidate_paths() -> tuple[bool, list[Path]]:
    git_dir = ROOT / ".git"
    if git_dir.exists():
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
        names = [name for name in result.stdout.decode("utf-8").split("\0") if name]
        return True, [ROOT / name for name in names]
    paths = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.name in IGNORED_NAMES:
            continue
        if any(part in IGNORED_PARTS for part in path.relative_to(ROOT).parts):
            continue
        paths.append(path)
    return False, paths


def _forbidden_tracked(path: Path) -> bool:
    if path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example"):
        return True
    if path.suffix.lower() in FORBIDDEN_TRACKED_SUFFIXES:
        return True
    return any(part in {"voice_logs", "data", "logs", "exports"} for part in path.parts)


def main() -> int:
    findings = scan()
    if findings:
        print("Potential secrets found:", file=sys.stderr)
        for finding in findings:
            print(f"- {finding}", file=sys.stderr)
        return 1
    print("Secret scan passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
