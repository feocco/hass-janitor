"""Shared dataclasses for the Home Assistant janitor."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class UpdateEntity:
    """A Home Assistant update entity that has an update available."""

    entity_id: str
    name: str
    installed_version: str
    latest_version: str
    last_changed: str | None = None
    last_updated: str | None = None


@dataclass
class UpdateAttempt:
    """The outcome of trying to install one update entity."""

    entity_id: str
    name: str
    from_version: str
    to_version: str
    result: str
    started_at: datetime
    finished_at: datetime
    notes: str = ""


@dataclass
class RestartResult:
    """The outcome of the end-of-run Home Assistant restart."""

    result: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    notes: str = ""


@dataclass
class RunSummary:
    """Summary of a complete janitor run."""

    started_at: datetime
    finished_at: datetime
    base_url: str
    mode: str = "run"
    discovered_count: int = 0
    attempted_count: int = 0
    succeeded_count: int = 0
    failed_count: int = 0
    timed_out_count: int = 0
    updates: list[UpdateAttempt] = field(default_factory=list)
    restart: RestartResult = field(
        default_factory=lambda: RestartResult(result="not_needed")
    )
    notes: str = ""
    exit_code: int = 0
