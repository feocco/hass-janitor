"""Markdown audit log rendering."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from .models import RunSummary


def append_audit_log(path: Path, summary: RunSummary) -> None:
    """Append one Markdown audit entry to the audit log file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    entry = render_audit_entry(summary)

    needs_spacing = path.exists() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        if needs_spacing:
            handle.write("\n")
        handle.write(entry)


def render_audit_entry(summary: RunSummary) -> str:
    """Render a run summary as one Markdown section."""

    host = urlparse(summary.base_url).netloc or summary.base_url
    lines = [
        f"## {format_timestamp(summary.started_at)}",
        "",
        f"- Mode: `{summary.mode}`",
        f"- Base URL host: `{host}`",
        f"- Started: `{format_timestamp(summary.started_at)}`",
        f"- Finished: `{format_timestamp(summary.finished_at)}`",
        f"- Discovered updates: `{summary.discovered_count}`",
        f"- Attempted updates: `{summary.attempted_count}`",
        f"- Succeeded: `{summary.succeeded_count}`",
        f"- Failed: `{summary.failed_count}`",
        f"- Timed out: `{summary.timed_out_count}`",
        f"- Restart: `{summary.restart.result}`",
    ]

    if summary.restart.notes:
        lines.append(f"- Restart notes: {escape_cell(summary.restart.notes)}")
    if summary.notes:
        lines.append(f"- Notes: {escape_cell(summary.notes)}")

    lines.extend(
        [
            "",
            "| Entity | Name | From | To | Result | Started | Finished | Notes/Error |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )

    if summary.updates:
        for attempt in summary.updates:
            lines.append(
                "| "
                + " | ".join(
                    [
                        escape_cell(attempt.entity_id),
                        escape_cell(attempt.name),
                        escape_cell(attempt.from_version),
                        escape_cell(attempt.to_version),
                        escape_cell(attempt.result),
                        escape_cell(format_timestamp(attempt.started_at)),
                        escape_cell(format_timestamp(attempt.finished_at)),
                        escape_cell(attempt.notes or "-"),
                    ]
                )
                + " |"
            )
    else:
        note = summary.notes or "No updates found."
        lines.append(
            "| _No updates_ | - | - | - | - | - | - | "
            f"{escape_cell(note)} |"
        )

    lines.append("")
    return "\n".join(lines)


def format_timestamp(value) -> str:
    """Format datetimes consistently for the audit log."""

    if value is None:
        return "-"
    return value.isoformat(timespec="seconds")


def escape_cell(value: object) -> str:
    """Escape Markdown table content."""

    text = str(value)
    return text.replace("|", r"\|").replace("\n", "<br>")
