"""Update discovery and orchestration."""

from __future__ import annotations

from datetime import datetime, timedelta
import time
from typing import Any, Callable, Sequence

from .client import HAAuthError, HAConnectionError, HAResponseError
from .models import RestartResult, RunSummary, UpdateAttempt, UpdateEntity

SYSTEM_UPDATE_ORDER = (
    "update.home_assistant_supervisor_update",
    "update.home_assistant_operating_system_update",
    "update.home_assistant_core_update",
)


def discover_updates(states: Sequence[dict[str, Any]]) -> list[UpdateEntity]:
    """Select update entities whose state indicates an available update."""

    updates: list[UpdateEntity] = []
    for state in states:
        entity_id = str(state.get("entity_id", "")).strip()
        if not entity_id.startswith("update."):
            continue
        if state.get("state") != "on":
            continue

        attributes = state.get("attributes") or {}
        updates.append(
            UpdateEntity(
                entity_id=entity_id,
                name=_display_name(entity_id, attributes),
                installed_version=_normalize_version(attributes.get("installed_version")),
                latest_version=_normalize_version(attributes.get("latest_version")),
                last_changed=state.get("last_changed"),
                last_updated=state.get("last_updated"),
            )
        )

    return updates


def order_updates(updates: Sequence[UpdateEntity]) -> list[UpdateEntity]:
    """Order non-system updates alphabetically and system updates last."""

    system_rank = {entity_id: index for index, entity_id in enumerate(SYSTEM_UPDATE_ORDER)}
    non_system = [item for item in updates if item.entity_id not in system_rank]
    system = [item for item in updates if item.entity_id in system_rank]

    non_system.sort(key=lambda item: (item.name.casefold(), item.entity_id))
    system.sort(key=lambda item: system_rank[item.entity_id])
    return [*non_system, *system]


class UpdateRunner:
    """Perform one Home Assistant update sweep."""

    def __init__(
        self,
        client: Any,
        base_url: str,
        *,
        poll_interval_seconds: int = 10,
        install_timeout_seconds: int = 20 * 60,
        restart_initial_delay_seconds: int = 15,
        restart_timeout_seconds: int = 15 * 60,
        now_func: Callable[[], datetime] | None = None,
        sleep_func: Callable[[float], None] | None = None,
    ) -> None:
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.poll_interval_seconds = poll_interval_seconds
        self.install_timeout_seconds = install_timeout_seconds
        self.restart_initial_delay_seconds = restart_initial_delay_seconds
        self.restart_timeout_seconds = restart_timeout_seconds
        self._now = now_func or (lambda: datetime.now().astimezone())
        self._sleep = sleep_func or time.sleep

    def run(self) -> RunSummary:
        """Execute the update workflow and return a structured summary."""

        summary, updates = self._discover_updates_summary(mode="run")
        if summary.exit_code == 2 or not updates:
            return summary

        install_request_accepted = False
        fatal_error = ""

        for update in updates:
            try:
                attempt, accepted = self._install_update(update)
            except HAAuthError as exc:
                attempt = self._failed_attempt(
                    update,
                    started_at=self._now(),
                    notes=f"Authentication failed during update run: {exc}",
                )
                summary.updates.append(attempt)
                fatal_error = f"Authentication failed during update run: {exc}"
                break
            except HAConnectionError as exc:
                attempt = self._failed_attempt(
                    update,
                    started_at=self._now(),
                    notes=f"Connectivity failed during update run: {exc}",
                )
                summary.updates.append(attempt)
                fatal_error = f"Connectivity failed during update run: {exc}"
                break

            summary.updates.append(attempt)
            install_request_accepted = install_request_accepted or accepted

        summary.attempted_count = len(summary.updates)
        summary.succeeded_count = sum(
            1 for attempt in summary.updates if attempt.result == "success"
        )
        summary.failed_count = sum(
            1 for attempt in summary.updates if attempt.result == "failed"
        )
        summary.timed_out_count = sum(
            1 for attempt in summary.updates if attempt.result == "timed_out"
        )

        if install_request_accepted:
            summary.restart = self._restart_home_assistant()

        if fatal_error:
            summary.notes = fatal_error

        if not summary.notes and summary.failed_count == 0 and summary.timed_out_count == 0:
            summary.notes = "Update run completed."

        summary.finished_at = self._now()
        summary.exit_code = self._determine_exit_code(summary)
        return summary

    def dry_run(self) -> RunSummary:
        """Discover updates and record what would be done without mutating Home Assistant."""

        return self._preview(mode="dry-run")

    def preflight(self) -> RunSummary:
        """Discover updates and record the plan for a confirmed run."""

        return self._preview(mode="preflight")

    def _discover_updates_summary(
        self, *, mode: str
    ) -> tuple[RunSummary, list[UpdateEntity]]:
        started_at = self._now()
        summary = RunSummary(
            started_at=started_at,
            finished_at=started_at,
            base_url=self.base_url,
            mode=mode,
        )

        try:
            self.client.health_check()
            states = self.client.list_states()
        except HAAuthError as exc:
            summary.finished_at = self._now()
            summary.notes = f"Initial authentication failed: {exc}"
            summary.exit_code = 2
            return summary, []
        except (HAConnectionError, HAResponseError) as exc:
            summary.finished_at = self._now()
            summary.notes = f"Initial connectivity check failed: {exc}"
            summary.exit_code = 2
            return summary, []

        updates = order_updates(discover_updates(states))
        summary.discovered_count = len(updates)

        if not updates:
            summary.finished_at = self._now()
            if mode == "dry-run":
                summary.notes = "Dry run found no updates."
            else:
                summary.notes = "No updates found."
            return summary, []

        return summary, updates

    def _preview(self, *, mode: str) -> RunSummary:
        summary, updates = self._discover_updates_summary(mode=mode)
        if summary.exit_code == 2 or not updates:
            return summary

        timestamp = self._now()
        result = "dry_run" if mode == "dry-run" else "planned"
        note = (
            "Dry run only. No install request was sent."
            if mode == "dry-run"
            else "Preflight only. Run with --confirm to install."
        )
        summary.updates = [
            UpdateAttempt(
                entity_id=update.entity_id,
                name=update.name,
                from_version=update.installed_version,
                to_version=update.latest_version,
                result=result,
                started_at=timestamp,
                finished_at=timestamp,
                notes=note,
            )
            for update in updates
        ]
        summary.notes = (
            "Dry run only. No install or restart actions were performed."
            if mode == "dry-run"
            else "Preflight only. No install or restart actions were performed."
        )
        summary.finished_at = self._now()
        return summary

    def _install_update(self, update: UpdateEntity) -> tuple[UpdateAttempt, bool]:
        started_at = self._now()
        request_accepted = True
        last_note = "Install request accepted."

        try:
            self.client.install_update(update.entity_id)
        except HAConnectionError as exc:
            last_note = (
                "Install request returned a connection error; polling entity state "
                f"because the update may have started anyway: {exc}"
            )
        except HAResponseError as exc:
            return (
                UpdateAttempt(
                    entity_id=update.entity_id,
                    name=update.name,
                    from_version=update.installed_version,
                    to_version=update.latest_version,
                    result="failed",
                    started_at=started_at,
                    finished_at=self._now(),
                    notes=f"Install request failed: {exc}",
                ),
                False,
            )

        deadline = started_at + timedelta(seconds=self.install_timeout_seconds)

        while self._now() < deadline:
            self._sleep(self.poll_interval_seconds)
            try:
                current_state = self.client.get_state(update.entity_id)
            except HAAuthError:
                raise
            except HAConnectionError as exc:
                last_note = f"Transient connectivity issue while polling: {exc}"
                continue
            except HAResponseError as exc:
                return (
                    UpdateAttempt(
                        entity_id=update.entity_id,
                        name=update.name,
                        from_version=update.installed_version,
                        to_version=update.latest_version,
                        result="failed",
                        started_at=started_at,
                        finished_at=self._now(),
                        notes=f"Polling failed: {exc}",
                    ),
                    True,
                )

            attributes = current_state.get("attributes") or {}
            current_status = str(current_state.get("state", ""))
            current_installed = _normalize_version(attributes.get("installed_version"))

            if current_status == "off":
                return (
                    UpdateAttempt(
                        entity_id=update.entity_id,
                        name=update.name,
                        from_version=update.installed_version,
                        to_version=update.latest_version,
                        result="success",
                        started_at=started_at,
                        finished_at=self._now(),
                        notes="Entity returned to off after installation.",
                    ),
                    request_accepted,
                )

            if current_installed == update.latest_version:
                return (
                    UpdateAttempt(
                        entity_id=update.entity_id,
                        name=update.name,
                        from_version=update.installed_version,
                        to_version=update.latest_version,
                        result="success",
                        started_at=started_at,
                        finished_at=self._now(),
                        notes="Installed version matched the targeted latest version.",
                    ),
                    request_accepted,
                )

            last_note = (
                "Waiting for install to finish. "
                f"Current installed version is {current_installed}."
            )

        return (
            UpdateAttempt(
                entity_id=update.entity_id,
                name=update.name,
                from_version=update.installed_version,
                to_version=update.latest_version,
                result="timed_out",
                started_at=started_at,
                finished_at=self._now(),
                notes=last_note,
            ),
            request_accepted,
        )

    def _restart_home_assistant(self) -> RestartResult:
        started_at = self._now()
        restart_note = "Restart request accepted."

        try:
            self.client.restart_home_assistant()
        except HAAuthError as exc:
            return RestartResult(
                result="failed",
                started_at=started_at,
                finished_at=self._now(),
                notes=f"Restart authentication failed: {exc}",
            )
        except HAResponseError as exc:
            return RestartResult(
                result="failed",
                started_at=started_at,
                finished_at=self._now(),
                notes=f"Restart request failed: {exc}",
            )
        except HAConnectionError as exc:
            restart_note = (
                "Connection dropped after restart request, treating it as expected: "
                f"{exc}"
            )

        self._sleep(self.restart_initial_delay_seconds)
        deadline = self._now() + timedelta(seconds=self.restart_timeout_seconds)
        last_error = restart_note

        while self._now() < deadline:
            try:
                self.client.health_check()
                self.client.list_states()
                return RestartResult(
                    result="succeeded",
                    started_at=started_at,
                    finished_at=self._now(),
                    notes="Home Assistant API recovered after restart.",
                )
            except HAAuthError as exc:
                return RestartResult(
                    result="failed",
                    started_at=started_at,
                    finished_at=self._now(),
                    notes=f"Restart recovery authentication failed: {exc}",
                )
            except (HAConnectionError, HAResponseError) as exc:
                last_error = f"Waiting for API recovery: {exc}"
                self._sleep(self.poll_interval_seconds)

        return RestartResult(
            result="timed_out",
            started_at=started_at,
            finished_at=self._now(),
            notes=last_error,
        )

    def _failed_attempt(
        self, update: UpdateEntity, *, started_at: datetime, notes: str
    ) -> UpdateAttempt:
        return UpdateAttempt(
            entity_id=update.entity_id,
            name=update.name,
            from_version=update.installed_version,
            to_version=update.latest_version,
            result="failed",
            started_at=started_at,
            finished_at=self._now(),
            notes=notes,
        )

    @staticmethod
    def _determine_exit_code(summary: RunSummary) -> int:
        if summary.exit_code == 2:
            return 2
        if summary.failed_count or summary.timed_out_count:
            return 1
        if summary.restart.result in {"failed", "timed_out"}:
            return 1
        return 0


def _display_name(entity_id: str, attributes: dict[str, Any]) -> str:
    title = attributes.get("title")
    if title:
        return str(title)
    friendly_name = attributes.get("friendly_name")
    if friendly_name:
        return str(friendly_name)
    return entity_id


def _normalize_version(value: Any) -> str:
    if value is None:
        return "-"
    text = str(value).strip()
    return text or "-"
