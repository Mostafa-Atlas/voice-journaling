from __future__ import annotations

import html
import json
import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


class MemoStatus(StrEnum):
    RECEIVED = "received"
    AUDIO_SAVED = "audio_saved"
    TRANSCRIBED = "transcribed"
    SUMMARIZED = "summarized"
    ARTIFACTS_WRITTEN = "artifacts_written"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ActionItem:
    task: str
    owner: str | None = None
    deadline: str | None = None

    @classmethod
    def from_value(cls, value: Any) -> ActionItem:
        if isinstance(value, str):
            return cls(task=_clean(value, 1_000))
        if not isinstance(value, dict):
            raise ValueError("action item must be an object")
        task = _clean(value.get("task"), 1_000)
        owner = _clean_optional(value.get("owner"), 200)
        deadline = _clean_optional(value.get("deadline"), 200)
        return cls(task=task, owner=owner, deadline=deadline)


@dataclass(frozen=True, slots=True)
class SummaryData:
    summary: str
    key_points: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    action_items: tuple[ActionItem, ...] = ()
    mentioned: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SummaryData:
        expected = {"summary", "key_points", "decisions", "action_items", "mentioned"}
        if set(payload) != expected:
            missing = sorted(expected - set(payload))
            extra = sorted(set(payload) - expected)
            raise ValueError(f"invalid summary fields; missing={missing}, extra={extra}")
        return cls(
            summary=_clean(payload["summary"], 4_000),
            key_points=_clean_list(payload["key_points"]),
            decisions=_clean_list(payload["decisions"]),
            action_items=tuple(
                ActionItem.from_value(value)
                for value in _require_list(payload["action_items"])[:50]
            ),
            mentioned=_clean_list(payload["mentioned"]),
        )

    @classmethod
    def from_json(cls, value: str) -> SummaryData:
        payload = json.loads(value)
        if not isinstance(payload, dict):
            raise ValueError("summary response must be a JSON object")
        return cls.from_dict(payload)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))

    def to_markdown(self) -> str:
        sections = ["## Summary", escape_markdown(self.summary)]
        _append_list(sections, "Key Points", self.key_points)
        _append_list(sections, "Decisions", self.decisions)
        if self.action_items:
            sections.extend(["## Action Items"])
            for item in self.action_items:
                suffix = []
                if item.owner:
                    suffix.append(f"owner: {item.owner}")
                if item.deadline:
                    suffix.append(f"deadline: {item.deadline}")
                details = f" ({'; '.join(suffix)})" if suffix else ""
                sections.append(f"- [ ] {escape_markdown(item.task)}{escape_markdown(details)}")
        _append_list(sections, "Mentioned", self.mentioned)
        return "\n\n".join(sections).strip() + "\n"

    def teaser(self, max_length: int = 220) -> str:
        text = " ".join(self.summary.split())
        if len(text) <= max_length:
            return text
        shortened = text[: max_length - 1].rsplit(" ", 1)[0]
        return (shortened or text[: max_length - 1]) + "…"


@dataclass(frozen=True, slots=True)
class Memo:
    memo_id: str
    message_id: str
    attachment_id: str
    discord_id: str
    username: str
    original_filename: str
    content_type: str | None
    size_bytes: int
    received_at: str
    status: str
    audio_path: str | None = None
    transcript: str | None = None
    summary_json: str | None = None
    summary_text: str | None = None
    transcript_path: str | None = None
    summary_path: str | None = None
    daily_index_path: str | None = None
    error_stage: str | None = None
    error_message: str | None = None
    attempt_count: int = 0
    completed_at: str | None = None
    updated_at: str | None = None
    processing_ms: int = 0

    @property
    def summary(self) -> SummaryData | None:
        if not self.summary_json:
            return None
        return SummaryData.from_json(self.summary_json)


def _require_list(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError("summary list field must be an array")
    return value


def _clean(value: Any, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValueError("summary text field must be a string")
    cleaned = " ".join(value.split()).strip()
    if not cleaned:
        raise ValueError("summary text must not be blank")
    return cleaned[:max_length]


def _clean_optional(value: Any, max_length: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("optional summary field must be a string or null")
    cleaned = " ".join(value.split()).strip()
    return cleaned[:max_length] or None


def _clean_list(value: Any) -> tuple[str, ...]:
    result = []
    for item in _require_list(value)[:50]:
        if not isinstance(item, str):
            raise ValueError("summary list entries must be strings")
        cleaned = " ".join(item.split()).strip()
        if cleaned:
            result.append(cleaned[:1_000])
    return tuple(result)


def _append_list(sections: list[str], title: str, values: tuple[str, ...]) -> None:
    if values:
        sections.append(f"## {title}")
        sections.append("\n".join(f"- {escape_markdown(value)}" for value in values))


def escape_markdown(value: str) -> str:
    """Render untrusted text without activating Markdown links, images, or HTML."""
    escaped = html.escape(value, quote=False)
    return re.sub(r"([\\`*_\[\]<>#|])", r"\\\1", escaped)
