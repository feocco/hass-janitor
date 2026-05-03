"""Background Home Assistant update monitor."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
import threading
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from .audit import append_audit_log
from .client import HAClientError, HomeAssistantClient
from .models import RunSummary
from .runner import UpdateRunner


LOGGER = logging.getLogger("hass-janitor.monitor")
CONFIRM_ACTION = "HASS_JANITOR_CONFIRM_UPDATE"
OPEN_UPDATES_ACTION = "URI"


@dataclass(frozen=True)
class MonitorConfig:
    ha_base_url: str
    ha_token: str
    audit_path: Any
    backup_entity_id: str
    backup_max_age_days: int
    check_interval_seconds: int
    notification_cooldown_seconds: int


@dataclass(frozen=True)
class BackupStatus:
    fresh: bool
    entity_id: str
    state: str
    checked_at: datetime
    age_days: float | None = None
    reason: str = ""


class JanitorMonitor:
    def __init__(
        self,
        config: MonitorConfig,
        *,
        client_factory: Callable[[], HomeAssistantClient] | None = None,
        notify_func: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.config = config
        self.client_factory = client_factory or (
            lambda: HomeAssistantClient(
                base_url=config.ha_base_url,
                token=config.ha_token,
            )
        )
        self.notify_func = notify_func or self._load_notify_func()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_signature = ""
        self._last_notification_at: datetime | None = None
        self._pending_confirmation = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def check_once(self, *, reason: str) -> RunSummary:
        client = self.client_factory()
        summary = UpdateRunner(client, base_url=self.config.ha_base_url).preflight()
        if summary.exit_code != 0 or summary.discovered_count == 0:
            LOGGER.info(
                "Update check completed",
                extra={
                    "reason": reason,
                    "exit_code": summary.exit_code,
                    "discovered_count": summary.discovered_count,
                    "notes": summary.notes,
                },
            )
            return summary

        backup = self.backup_status(client)
        append_audit_log(self.config.audit_path, summary)

        if not backup.fresh:
            self._notify_stale_backup(summary, backup, reason=reason)
            return summary

        self._notify_confirmation(summary, backup, reason=reason)
        return summary

    def backup_status(self, client: HomeAssistantClient | None = None) -> BackupStatus:
        checked_at = datetime.now(timezone.utc)
        client = client or self.client_factory()
        try:
            state = client.get_state(self.config.backup_entity_id)
        except HAClientError as exc:
            return BackupStatus(
                fresh=False,
                entity_id=self.config.backup_entity_id,
                state="unavailable",
                checked_at=checked_at,
                reason=f"Could not read backup entity: {exc}",
            )

        raw_state = str(state.get("state") or "").strip()
        try:
            backup_time = parse_ha_datetime(raw_state)
        except ValueError:
            return BackupStatus(
                fresh=False,
                entity_id=self.config.backup_entity_id,
                state=raw_state,
                checked_at=checked_at,
                reason="Backup timestamp was missing or invalid.",
            )

        age = checked_at - backup_time
        age_days = age.total_seconds() / 86400
        if age <= timedelta(days=self.config.backup_max_age_days):
            return BackupStatus(
                fresh=True,
                entity_id=self.config.backup_entity_id,
                state=raw_state,
                checked_at=checked_at,
                age_days=age_days,
                reason="Backup is fresh.",
            )

        return BackupStatus(
            fresh=False,
            entity_id=self.config.backup_entity_id,
            state=raw_state,
            checked_at=checked_at,
            age_days=age_days,
            reason=f"Latest backup is {age_days:.1f} days old.",
        )

    async def listen_forever(self) -> None:
        import aiohttp

        while not self._stop_event.is_set():
            try:
                await self._listen_once(aiohttp)
            except Exception as exc:
                LOGGER.warning("HA event listener disconnected: %s", exc)
                await asyncio.sleep(10)

    async def periodic_check_forever(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.check_once(reason="periodic")
            except Exception as exc:
                LOGGER.warning("Periodic update check failed: %s", exc)
            await asyncio.sleep(self.config.check_interval_seconds)

    async def _listen_once(self, aiohttp_module) -> None:
        ws_url = websocket_url(self.config.ha_base_url)
        async with aiohttp_module.ClientSession() as session:
            async with session.ws_connect(ws_url) as ws:
                auth_required = await ws.receive_json()
                if auth_required.get("type") != "auth_required":
                    raise RuntimeError("Home Assistant did not request WebSocket auth")

                await ws.send_json(
                    {
                        "type": "auth",
                        "access_token": self.config.ha_token,
                    }
                )
                auth_response = await ws.receive_json()
                if auth_response.get("type") != "auth_ok":
                    raise RuntimeError(f"Home Assistant auth failed: {auth_response}")

                await ws.send_json(
                    {
                        "id": 1,
                        "type": "subscribe_events",
                        "event_type": "state_changed",
                    }
                )
                await ws.send_json(
                    {
                        "id": 2,
                        "type": "subscribe_events",
                        "event_type": "mobile_app_notification_action",
                    }
                )
                LOGGER.info("Subscribed to Home Assistant update and notification events")

                async for message in ws:
                    if message.type != aiohttp_module.WSMsgType.TEXT:
                        continue
                    payload = message.json()
                    event = payload.get("event") or {}
                    event_type = event.get("event_type")
                    data = event.get("data") or {}
                    if event_type == "state_changed":
                        self.handle_state_changed(data)
                    elif event_type == "mobile_app_notification_action":
                        self.handle_notification_action(data)

    def handle_state_changed(self, data: dict[str, Any]) -> None:
        entity_id = str(data.get("entity_id") or "")
        new_state = data.get("new_state") or {}
        if not entity_id.startswith("update."):
            return

        log_payload = summarize_update_event(entity_id, new_state)
        LOGGER.info("Home Assistant update state_changed: %s", json.dumps(log_payload, sort_keys=True))

        if new_state.get("state") != "on":
            return

        try:
            self.check_once(reason=f"state_changed:{entity_id}")
        except Exception as exc:
            LOGGER.warning("Update check failed after %s changed: %s", entity_id, exc)

    def handle_notification_action(self, data: dict[str, Any]) -> None:
        action = data.get("action")
        LOGGER.info("Mobile notification action: %s", json.dumps(data, sort_keys=True, default=str))
        if action != CONFIRM_ACTION or not self._pending_confirmation:
            return

        client = self.client_factory()
        backup = self.backup_status(client)
        if not backup.fresh:
            summary = UpdateRunner(client, base_url=self.config.ha_base_url).preflight()
            self._notify_stale_backup(summary, backup, reason="confirmation")
            return

        summary = UpdateRunner(client, base_url=self.config.ha_base_url).run()
        append_audit_log(self.config.audit_path, summary)
        self._pending_confirmation = False
        self.notify_func(
            "Home Assistant update finished",
            (
                f"Processed {summary.attempted_count}/{summary.discovered_count}. "
                f"Succeeded: {summary.succeeded_count}, failed: {summary.failed_count}, "
                f"timed out: {summary.timed_out_count}, restart: {summary.restart.result}."
            ),
            tag="hass-janitor-update-finished",
            group="hass-janitor",
        )

    def _notify_stale_backup(
        self,
        summary: RunSummary,
        backup: BackupStatus,
        *,
        reason: str,
    ) -> None:
        signature = f"stale:{summary.discovered_count}:{backup.state}:{backup.reason}"
        if not self._should_notify(signature):
            return
        self.notify_func(
            "Home Assistant update blocked",
            (
                f"{summary.discovered_count} update(s) are available, but no fresh backup "
                f"was confirmed. {backup.reason}"
            ),
            tag="hass-janitor-backup-stale",
            group="hass-janitor",
            url="/config/updates",
        )
        LOGGER.info("Blocked update notification sent for reason=%s backup=%s", reason, backup)

    def _notify_confirmation(
        self,
        summary: RunSummary,
        backup: BackupStatus,
        *,
        reason: str,
    ) -> None:
        update_lines = [
            f"{attempt.name}: {attempt.from_version} -> {attempt.to_version}"
            for attempt in summary.updates[:4]
        ]
        extra = "" if len(summary.updates) <= 4 else f" +{len(summary.updates) - 4} more"
        signature = "confirm:" + "|".join(update_lines) + extra
        if not self._should_notify(signature):
            return

        self._pending_confirmation = True
        backup_age = (
            "unknown age"
            if backup.age_days is None
            else f"{backup.age_days:.1f} days old"
        )
        self.notify_func(
            "Home Assistant updates available",
            (
                f"{summary.discovered_count} update(s): "
                + "; ".join(update_lines)
                + extra
                + f". Backup is {backup_age}."
            ),
            tag="hass-janitor-update-confirm",
            group="hass-janitor",
            url="/config/updates",
            buttons=[
                {"title": "Update now", "action": CONFIRM_ACTION},
                {"title": "Open updates", "action": OPEN_UPDATES_ACTION, "uri": "/config/updates"},
            ],
        )
        LOGGER.info("Update confirmation notification sent for reason=%s", reason)

    def _should_notify(self, signature: str) -> bool:
        now = datetime.now(timezone.utc)
        if self._last_signature != signature:
            self._last_signature = signature
            self._last_notification_at = now
            return True
        if self._last_notification_at is None:
            self._last_notification_at = now
            return True
        if now - self._last_notification_at >= timedelta(
            seconds=self.config.notification_cooldown_seconds
        ):
            self._last_notification_at = now
            return True
        return False

    def _thread_main(self) -> None:
        asyncio.run(self._async_main())

    async def _async_main(self) -> None:
        await asyncio.gather(
            self.listen_forever(),
            self.periodic_check_forever(),
        )

    @staticmethod
    def _load_notify_func():
        from homelab import notify_joe

        return notify_joe


def parse_ha_datetime(value: str) -> datetime:
    if value in {"", "unknown", "unavailable", "none"}:
        raise ValueError("empty timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def websocket_url(ha_url: str) -> str:
    parsed = urlsplit(ha_url.rstrip("/"))
    if parsed.scheme == "https":
        scheme = "wss"
    elif parsed.scheme == "http":
        scheme = "ws"
    elif parsed.scheme in ("ws", "wss"):
        scheme = parsed.scheme
    else:
        raise ValueError("HA URL must start with http://, https://, ws://, or wss://")

    path = parsed.path.rstrip("/")
    if not path.endswith("/api/websocket"):
        path = f"{path}/api/websocket"
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


def summarize_update_event(entity_id: str, new_state: dict[str, Any]) -> dict[str, Any]:
    attrs = new_state.get("attributes") or {}
    return {
        "entity_id": entity_id,
        "state": new_state.get("state"),
        "title": attrs.get("title") or attrs.get("friendly_name"),
        "installed_version": attrs.get("installed_version"),
        "latest_version": attrs.get("latest_version"),
        "auto_update": attrs.get("auto_update"),
        "in_progress": attrs.get("in_progress"),
        "release_summary": attrs.get("release_summary"),
        "release_url": attrs.get("release_url"),
        "last_changed": new_state.get("last_changed"),
        "last_updated": new_state.get("last_updated"),
    }
