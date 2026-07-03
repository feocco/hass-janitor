from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any
from unittest import IsolatedAsyncioTestCase, TestCase, mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from hass_janitor import cli
from hass_janitor.audit import render_audit_entry
from hass_janitor.client import HAAuthError, HAConnectionError, HAResponseError
from hass_janitor.monitor import (
    CONFIRM_ACTION,
    DISMISS_ACTION,
    JanitorMonitor,
    MonitorConfig,
    SNOOZE_ACTION,
    confirmation_action_token,
    parse_ha_datetime,
    summarize_update_event,
    update_event_signature,
)
from hass_janitor.models import RestartResult, RunSummary, UpdateAttempt
from hass_janitor.runner import UpdateRunner, discover_updates, order_updates
from hass_janitor.service import Handler, parse_mode


def build_state(
    entity_id: str,
    state: str,
    *,
    title: str | None = None,
    friendly_name: str | None = None,
    installed_version: str | None = None,
    latest_version: str | None = None,
) -> dict[str, Any]:
    attributes: dict[str, Any] = {}
    if title is not None:
        attributes["title"] = title
    if friendly_name is not None:
        attributes["friendly_name"] = friendly_name
    if installed_version is not None:
        attributes["installed_version"] = installed_version
    if latest_version is not None:
        attributes["latest_version"] = latest_version

    return {
        "entity_id": entity_id,
        "state": state,
        "attributes": attributes,
        "last_changed": "2026-04-18T18:00:00-04:00",
        "last_updated": "2026-04-18T18:00:00-04:00",
    }


class FakeClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 4, 18, 18, 0, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class FakeClient:
    def __init__(
        self,
        *,
        initial_states: list[dict[str, Any]],
        install_errors: dict[str, Exception] | None = None,
        poll_sequences: dict[str, list[Any]] | None = None,
        restart_error: Exception | None = None,
        health_sequence: list[Any] | None = None,
        list_states_sequence: list[Any] | None = None,
    ) -> None:
        self.base_url = "https://example.ui.nabu.casa"
        self.initial_states = initial_states
        self.install_errors = install_errors or {}
        self.poll_sequences = {
            key: list(value) for key, value in (poll_sequences or {}).items()
        }
        self.restart_error = restart_error
        self.health_sequence = list(health_sequence or [])
        self.list_states_sequence = list(list_states_sequence or [initial_states])
        self.install_calls: list[str] = []
        self.restart_calls = 0

    def health_check(self) -> dict[str, str]:
        if self.health_sequence:
            outcome = self.health_sequence.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
        return {"message": "API running."}

    def list_states(self) -> list[dict[str, Any]]:
        if self.list_states_sequence:
            outcome = self.list_states_sequence.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return []

    def install_update(self, entity_id: str) -> list[Any]:
        self.install_calls.append(entity_id)
        outcome = self.install_errors.get(entity_id)
        if outcome is not None:
            raise outcome
        return []

    def get_state(self, entity_id: str) -> dict[str, Any]:
        queue = self.poll_sequences[entity_id]
        outcome = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def restart_home_assistant(self) -> list[Any]:
        self.restart_calls += 1
        if self.restart_error is not None:
            raise self.restart_error
        return []


class FakeHTTPResponse:
    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.headers = {"Content-Type": "application/json"}

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def fake_urlopen_factory(expected_calls: list[tuple[str, str, Any]]) -> Any:
    remaining = list(expected_calls)

    def fake_urlopen(request, timeout=30):
        if not remaining:
            raise AssertionError("Unexpected extra HTTP request")

        method, suffix, payload = remaining.pop(0)
        if request.method != method:
            raise AssertionError(f"Expected {method}, got {request.method}")
        if not request.full_url.endswith(suffix):
            raise AssertionError(f"Expected URL ending with {suffix}, got {request.full_url}")

        if isinstance(payload, Exception):
            raise payload
        return FakeHTTPResponse(payload)

    return fake_urlopen


class MonitorFakeClient(FakeClient):
    def __init__(
        self,
        *,
        backup_state: str,
        backup_attributes: dict[str, Any] | None = None,
        initial_states: list[dict[str, Any]],
    ) -> None:
        super().__init__(initial_states=initial_states)
        self.backup_state = backup_state
        self.backup_attributes = backup_attributes or {"event_type": "completed"}

    def get_state(self, entity_id: str) -> dict[str, Any]:
        if entity_id in {"event.backup_automatic_backup", "sensor.backup_state"}:
            return {
                "entity_id": entity_id,
                "state": self.backup_state,
                "attributes": self.backup_attributes,
            }
        return super().get_state(entity_id)


@contextmanager
def temporary_cwd() -> Path:
    original = Path.cwd()
    with tempfile.TemporaryDirectory() as tmpdir:
        os.chdir(tmpdir)
        try:
            yield Path(tmpdir)
        finally:
            os.chdir(original)


class DiscoveryTests(TestCase):
    def test_discover_updates_only_returns_on_update_entities(self) -> None:
        states = [
            build_state(
                "update.zigbee2mqtt",
                "on",
                title="Zigbee2MQTT",
                installed_version="1.0.0",
                latest_version="1.1.0",
            ),
            build_state("update.already_done", "off", title="Already Done"),
            build_state("sensor.temperature", "on", friendly_name="Temp"),
        ]

        updates = discover_updates(states)

        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].entity_id, "update.zigbee2mqtt")
        self.assertEqual(updates[0].name, "Zigbee2MQTT")
        self.assertEqual(updates[0].installed_version, "1.0.0")
        self.assertEqual(updates[0].latest_version, "1.1.0")

    def test_order_updates_puts_system_updates_last(self) -> None:
        updates = discover_updates(
            [
                build_state(
                    "update.home_assistant_core_update",
                    "on",
                    title="Home Assistant Core",
                    installed_version="2026.4.0",
                    latest_version="2026.4.1",
                ),
                build_state(
                    "update.z_wave_js_ui",
                    "on",
                    title="Z-Wave JS UI",
                    installed_version="3.0.0",
                    latest_version="3.0.1",
                ),
                build_state(
                    "update.home_assistant_supervisor_update",
                    "on",
                    title="Home Assistant Supervisor",
                    installed_version="2026.04.0",
                    latest_version="2026.04.1",
                ),
            ]
        )

        ordered = order_updates(updates)

        self.assertEqual(
            [item.entity_id for item in ordered],
            [
                "update.z_wave_js_ui",
                "update.home_assistant_supervisor_update",
                "update.home_assistant_core_update",
            ],
        )


class AuditRenderingTests(TestCase):
    def test_render_audit_entry_for_no_updates(self) -> None:
        timestamp = datetime(2026, 4, 18, 18, 0, tzinfo=timezone.utc)
        summary = RunSummary(
            started_at=timestamp,
            finished_at=timestamp,
            base_url="https://example.ui.nabu.casa",
            notes="No updates found.",
        )

        rendered = render_audit_entry(summary)

        self.assertIn("No updates found.", rendered)
        self.assertIn("| _No updates_ |", rendered)
        self.assertIn("example.ui.nabu.casa", rendered)


class RunnerFlowTests(TestCase):
    def test_preflight_discovers_updates_without_installing_or_restarting(self) -> None:
        clock = FakeClock()
        initial_states = [
            build_state(
                "update.z_wave_js_ui",
                "on",
                title="Z-Wave JS UI",
                installed_version="3.0.0",
                latest_version="3.0.1",
            ),
            build_state(
                "update.home_assistant_core_update",
                "on",
                title="Home Assistant Core",
                installed_version="2026.4.0",
                latest_version="2026.4.1",
            ),
        ]
        client = FakeClient(initial_states=initial_states)

        summary = UpdateRunner(
            client,
            base_url=client.base_url,
            now_func=clock.now,
            sleep_func=clock.sleep,
        ).preflight()

        self.assertEqual(summary.exit_code, 0)
        self.assertEqual(summary.mode, "preflight")
        self.assertEqual(summary.discovered_count, 2)
        self.assertEqual(summary.attempted_count, 0)
        self.assertEqual(client.install_calls, [])
        self.assertEqual(client.restart_calls, 0)
        self.assertEqual(
            [attempt.entity_id for attempt in summary.updates],
            ["update.z_wave_js_ui", "update.home_assistant_core_update"],
        )
        self.assertTrue(all(attempt.result == "planned" for attempt in summary.updates))

    def test_dry_run_discovers_updates_without_installing_or_restarting(self) -> None:
        clock = FakeClock()
        initial_states = [
            build_state(
                "update.z_wave_js_ui",
                "on",
                title="Z-Wave JS UI",
                installed_version="3.0.0",
                latest_version="3.0.1",
            ),
            build_state(
                "update.home_assistant_core_update",
                "on",
                title="Home Assistant Core",
                installed_version="2026.4.0",
                latest_version="2026.4.1",
            ),
        ]
        client = FakeClient(initial_states=initial_states)

        summary = UpdateRunner(
            client,
            base_url=client.base_url,
            now_func=clock.now,
            sleep_func=clock.sleep,
        ).dry_run()

        self.assertEqual(summary.exit_code, 0)
        self.assertEqual(summary.mode, "dry-run")
        self.assertEqual(summary.discovered_count, 2)
        self.assertEqual(summary.attempted_count, 0)
        self.assertEqual(client.install_calls, [])
        self.assertEqual(client.restart_calls, 0)
        self.assertEqual(
            [attempt.entity_id for attempt in summary.updates],
            ["update.z_wave_js_ui", "update.home_assistant_core_update"],
        )
        self.assertTrue(all(attempt.result == "dry_run" for attempt in summary.updates))

    def test_all_updates_succeed_and_restart_recovers(self) -> None:
        clock = FakeClock()
        initial_states = [
            build_state(
                "update.z_wave_js_ui",
                "on",
                title="Z-Wave JS UI",
                installed_version="3.0.0",
                latest_version="3.0.1",
            ),
            build_state(
                "update.home_assistant_core_update",
                "on",
                title="Home Assistant Core",
                installed_version="2026.4.0",
                latest_version="2026.4.1",
            ),
        ]
        client = FakeClient(
            initial_states=initial_states,
            poll_sequences={
                "update.z_wave_js_ui": [
                    build_state(
                        "update.z_wave_js_ui",
                        "off",
                        title="Z-Wave JS UI",
                        installed_version="3.0.1",
                        latest_version="3.0.1",
                    )
                ],
                "update.home_assistant_core_update": [
                    build_state(
                        "update.home_assistant_core_update",
                        "on",
                        title="Home Assistant Core",
                        installed_version="2026.4.1",
                        latest_version="2026.4.1",
                    )
                ],
            },
            health_sequence=[None, HAConnectionError("booting"), None],
            list_states_sequence=[initial_states, []],
        )

        summary = UpdateRunner(
            client,
            base_url=client.base_url,
            now_func=clock.now,
            sleep_func=clock.sleep,
            poll_interval_seconds=10,
            restart_timeout_seconds=30,
        ).run()

        self.assertEqual(summary.exit_code, 0)
        self.assertEqual(summary.succeeded_count, 2)
        self.assertEqual(summary.restart.result, "succeeded")
        self.assertEqual(
            client.install_calls,
            ["update.z_wave_js_ui", "update.home_assistant_core_update"],
        )

    def test_one_update_failure_does_not_stop_later_updates(self) -> None:
        clock = FakeClock()
        initial_states = [
            build_state(
                "update.broken_addon",
                "on",
                title="Broken Add-on",
                installed_version="1.0.0",
                latest_version="1.1.0",
            ),
            build_state(
                "update.good_addon",
                "on",
                title="Good Add-on",
                installed_version="2.0.0",
                latest_version="2.1.0",
            ),
        ]
        client = FakeClient(
            initial_states=initial_states,
            install_errors={
                "update.broken_addon": HAResponseError(400, "Bad request")
            },
            poll_sequences={
                "update.good_addon": [
                    build_state(
                        "update.good_addon",
                        "on",
                        title="Good Add-on",
                        installed_version="2.1.0",
                        latest_version="2.1.0",
                    )
                ]
            },
            list_states_sequence=[initial_states, []],
        )

        summary = UpdateRunner(
            client,
            base_url=client.base_url,
            now_func=clock.now,
            sleep_func=clock.sleep,
            restart_timeout_seconds=30,
        ).run()

        self.assertEqual(summary.exit_code, 1)
        self.assertEqual(summary.succeeded_count, 1)
        self.assertEqual(summary.failed_count, 1)
        self.assertEqual(
            client.install_calls,
            ["update.broken_addon", "update.good_addon"],
        )
        self.assertEqual(summary.restart.result, "succeeded")

    def test_install_request_connection_error_can_still_resolve_as_success(self) -> None:
        clock = FakeClock()
        initial_states = [
            build_state(
                "update.matter_server_update",
                "on",
                title="Matter Server",
                installed_version="8.2.2",
                latest_version="8.4.0",
            ),
            build_state(
                "update.terminal_ssh_update",
                "on",
                title="Terminal & SSH",
                installed_version="10.0.0",
                latest_version="10.1.0",
            ),
        ]
        client = FakeClient(
            initial_states=initial_states,
            install_errors={
                "update.matter_server_update": HAConnectionError("read timed out")
            },
            poll_sequences={
                "update.matter_server_update": [
                    build_state(
                        "update.matter_server_update",
                        "off",
                        title="Matter Server",
                        installed_version="8.4.0",
                        latest_version="8.4.0",
                    )
                ],
                "update.terminal_ssh_update": [
                    build_state(
                        "update.terminal_ssh_update",
                        "off",
                        title="Terminal & SSH",
                        installed_version="10.1.0",
                        latest_version="10.1.0",
                    )
                ],
            },
            list_states_sequence=[initial_states, []],
        )

        summary = UpdateRunner(
            client,
            base_url=client.base_url,
            now_func=clock.now,
            sleep_func=clock.sleep,
            restart_timeout_seconds=30,
        ).run()

        self.assertEqual(summary.exit_code, 0)
        self.assertEqual(summary.succeeded_count, 2)
        self.assertEqual(
            [attempt.entity_id for attempt in summary.updates],
            ["update.matter_server_update", "update.terminal_ssh_update"],
        )
        self.assertEqual(client.install_calls, ["update.matter_server_update", "update.terminal_ssh_update"])

    def test_update_timeout_is_recorded(self) -> None:
        clock = FakeClock()
        initial_states = [
            build_state(
                "update.slow_addon",
                "on",
                title="Slow Add-on",
                installed_version="1.0.0",
                latest_version="1.1.0",
            )
        ]
        client = FakeClient(
            initial_states=initial_states,
            poll_sequences={
                "update.slow_addon": [
                    build_state(
                        "update.slow_addon",
                        "on",
                        title="Slow Add-on",
                        installed_version="1.0.0",
                        latest_version="1.1.0",
                    )
                ]
            },
            list_states_sequence=[initial_states, []],
        )

        summary = UpdateRunner(
            client,
            base_url=client.base_url,
            now_func=clock.now,
            sleep_func=clock.sleep,
            poll_interval_seconds=5,
            install_timeout_seconds=15,
            restart_timeout_seconds=30,
        ).run()

        self.assertEqual(summary.exit_code, 1)
        self.assertEqual(summary.timed_out_count, 1)
        self.assertEqual(summary.restart.result, "succeeded")

    def test_initial_auth_failure_returns_exit_code_two(self) -> None:
        clock = FakeClock()
        client = FakeClient(
            initial_states=[],
            health_sequence=[HAAuthError("Invalid token")],
        )

        summary = UpdateRunner(
            client,
            base_url=client.base_url,
            now_func=clock.now,
            sleep_func=clock.sleep,
        ).run()

        self.assertEqual(summary.exit_code, 2)
        self.assertIn("Initial authentication failed", summary.notes)

    def test_restart_timeout_is_reported(self) -> None:
        clock = FakeClock()
        initial_states = [
            build_state(
                "update.good_addon",
                "on",
                title="Good Add-on",
                installed_version="2.0.0",
                latest_version="2.1.0",
            )
        ]
        client = FakeClient(
            initial_states=initial_states,
            poll_sequences={
                "update.good_addon": [
                    build_state(
                        "update.good_addon",
                        "off",
                        title="Good Add-on",
                        installed_version="2.1.0",
                        latest_version="2.1.0",
                    )
                ]
            },
            health_sequence=[
                None,
                HAConnectionError("still booting"),
                HAConnectionError("still booting"),
                HAConnectionError("still booting"),
            ],
        )

        summary = UpdateRunner(
            client,
            base_url=client.base_url,
            now_func=clock.now,
            sleep_func=clock.sleep,
            poll_interval_seconds=5,
            restart_timeout_seconds=15,
        ).run()

        self.assertEqual(summary.exit_code, 1)
        self.assertEqual(summary.restart.result, "timed_out")


class CliSmokeTests(TestCase):
    def test_cli_main_requires_ha_base_url(self) -> None:
        with temporary_cwd() as tmpdir:
            with (
                mock.patch.dict(os.environ, {"HA_TOKEN": "test-token"}, clear=True),
                mock.patch("hass_janitor.client.urlopen") as urlopen_mock,
            ):
                exit_code = cli.main(["dry-run"])

            audit_path = tmpdir / "logs" / "ha-update-audit.md"
            audit_exists = audit_path.exists()
            audit_contents = (
                audit_path.read_text(encoding="utf-8") if audit_exists else ""
            )

        self.assertEqual(exit_code, 2)
        urlopen_mock.assert_not_called()
        self.assertTrue(audit_exists)
        self.assertIn("HA_BASE_URL is required.", audit_contents)
        self.assertIn("Base URL host: `unset`", audit_contents)

    def test_cli_main_dry_run_loads_credentials_from_dotenv(self) -> None:
        expected_calls = [
            ("GET", "/api/", {"message": "API running."}),
            (
                "GET",
                "/api/states",
                [
                    build_state(
                        "update.example_addon",
                        "on",
                        title="Example Add-on",
                        installed_version="1.0.0",
                        latest_version="1.1.0",
                    )
                ],
            ),
        ]

        with temporary_cwd() as tmpdir:
            (tmpdir / ".env").write_text(
                "\n".join(
                    [
                        "HA_BASE_URL=https://example.ui.nabu.casa",
                        "HA_TOKEN=test-token",
                    ]
                ),
                encoding="utf-8",
            )

            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch("hass_janitor.client.urlopen", fake_urlopen_factory(expected_calls)),
            ):
                exit_code = cli.main(["dry-run"])

            audit_path = tmpdir / "logs" / "ha-update-audit.md"
            audit_exists = audit_path.exists()
            audit_contents = (
                audit_path.read_text(encoding="utf-8") if audit_exists else ""
            )

        self.assertEqual(exit_code, 0)
        self.assertTrue(audit_exists)
        self.assertIn("Mode: `dry-run`", audit_contents)
        self.assertIn("Example Add-on", audit_contents)

    def test_cli_main_prefers_environment_over_dotenv(self) -> None:
        expected_calls = [
            ("GET", "/api/", {"message": "API running."}),
            (
                "GET",
                "/api/states",
                [
                    build_state(
                        "update.example_addon",
                        "on",
                        title="Example Add-on",
                        installed_version="1.0.0",
                        latest_version="1.1.0",
                    )
                ],
            ),
        ]

        with temporary_cwd() as tmpdir:
            (tmpdir / ".env").write_text(
                "\n".join(
                    [
                        "HA_BASE_URL=https://wrong.example",
                        "HA_TOKEN=wrong-token",
                    ]
                ),
                encoding="utf-8",
            )

            env = {
                "HA_TOKEN": "test-token",
                "HA_BASE_URL": "https://example.ui.nabu.casa",
            }
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("hass_janitor.client.urlopen", fake_urlopen_factory(expected_calls)),
            ):
                exit_code = cli.main(["dry-run"])

        self.assertEqual(exit_code, 0)

    def test_cli_main_dry_run_writes_audit_log_without_mutation_calls(self) -> None:
        expected_calls = [
            ("GET", "/api/", {"message": "API running."}),
            (
                "GET",
                "/api/states",
                [
                    build_state(
                        "update.example_addon",
                        "on",
                        title="Example Add-on",
                        installed_version="1.0.0",
                        latest_version="1.1.0",
                    )
                ],
            ),
        ]

        with temporary_cwd() as tmpdir:
            env = {
                "HA_TOKEN": "test-token",
                "HA_BASE_URL": "https://example.ui.nabu.casa",
            }
            with (
                mock.patch.dict(os.environ, env, clear=False),
                mock.patch("hass_janitor.client.urlopen", fake_urlopen_factory(expected_calls)),
            ):
                exit_code = cli.main(["dry-run"])

            audit_path = tmpdir / "logs" / "ha-update-audit.md"
            audit_exists = audit_path.exists()
            audit_contents = (
                audit_path.read_text(encoding="utf-8") if audit_exists else ""
            )

        self.assertEqual(exit_code, 0)
        self.assertTrue(audit_exists)
        self.assertIn("Mode: `dry-run`", audit_contents)
        self.assertIn("Dry run only. No install request was sent.", audit_contents)

    def test_cli_main_run_without_confirm_only_writes_preflight_audit(self) -> None:
        expected_calls = [
            ("GET", "/api/", {"message": "API running."}),
            (
                "GET",
                "/api/states",
                [
                    build_state(
                        "update.example_addon",
                        "on",
                        title="Example Add-on",
                        installed_version="1.0.0",
                        latest_version="1.1.0",
                    )
                ],
            ),
        ]

        with temporary_cwd() as tmpdir:
            env = {
                "HA_TOKEN": "test-token",
                "HA_BASE_URL": "https://example.ui.nabu.casa",
            }
            with (
                mock.patch.dict(os.environ, env, clear=False),
                mock.patch("hass_janitor.client.urlopen", fake_urlopen_factory(expected_calls)),
            ):
                exit_code = cli.main(["run"])

            audit_path = tmpdir / "logs" / "ha-update-audit.md"
            audit_exists = audit_path.exists()
            audit_contents = (
                audit_path.read_text(encoding="utf-8") if audit_exists else ""
            )

        self.assertEqual(exit_code, 0)
        self.assertTrue(audit_exists)
        self.assertIn("Mode: `preflight`", audit_contents)
        self.assertIn("Preflight only. Run with --confirm to install.", audit_contents)

    def test_cli_main_runs_and_writes_audit_log(self) -> None:
        expected_calls = [
            ("GET", "/api/", {"message": "API running."}),
            (
                "GET",
                "/api/states",
                [
                    build_state(
                        "update.example_addon",
                        "on",
                        title="Example Add-on",
                        installed_version="1.0.0",
                        latest_version="1.1.0",
                    )
                ],
            ),
            ("GET", "/api/", {"message": "API running."}),
            (
                "GET",
                "/api/states",
                [
                    build_state(
                        "update.example_addon",
                        "on",
                        title="Example Add-on",
                        installed_version="1.0.0",
                        latest_version="1.1.0",
                    )
                ],
            ),
            ("POST", "/api/services/update/install", []),
            (
                "GET",
                "/api/states/update.example_addon",
                build_state(
                    "update.example_addon",
                    "off",
                    title="Example Add-on",
                    installed_version="1.1.0",
                    latest_version="1.1.0",
                ),
            ),
            ("POST", "/api/services/homeassistant/restart", []),
            ("GET", "/api/", {"message": "API running."}),
            ("GET", "/api/states", []),
        ]

        with temporary_cwd() as tmpdir:
            env = {
                "HA_TOKEN": "test-token",
                "HA_BASE_URL": "https://example.ui.nabu.casa",
            }
            with (
                mock.patch.dict(os.environ, env, clear=False),
                mock.patch("hass_janitor.client.urlopen", fake_urlopen_factory(expected_calls)),
                mock.patch("hass_janitor.runner.time.sleep", lambda _: None),
                ):
                exit_code = cli.main(["run", "--confirm"])

            audit_path = tmpdir / "logs" / "ha-update-audit.md"
            audit_exists = audit_path.exists()
            audit_contents = (
                audit_path.read_text(encoding="utf-8") if audit_exists else ""
            )

        self.assertEqual(exit_code, 0)
        self.assertTrue(audit_exists)
        self.assertIn("Example Add-on", audit_contents)


class ServiceTests(TestCase):
    def test_service_health_is_unchanged(self) -> None:
        server, thread = self._start_server()
        try:
            request = Request(f"http://127.0.0.1:{server.server_port}/health", method="GET")
            with urlopen(request, timeout=2) as response:
                payload = json.loads(response.read())

            self.assertEqual(response.status, 200)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["service"], "hass-janitor")
            self.assertTrue(payload["ha_base_url_configured"])
            self.assertTrue(payload["ha_token_configured"])
            self.assertTrue(payload["api_token_configured"])
            self.assertEqual(payload["audit_path"], "logs/ha-update-audit.md")
            self.assertIsNone(payload["monitor"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_service_docs_returns_html_without_token_execution(self) -> None:
        server, thread = self._start_server()
        try:
            request = Request(f"http://127.0.0.1:{server.server_port}/docs", method="GET")
            with urlopen(request, timeout=2) as response:
                body = response.read().decode("utf-8")
                content_type = response.headers.get_content_type()

            self.assertEqual(response.status, 200)
            self.assertEqual(content_type, "text/html")
            self.assertIn("hass-janitor API docs", body)
            self.assertIn("/openapi.json", body)
            self.assertIn("/v1/home-assistant/update", body)
            self.assertNotIn("Bearer secret", body)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_service_openapi_is_valid_json_with_documented_routes(self) -> None:
        server, thread = self._start_server()
        try:
            request = Request(
                f"http://127.0.0.1:{server.server_port}/openapi.json",
                method="GET",
            )
            with urlopen(request, timeout=2) as response:
                payload = json.loads(response.read())

            self.assertEqual(response.status, 200)
            self.assertEqual(payload["openapi"], "3.1.0")
            self.assertEqual(payload["info"]["title"], "hass-janitor")
            self.assertIn("/health", payload["paths"])
            self.assertIn("/docs", payload["paths"])
            self.assertIn("/openapi.json", payload["paths"])
            self.assertIn("/v1/home-assistant/update", payload["paths"])
            self.assertEqual(
                payload["paths"]["/v1/home-assistant/update"]["post"]["security"],
                [{"bearerAuth": []}],
            )
            self.assertIn("securitySchemes", payload["components"])
            self.assertIn(
                "requestBody",
                payload["paths"]["/v1/home-assistant/update"]["post"],
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_parse_mode_requires_confirm_for_run(self) -> None:
        self.assertEqual(parse_mode({}), "preflight")
        self.assertEqual(parse_mode({"mode": "dry-run"}), "dry-run")
        with self.assertRaisesRegex(ValueError, "confirm must be true"):
            parse_mode({"mode": "run"})
        self.assertEqual(parse_mode({"mode": "run", "confirm": True}), "run")

    def test_service_rejects_missing_auth(self) -> None:
        server, thread = self._start_server()
        try:
            request = Request(
                f"http://127.0.0.1:{server.server_port}/v1/home-assistant/update",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=2)

            self.assertEqual(raised.exception.code, 401)
            raised.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_service_runs_preflight_with_auth(self) -> None:
        server, thread = self._start_server()
        summary = RunSummary(
            started_at=datetime(2026, 4, 18, 18, 0, tzinfo=timezone.utc),
            finished_at=datetime(2026, 4, 18, 18, 1, tzinfo=timezone.utc),
            base_url="https://example.ui.nabu.casa",
            mode="preflight",
            notes="No updates found.",
        )
        try:
            with mock.patch("hass_janitor.service.run_update", return_value=summary) as run_update:
                request = Request(
                    f"http://127.0.0.1:{server.server_port}/v1/home-assistant/update",
                    data=json.dumps({"mode": "preflight"}).encode("utf-8"),
                    headers={
                        "Authorization": "Bearer secret",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                with urlopen(request, timeout=2) as response:
                    payload = json.loads(response.read())

            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["mode"], "preflight")
            run_update.assert_called_once()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def _start_server(self):
        class Config:
            ha_base_url = "https://example.ui.nabu.casa"
            ha_token = "ha-token"
            api_token = "secret"
            audit_path = Path("logs/ha-update-audit.md")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.config = Config()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread


class MonitorTests(TestCase):
    def test_default_notify_function_is_loaded_lazily(self) -> None:
        with mock.patch.object(JanitorMonitor, "_load_notify_func") as load_notify_func:
            monitor = JanitorMonitor(
                self._monitor_config(),
                client_factory=lambda: MonitorFakeClient(
                    backup_state=datetime.now(timezone.utc).isoformat(),
                    initial_states=[],
                ),
            )

        load_notify_func.assert_not_called()
        self.assertTrue(callable(monitor.notify_func))

    def test_default_notify_function_failure_is_logged(self) -> None:
        monitor = JanitorMonitor(
            self._monitor_config(),
            client_factory=lambda: MonitorFakeClient(
                backup_state=datetime.now(timezone.utc).isoformat(),
                initial_states=[],
            ),
        )

        with (
            mock.patch.object(
                monitor,
                "_load_notify_func",
                side_effect=ModuleNotFoundError("No module named 'homelab'"),
            ),
            self.assertLogs("hass-janitor.monitor", level="WARNING") as logs,
        ):
            result = monitor.notify_func("Title", "Message")

        self.assertEqual(result["status"], "failed")
        self.assertIn(
            "Failed to send Home Assistant update notification",
            logs.output[0],
        )

    def test_parse_ha_datetime_handles_home_assistant_timestamp(self) -> None:
        parsed = parse_ha_datetime("2026-04-29T18:29:55.576+00:00")

        self.assertEqual(parsed.year, 2026)
        self.assertEqual(parsed.tzinfo, timezone.utc)

    def test_summarize_update_event_includes_release_fields(self) -> None:
        summary = summarize_update_event(
            "update.home_assistant_core_update",
            {
                "state": "on",
                "attributes": {
                    "title": "Home Assistant Core",
                    "installed_version": "2026.2.1",
                    "latest_version": "2026.4.4",
                    "release_summary": "Important changes.",
                    "release_url": "https://example.com/release",
                },
                "last_changed": "2026-05-02T01:57:30+00:00",
            },
        )

        self.assertEqual(summary["entity_id"], "update.home_assistant_core_update")
        self.assertEqual(summary["release_summary"], "Important changes.")
        self.assertEqual(summary["release_url"], "https://example.com/release")

    def test_update_event_signature_ignores_timestamp_only_changes(self) -> None:
        first = {
            "entity_id": "update.example",
            "state": "on",
            "installed_version": "1.0.0",
            "latest_version": "1.1.0",
            "in_progress": True,
            "release_summary": None,
            "release_url": None,
            "last_updated": "2026-05-03T02:00:00+00:00",
        }
        second = {**first, "last_updated": "2026-05-03T02:00:01+00:00"}

        self.assertEqual(update_event_signature(first), update_event_signature(second))

    def test_state_changed_ignores_duplicate_and_in_progress_events(self) -> None:
        monitor = JanitorMonitor(
            self._monitor_config(),
            client_factory=lambda: MonitorFakeClient(
                backup_state=datetime.now(timezone.utc).isoformat(),
                initial_states=[],
            ),
            notify_func=lambda title, message, **kwargs: {"status": "sent"},
        )
        calls = []
        monitor.check_once = lambda *, reason: calls.append(reason)  # type: ignore[method-assign]
        event = {
            "entity_id": "update.example",
            "new_state": {
                "state": "on",
                "attributes": {
                    "title": "Example",
                    "installed_version": "1.0.0",
                    "latest_version": "1.1.0",
                    "in_progress": True,
                },
            },
        }

        monitor.handle_state_changed(event)
        monitor.handle_state_changed(event)

        self.assertEqual(calls, [])

    def test_state_changed_checks_once_for_distinct_available_update(self) -> None:
        monitor = JanitorMonitor(
            self._monitor_config(),
            client_factory=lambda: MonitorFakeClient(
                backup_state=datetime.now(timezone.utc).isoformat(),
                initial_states=[],
            ),
            notify_func=lambda title, message, **kwargs: {"status": "sent"},
        )
        calls = []
        monitor.check_once = lambda *, reason: calls.append(reason)  # type: ignore[method-assign]
        event = {
            "entity_id": "update.example",
            "new_state": {
                "state": "on",
                "attributes": {
                    "title": "Example",
                    "installed_version": "1.0.0",
                    "latest_version": "1.1.0",
                    "in_progress": False,
                },
            },
        }

        monitor.handle_state_changed(event)
        monitor.handle_state_changed(event)

        self.assertEqual(calls, ["state_changed:update.example"])

    def test_notification_action_is_recorded_even_without_pending_confirmation(self) -> None:
        recorded: list[dict[str, Any]] = []
        monitor = JanitorMonitor(
            self._monitor_config(),
            client_factory=lambda: MonitorFakeClient(
                backup_state=datetime.now(timezone.utc).isoformat(),
                initial_states=[],
            ),
            notify_func=lambda title, message, **kwargs: {"status": "sent"},
            record_action_func=lambda action, **kwargs: recorded.append(
                {"action": action, **kwargs}
            )
            or {"status": "recorded"},
        )

        monitor.handle_notification_action(
            {
                "action": "HASS_JANITOR_CONFIRM_UPDATE",
                "tag": "hass-janitor-update-confirm",
                "group": "hass-janitor",
                "reply_text": "run it",
                "sourceDeviceName": "Pixel",
            }
        )

        self.assertEqual(
            recorded,
            [
                {
                    "action": "HASS_JANITOR_CONFIRM_UPDATE",
                    "tag": "hass-janitor-update-confirm",
                    "group": "hass-janitor",
                    "reply_text": "run it",
                    "event": {
                        "action": "HASS_JANITOR_CONFIRM_UPDATE",
                        "tag": "hass-janitor-update-confirm",
                        "group": "hass-janitor",
                        "reply_text": "run it",
                        "sourceDeviceName": "Pixel",
                    },
                }
            ],
        )

    def test_process_ledger_action_runs_matching_update_confirmation(self) -> None:
        update_state = build_state(
            "update.home_assistant_core_update",
            "on",
            title="Home Assistant Core",
            installed_version="2026.5.4",
            latest_version="2026.6.4",
        )
        token = confirmation_action_token("confirm:Home Assistant Core: 2026.5.4 -> 2026.6.4")
        client = MonitorFakeClient(
            backup_state=datetime.now(timezone.utc).isoformat(),
            initial_states=[update_state],
        )
        notifications: list[dict[str, Any]] = []
        preflight_summary = RunSummary(
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            base_url="https://example.ui.nabu.casa",
            mode="preflight",
            discovered_count=1,
            updates=[
                UpdateAttempt(
                    entity_id="update.home_assistant_core_update",
                    name="Home Assistant Core",
                    from_version="2026.5.4",
                    to_version="2026.6.4",
                    result="planned",
                    started_at=datetime.now(timezone.utc),
                    finished_at=datetime.now(timezone.utc),
                    notes="Preflight only.",
                )
            ],
        )
        run_summary = RunSummary(
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            base_url="https://example.ui.nabu.casa",
            mode="run",
            discovered_count=1,
            attempted_count=1,
            succeeded_count=1,
            notes="Update run completed.",
        )
        runner_instances = []

        class FakeRunner:
            def __init__(self, *_args, **_kwargs):
                runner_instances.append(self)

            def preflight(self):
                return preflight_summary

            def run(self):
                return run_summary

        ledger = {
            "notifications": [
                {
                    "id": 138,
                    "tag": "hass-janitor-update-confirm",
                    "group": "hass-janitor",
                    "actions": [
                        {
                            "id": 99,
                            "created_at": datetime.now(timezone.utc).isoformat(),
                            "action": f"HASS_JANITOR_CONFIRM_UPDATE::{token}",
                            "tag": "hass-janitor-update-confirm",
                            "group": "hass-janitor",
                            "event": {"sourceDeviceName": "Pixel"},
                        }
                    ],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            monitor = JanitorMonitor(
                MonitorConfig(
                    **{
                        **self._monitor_config().__dict__,
                        "action_state_path": Path(tmpdir) / "actions.json",
                    }
                ),
                client_factory=lambda: client,
                notify_func=lambda title, message, **kwargs: notifications.append(
                    {"title": title, "message": message, **kwargs}
                )
                or {"status": "sent"},
                list_notifications_func=lambda **kwargs: ledger,
            )

            with mock.patch("hass_janitor.monitor.UpdateRunner", FakeRunner):
                monitor.process_ledger_actions()

        self.assertEqual(notifications[0]["title"], "Home Assistant update finished")
        self.assertIn(99, monitor._processed_action_ids)

    def test_process_ledger_action_ignores_duplicate_after_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            action_state_path = Path(tmpdir) / "actions.json"
            action_state_path.write_text('{"processed_action_ids":[99]}', encoding="utf-8")
            monitor = JanitorMonitor(
                MonitorConfig(
                    **{
                        **self._monitor_config().__dict__,
                        "action_state_path": action_state_path,
                    }
                ),
                client_factory=lambda: MonitorFakeClient(
                    backup_state=datetime.now(timezone.utc).isoformat(),
                    initial_states=[],
                ),
                list_notifications_func=lambda **kwargs: {
                    "notifications": [
                        {
                            "actions": [
                                {
                                    "id": 99,
                                    "created_at": datetime.now(timezone.utc).isoformat(),
                                    "action": "HASS_JANITOR_CONFIRM_UPDATE::token",
                                }
                            ]
                        }
                    ]
                },
            )

            with mock.patch("hass_janitor.monitor.UpdateRunner") as runner:
                monitor.process_ledger_actions()

        runner.assert_not_called()

    def test_process_ledger_action_blocks_run_when_backup_is_stale(self) -> None:
        update_state = build_state(
            "update.home_assistant_core_update",
            "on",
            title="Home Assistant Core",
            installed_version="2026.5.4",
            latest_version="2026.6.4",
        )
        token = confirmation_action_token("confirm:Home Assistant Core: 2026.5.4 -> 2026.6.4")
        client = MonitorFakeClient(
            backup_state="2026-05-01T04:00:00+00:00",
            initial_states=[update_state],
        )
        notifications: list[dict[str, Any]] = []
        preflight_summary = RunSummary(
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            base_url="https://example.ui.nabu.casa",
            mode="preflight",
            discovered_count=1,
            updates=[
                UpdateAttempt(
                    entity_id="update.home_assistant_core_update",
                    name="Home Assistant Core",
                    from_version="2026.5.4",
                    to_version="2026.6.4",
                    result="planned",
                    started_at=datetime.now(timezone.utc),
                    finished_at=datetime.now(timezone.utc),
                    notes="Preflight only.",
                )
            ],
        )

        class FakeRunner:
            def __init__(self, *_args, **_kwargs):
                pass

            def preflight(self):
                return preflight_summary

            def run(self):
                raise AssertionError("stale backup should block run")

        ledger = {
            "notifications": [
                {
                    "actions": [
                        {
                            "id": 100,
                            "created_at": datetime.now(timezone.utc).isoformat(),
                            "action": f"{CONFIRM_ACTION}::{token}",
                        }
                    ]
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            monitor = JanitorMonitor(
                MonitorConfig(
                    **{
                        **self._monitor_config().__dict__,
                        "action_state_path": Path(tmpdir) / "actions.json",
                    }
                ),
                client_factory=lambda: client,
                notify_func=lambda title, message, **kwargs: notifications.append(
                    {"title": title, "message": message, **kwargs}
                )
                or {"status": "sent"},
                list_notifications_func=lambda **kwargs: ledger,
            )

            with mock.patch("hass_janitor.monitor.UpdateRunner", FakeRunner):
                monitor.process_ledger_actions()

        self.assertEqual(notifications[0]["title"], "Home Assistant update blocked")
        self.assertIn(100, monitor._processed_action_ids)

    def test_process_ledger_snooze_and_dismiss_actions_are_persisted(self) -> None:
        update_state = build_state(
            "update.home_assistant_core_update",
            "on",
            title="Home Assistant Core",
            installed_version="2026.5.4",
            latest_version="2026.6.4",
        )
        token = confirmation_action_token("confirm:Home Assistant Core: 2026.5.4 -> 2026.6.4")
        preflight_summary = RunSummary(
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            base_url="https://example.ui.nabu.casa",
            mode="preflight",
            discovered_count=1,
            updates=[
                UpdateAttempt(
                    entity_id="update.home_assistant_core_update",
                    name="Home Assistant Core",
                    from_version="2026.5.4",
                    to_version="2026.6.4",
                    result="planned",
                    started_at=datetime.now(timezone.utc),
                    finished_at=datetime.now(timezone.utc),
                    notes="Preflight only.",
                )
            ],
        )

        class FakeRunner:
            def __init__(self, *_args, **_kwargs):
                pass

            def preflight(self):
                return preflight_summary

            def run(self):
                raise AssertionError("snooze/dismiss should not run updates")

        for action_id, action_name in ((101, SNOOZE_ACTION), (102, DISMISS_ACTION)):
            with self.subTest(action=action_name), tempfile.TemporaryDirectory() as tmpdir:
                ledger = {
                    "notifications": [
                        {
                            "actions": [
                                {
                                    "id": action_id,
                                    "created_at": datetime.now(timezone.utc).isoformat(),
                                    "action": f"{action_name}::{token}",
                                }
                            ]
                        }
                    ]
                }
                monitor = JanitorMonitor(
                    MonitorConfig(
                        **{
                            **self._monitor_config().__dict__,
                            "action_state_path": Path(tmpdir) / "actions.json",
                        }
                    ),
                    client_factory=lambda: MonitorFakeClient(
                        backup_state=datetime.now(timezone.utc).isoformat(),
                        initial_states=[update_state],
                    ),
                    list_notifications_func=lambda **kwargs: ledger,
                )
                monitor._pending_confirmation = True

                with mock.patch("hass_janitor.monitor.UpdateRunner", FakeRunner):
                    monitor.process_ledger_actions()

                self.assertFalse(monitor._pending_confirmation)
                self.assertIn(action_id, monitor._processed_action_ids)

    def test_check_once_blocks_when_backup_is_stale(self) -> None:
        notifications: list[dict[str, Any]] = []
        client = MonitorFakeClient(
            backup_state="2026-04-01T18:29:55.576+00:00",
            initial_states=[
                build_state(
                    "update.home_assistant_core_update",
                    "on",
                    title="Home Assistant Core",
                    installed_version="2026.2.1",
                    latest_version="2026.4.4",
                )
            ],
        )
        monitor = JanitorMonitor(
            self._monitor_config(),
            client_factory=lambda: client,
            notify_func=lambda title, message, **kwargs: notifications.append(
                {"title": title, "message": message, **kwargs}
            )
            or {"status": "sent"},
            list_notifications_func=lambda **kwargs: {"notifications": []},
        )

        summary = monitor.check_once(reason="test")

        self.assertEqual(summary.discovered_count, 1)
        self.assertEqual(notifications[0]["title"], "Home Assistant update blocked")
        self.assertIn("no fresh backup", notifications[0]["message"])

    def test_check_once_sends_confirmation_when_backup_is_fresh(self) -> None:
        notifications: list[dict[str, Any]] = []
        client = MonitorFakeClient(
            backup_state=datetime.now(timezone.utc).isoformat(),
            initial_states=[
                build_state(
                    "update.home_assistant_core_update",
                    "on",
                    title="Home Assistant Core",
                    installed_version="2026.2.1",
                    latest_version="2026.4.4",
                )
            ],
        )
        monitor = JanitorMonitor(
            self._monitor_config(),
            client_factory=lambda: client,
            notify_func=lambda title, message, **kwargs: notifications.append(
                {"title": title, "message": message, **kwargs}
            )
            or {"status": "sent"},
            list_notifications_func=lambda **kwargs: {"notifications": []},
        )

        summary = monitor.check_once(reason="test")

        self.assertEqual(summary.discovered_count, 1)
        self.assertEqual(notifications[0]["title"], "Home Assistant updates available")
        self.assertEqual(notifications[0]["buttons"][0]["title"], "Update now")
        self.assertEqual(notifications[0]["buttons"][1]["title"], "Snooze 24h")
        self.assertEqual(notifications[0]["buttons"][2]["title"], "Dismiss this version")
        self.assertTrue(notifications[0]["buttons"][0]["action"].startswith("HASS_JANITOR_CONFIRM_UPDATE::"))

    def test_check_once_skips_confirmation_when_fingerprint_is_snoozed(self) -> None:
        notifications: list[dict[str, Any]] = []
        update_state = build_state(
            "update.home_assistant_core_update",
            "on",
            title="Home Assistant Core",
            installed_version="2026.2.1",
            latest_version="2026.4.4",
        )
        update_line = "Home Assistant Core: 2026.2.1 -> 2026.4.4"
        token = confirmation_action_token(f"confirm:{update_line}")
        client = MonitorFakeClient(
            backup_state=datetime.now(timezone.utc).isoformat(),
            initial_states=[update_state],
        )
        monitor = JanitorMonitor(
            self._monitor_config(),
            client_factory=lambda: client,
            notify_func=lambda title, message, **kwargs: notifications.append(
                {"title": title, "message": message, **kwargs}
            )
            or {"status": "sent"},
            list_notifications_func=lambda **kwargs: {
                "notifications": [
                    {
                        "actions": [
                            {
                                "action": f"{SNOOZE_ACTION}::{token}",
                                "created_at": datetime.now(timezone.utc).isoformat(),
                            }
                        ]
                    }
                ]
            },
        )

        summary = monitor.check_once(reason="test")

        self.assertEqual(summary.discovered_count, 1)
        self.assertEqual(notifications, [])

    def test_check_once_skips_confirmation_when_fingerprint_is_dismissed(self) -> None:
        notifications: list[dict[str, Any]] = []
        update_state = build_state(
            "update.home_assistant_core_update",
            "on",
            title="Home Assistant Core",
            installed_version="2026.2.1",
            latest_version="2026.4.4",
        )
        update_line = "Home Assistant Core: 2026.2.1 -> 2026.4.4"
        token = confirmation_action_token(f"confirm:{update_line}")
        client = MonitorFakeClient(
            backup_state=datetime.now(timezone.utc).isoformat(),
            initial_states=[update_state],
        )
        monitor = JanitorMonitor(
            self._monitor_config(),
            client_factory=lambda: client,
            notify_func=lambda title, message, **kwargs: notifications.append(
                {"title": title, "message": message, **kwargs}
            )
            or {"status": "sent"},
            list_notifications_func=lambda **kwargs: {
                "notifications": [
                    {
                        "actions": [
                            {
                                "action": f"{DISMISS_ACTION}::{token}",
                                "created_at": "2026-01-01T00:00:00+00:00",
                            }
                        ]
                    }
                ]
            },
        )

        summary = monitor.check_once(reason="test")

        self.assertEqual(summary.discovered_count, 1)
        self.assertEqual(notifications, [])

    def test_backup_status_uses_configured_timestamp_attribute(self) -> None:
        client = MonitorFakeClient(
            backup_state="backed_up",
            backup_attributes={
                "last_backup": datetime.now(timezone.utc).isoformat(),
            },
            initial_states=[],
        )
        config = MonitorConfig(
            ha_base_url="https://example.ui.nabu.casa",
            ha_token="ha-token",
            audit_path=Path(os.devnull),
            backup_entity_id="sensor.backup_state",
            backup_max_age_days=7,
            check_interval_seconds=86400,
            notification_cooldown_seconds=21600,
            backup_timestamp_attribute="last_backup",
        )
        monitor = JanitorMonitor(
            config,
            client_factory=lambda: client,
            notify_func=lambda title, message, **kwargs: {"status": "sent"},
        )

        status = monitor.backup_status(client)

        self.assertTrue(status.fresh)
        self.assertEqual(status.state, client.backup_attributes["last_backup"])
        self.assertEqual(status.reason, "Backup is fresh.")

    def _monitor_config(self) -> MonitorConfig:
        return MonitorConfig(
            ha_base_url="https://example.ui.nabu.casa",
            ha_token="ha-token",
            audit_path=Path(os.devnull),
            backup_entity_id="event.backup_automatic_backup",
            backup_max_age_days=7,
            check_interval_seconds=86400,
            notification_cooldown_seconds=21600,
        )


class ListenerRefactorTests(IsolatedAsyncioTestCase):
    async def test_listen_once_uses_shared_home_assistant_client(self) -> None:
        class ExpectedDisconnect(RuntimeError):
            pass

        clients = []
        state_events: list[dict[str, Any]] = []
        action_events: list[dict[str, Any]] = []

        class FakeHomeAssistantWebSocketClient:
            def __init__(self, config):
                self.config = config
                self.handlers = []
                self.subscriptions: list[str] = []
                clients.append(self)

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def add_event_handler(self, handler):
                self.handlers.append(handler)

            async def subscribe_events(self, event_type):
                self.subscriptions.append(event_type)

            async def wait_closed(self):
                await self.handlers[0](
                    {
                        "event_type": "state_changed",
                        "data": {"entity_id": "update.example", "new_state": {"state": "off"}},
                    }
                )
                await self.handlers[0](
                    {
                        "event_type": "mobile_app_notification_action",
                        "data": {"action": "HASS_JANITOR_CANARY::token"},
                    }
                )
                raise ExpectedDisconnect("closed")

        class FakeHomeAssistantConfig:
            def __init__(self, *, ha_url, ha_long_lived_token):
                self.ha_url = ha_url
                self.ha_long_lived_token = ha_long_lived_token

        monitor = JanitorMonitor(self._monitor_config())
        monitor.handle_state_changed = state_events.append  # type: ignore[method-assign]
        monitor.handle_notification_action = action_events.append  # type: ignore[method-assign]

        with mock.patch.object(
            monitor,
            "_load_home_assistant_websocket_client",
            return_value=(FakeHomeAssistantConfig, FakeHomeAssistantWebSocketClient),
        ):
            with self.assertRaises(ExpectedDisconnect):
                await monitor._listen_once()

        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0].config.ha_url, "https://example.ui.nabu.casa")
        self.assertEqual(clients[0].config.ha_long_lived_token, "ha-token")
        self.assertEqual(
            clients[0].subscriptions,
            ["state_changed", "mobile_app_notification_action"],
        )
        self.assertTrue(monitor._ha_listener_connected)
        self.assertEqual(monitor._ha_listener_last_error, "")
        self.assertEqual(
            state_events,
            [{"entity_id": "update.example", "new_state": {"state": "off"}}],
        )
        self.assertEqual(action_events, [{"action": "HASS_JANITOR_CANARY::token"}])

    def _monitor_config(self) -> MonitorConfig:
        return MonitorConfig(
            ha_base_url="https://example.ui.nabu.casa",
            ha_token="ha-token",
            audit_path=Path(os.devnull),
            backup_entity_id="event.backup_automatic_backup",
            backup_max_age_days=7,
            check_interval_seconds=86400,
            notification_cooldown_seconds=21600,
        )
