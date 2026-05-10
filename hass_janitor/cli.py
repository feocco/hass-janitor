"""CLI entrypoint for the Home Assistant janitor."""

from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path
import sys

from .audit import append_audit_log
from .client import HomeAssistantClient
from .models import RunSummary
from .runner import UpdateRunner

DEFAULT_ENV_PATH = Path(".env")
DEFAULT_AUDIT_PATH = Path("logs/ha-update-audit.md")


def main(argv: list[str] | None = None) -> int:
    """Parse CLI arguments and execute the requested command."""

    _load_dotenv()

    parser = argparse.ArgumentParser(
        prog="python -m hass_janitor",
        description="Run Home Assistant updates and append a Markdown audit log.",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.required = True
    run_parser = subparsers.add_parser("run", help="Run one Home Assistant update sweep.")
    run_parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually install updates after showing the preflight summary.",
    )
    subparsers.add_parser(
        "dry-run",
        help="Discover updates and write an audit entry without installing anything.",
    )

    args = parser.parse_args(argv)
    if args.command == "run":
        return run_command(confirm=args.confirm)
    if args.command == "dry-run":
        return dry_run_command()

    parser.error(f"Unsupported command: {args.command}")
    return 2


def run_command(*, confirm: bool) -> int:
    """Run the janitor once using environment-based configuration."""

    base_url, token, summary = _load_runtime_config()
    if summary is not None:
        append_audit_log(DEFAULT_AUDIT_PATH, summary)
        print(summary.notes, file=sys.stderr)
        return summary.exit_code

    client = HomeAssistantClient(base_url=base_url, token=token)
    runner = UpdateRunner(client, base_url=base_url)
    preflight = runner.preflight()
    print(_preflight_console_summary(preflight), file=sys.stdout)

    if preflight.exit_code == 2:
        append_audit_log(DEFAULT_AUDIT_PATH, preflight)
        return preflight.exit_code

    if preflight.discovered_count == 0:
        append_audit_log(DEFAULT_AUDIT_PATH, preflight)
        return preflight.exit_code

    if not confirm:
        append_audit_log(DEFAULT_AUDIT_PATH, preflight)
        print(
            "No changes were made. Re-run with `python -m hass_janitor run --confirm` "
            "to install these updates.",
            file=sys.stdout,
        )
        return 0

    summary = runner.run()
    append_audit_log(DEFAULT_AUDIT_PATH, summary)
    print(_console_summary(summary), file=sys.stdout)
    return summary.exit_code


def dry_run_command() -> int:
    """Run discovery only and record what would be updated."""

    base_url, token, summary = _load_runtime_config(mode="dry-run")
    if summary is not None:
        append_audit_log(DEFAULT_AUDIT_PATH, summary)
        print(summary.notes, file=sys.stderr)
        return summary.exit_code

    client = HomeAssistantClient(base_url=base_url, token=token)
    summary = UpdateRunner(client, base_url=base_url).dry_run()
    append_audit_log(DEFAULT_AUDIT_PATH, summary)
    print(_console_summary(summary), file=sys.stdout)
    return summary.exit_code


def _load_runtime_config(*, mode: str = "run") -> tuple[str, str, RunSummary | None]:
    base_url = os.environ.get("HA_BASE_URL", "").strip()
    token = os.environ.get("HA_TOKEN", "").strip()

    if not base_url:
        return (
            base_url,
            token,
            _configuration_error_summary(
                base_url="unset",
                message="HA_BASE_URL is required.",
                mode=mode,
            ),
        )

    if not token:
        return (
            base_url,
            token,
            _configuration_error_summary(
                base_url=base_url,
                message="HA_TOKEN is required.",
                mode=mode,
            ),
        )

    return base_url, token, None


def _configuration_error_summary(
    *, base_url: str, message: str, mode: str = "run"
) -> RunSummary:
    now = datetime.now().astimezone()
    return RunSummary(
        started_at=now,
        finished_at=now,
        base_url=base_url.rstrip("/"),
        mode=mode,
        notes=message,
        exit_code=2,
    )


def _console_summary(summary: RunSummary) -> str:
    if summary.exit_code == 2:
        return summary.notes

    if summary.mode == "dry-run":
        if summary.discovered_count == 0:
            return "Dry run found no updates. Audit appended to logs/ha-update-audit.md."
        return (
            f"Dry run found {summary.discovered_count} updates and would install "
            f"{summary.discovered_count} item(s). "
            "No install or restart actions were performed. "
            "Audit appended to logs/ha-update-audit.md."
        )

    if summary.discovered_count == 0:
        return "No updates found. Audit appended to logs/ha-update-audit.md."

    return (
        f"Processed {summary.attempted_count} of {summary.discovered_count} updates. "
        f"Succeeded: {summary.succeeded_count}, "
        f"failed: {summary.failed_count}, "
        f"timed out: {summary.timed_out_count}, "
        f"restart: {summary.restart.result}. "
        "Audit appended to logs/ha-update-audit.md."
    )


def _preflight_console_summary(summary: RunSummary) -> str:
    if summary.exit_code == 2:
        return summary.notes

    if summary.discovered_count == 0:
        return "Preflight summary:\n- No updates found."

    lines = [
        "Preflight summary:",
        f"- {summary.discovered_count} updates discovered.",
    ]
    for attempt in summary.updates:
        lines.append(
            f"- {attempt.name}: {attempt.from_version} -> {attempt.to_version}"
        )
    return "\n".join(lines)


def _load_dotenv(path: Path = DEFAULT_ENV_PATH) -> None:
    """Load simple KEY=VALUE pairs from a local .env file into the process."""

    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue

        if key.startswith("export "):
            key = key.removeprefix("export ").strip()

        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]

        os.environ.setdefault(key, value)
