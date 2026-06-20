"""Background Home Assistant update monitor."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from pathlib import Path
import threading
from typing import Any, Callable

from .audit import append_audit_log
from .client import HAClientError, HomeAssistantClient
from .models import RunSummary
from .runner import UpdateRunner


LOGGER = logging.getLogger("hass-janitor.monitor")
CONFIRM_ACTION = "HASS_JANITOR_CONFIRM_UPDATE"
SNOOZE_ACTION = "HASS_JANITOR_SNOOZE_UPDATE"
DISMISS_ACTION = "HASS_JANITOR_DISMISS_UPDATE"
UPDATE_CONFIRM_TAG = "hass-janitor-update-confirm"
UPDATE_GROUP = "hass-janitor"
SNOOZE_DURATION = timedelta(hours=24)


@dataclass(frozen=True)
class MonitorConfig:
    ha_base_url: str
    ha_token: str
    audit_path: Any
    backup_entity_id: str
    backup_max_age_days: int
    check_interval_seconds: int
    notification_cooldown_seconds: int
    backup_timestamp_attribute: str = ""
    ledger_action_poll_seconds: int = 30
    action_state_path: Any | None = None


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
        record_action_func: Callable[..., dict[str, Any]] | None = None,
        list_notifications_func: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.config = config
        self.client_factory = client_factory or (
            lambda: HomeAssistantClient(
                base_url=config.ha_base_url,
                token=config.ha_token,
            )
        )
        self.notify_func = notify_func or self._notify_via_homelab
        self._loaded_notify_func: Callable[..., dict[str, Any]] | None = None
        self.record_action_func = record_action_func or self._record_action_via_homelab
        self._loaded_record_action_func: Callable[..., dict[str, Any]] | None = None
        self.list_notifications_func = list_notifications_func or self._list_notifications_via_homelab
        self._loaded_list_notifications_func: Callable[..., dict[str, Any]] | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_signature = ""
        self._last_notification_at: datetime | None = None
        self._pending_confirmation = False
        self._last_event_signature_by_entity: dict[str, str] = {}
        self._action_state_path = (
            Path(config.action_state_path)
            if config.action_state_path is not None
            else Path(config.audit_path).parent / "processed-notification-actions.json"
        )
        self._processed_action_ids = self._load_processed_action_ids()
        self._current_confirmation_token = ""
        self._last_notification_sent_at: datetime | None = None
        self._last_notification_id: int | None = None
        self._last_ledger_action_seen_id: int | None = None
        self._last_processed_action_id: int | None = None
        self._ha_listener_connected = False
        self._ha_listener_last_error = ""

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

        raw_state = self._backup_timestamp(state)
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

    def _backup_timestamp(self, state: dict[str, Any]) -> str:
        attribute_name = self.config.backup_timestamp_attribute.strip()
        if not attribute_name:
            return str(state.get("state") or "").strip()
        attributes = state.get("attributes") or {}
        return str(attributes.get(attribute_name) or "").strip()

    async def listen_forever(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._listen_once()
            except Exception as exc:
                self._ha_listener_connected = False
                self._ha_listener_last_error = str(exc)
                LOGGER.warning("HA event listener disconnected: %s", exc)
                await asyncio.sleep(10)

    async def periodic_check_forever(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.check_once(reason="periodic")
            except Exception as exc:
                LOGGER.warning("Periodic update check failed: %s", exc)
            await asyncio.sleep(self.config.check_interval_seconds)

    async def ledger_action_check_forever(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.process_ledger_actions()
            except Exception as exc:
                LOGGER.warning("Ledger notification action check failed: %s", exc)
            await asyncio.sleep(self.config.ledger_action_poll_seconds)

    async def _listen_once(self) -> None:
        HomeAssistantConfig, HomeAssistantWebSocketClient = self._load_home_assistant_websocket_client()
        config = HomeAssistantConfig(
            ha_url=self.config.ha_base_url,
            ha_long_lived_token=self.config.ha_token,
        )
        async with HomeAssistantWebSocketClient(config) as ha:
            ha.add_event_handler(self._handle_home_assistant_event)
            await ha.subscribe_events("state_changed")
            await ha.subscribe_events("mobile_app_notification_action")
            LOGGER.info("Subscribed to Home Assistant update and notification events")
            self._ha_listener_connected = True
            self._ha_listener_last_error = ""
            await ha.wait_closed()

    @staticmethod
    def _load_home_assistant_websocket_client():
        from homelab import HomeAssistantConfig, HomeAssistantWebSocketClient

        return HomeAssistantConfig, HomeAssistantWebSocketClient

    async def _handle_home_assistant_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("event_type")
        data = event.get("data") or {}
        if not isinstance(data, dict):
            return
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
        event_signature = update_event_signature(log_payload)
        if self._last_event_signature_by_entity.get(entity_id) == event_signature:
            LOGGER.debug(
                "Duplicate Home Assistant update state_changed signature for %s: %s",
                entity_id,
                event_signature,
            )
            return
        self._last_event_signature_by_entity[entity_id] = event_signature
        LOGGER.info("Home Assistant update state_changed: %s", json.dumps(log_payload, sort_keys=True))

        if new_state.get("state") != "on":
            return
        if (new_state.get("attributes") or {}).get("in_progress"):
            LOGGER.info("Skipping update prompt while %s is already in progress", entity_id)
            return

        try:
            self.check_once(reason=f"state_changed:{entity_id}")
        except Exception as exc:
            LOGGER.warning("Update check failed after %s changed: %s", entity_id, exc)

    def handle_notification_action(self, data: dict[str, Any]) -> None:
        action = data.get("action")
        LOGGER.info("Mobile notification action: %s", json.dumps(data, sort_keys=True, default=str))
        record_result = self._record_notification_action(data)
        action_name, _token = split_action_token(str(action or ""))
        if action_name in {SNOOZE_ACTION, DISMISS_ACTION}:
            self._pending_confirmation = False
            self._mark_recorded_action_processed(record_result)
            return
        if action_name != CONFIRM_ACTION or not self._pending_confirmation:
            return

        client = self.client_factory()
        backup = self.backup_status(client)
        if not backup.fresh:
            summary = UpdateRunner(client, base_url=self.config.ha_base_url).preflight()
            self._notify_stale_backup(summary, backup, reason="confirmation")
            self._mark_recorded_action_processed(record_result)
            return

        summary = UpdateRunner(client, base_url=self.config.ha_base_url).run()
        append_audit_log(self.config.audit_path, summary)
        self._pending_confirmation = False
        self._mark_recorded_action_processed(record_result)
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

    def process_ledger_actions(self) -> None:
        history = self.list_notifications_func(
            group=UPDATE_GROUP,
            tag=UPDATE_CONFIRM_TAG,
            limit=100,
        )
        notifications = history.get("notifications")
        if not isinstance(notifications, list):
            return

        actions: list[dict[str, Any]] = []
        for notification in notifications:
            if not isinstance(notification, dict):
                continue
            notification_actions = notification.get("actions")
            if not isinstance(notification_actions, list):
                continue
            for action_event in notification_actions:
                if isinstance(action_event, dict):
                    actions.append(action_event)

        for action_event in sorted(actions, key=lambda item: int(item.get("id") or 0)):
            action_id = self._action_id(action_event)
            if action_id is None:
                continue
            self._last_ledger_action_seen_id = action_id
            if action_id in self._processed_action_ids:
                continue

            action_name, action_token = split_action_token(str(action_event.get("action") or ""))
            if not action_name.startswith("HASS_JANITOR_"):
                continue
            if action_name not in {CONFIRM_ACTION, SNOOZE_ACTION, DISMISS_ACTION}:
                self._mark_action_processed(action_id)
                continue

            client = self.client_factory()
            runner = UpdateRunner(client, base_url=self.config.ha_base_url)
            preflight = runner.preflight()
            if preflight.exit_code != 0 or preflight.discovered_count == 0:
                self._mark_action_processed(action_id)
                continue

            token = confirmation_action_token(confirmation_signature(preflight))
            self._current_confirmation_token = token
            if action_token != token:
                LOGGER.info(
                    "Ignoring stale notification action id=%s token=%s current=%s",
                    action_id,
                    action_token,
                    token,
                )
                self._mark_action_processed(action_id)
                continue

            if action_name in {SNOOZE_ACTION, DISMISS_ACTION}:
                self._pending_confirmation = False
                self._mark_action_processed(action_id)
                continue

            append_audit_log(self.config.audit_path, preflight)
            backup = self.backup_status(client)
            if not backup.fresh:
                self._notify_stale_backup(preflight, backup, reason="ledger-confirmation")
                self._mark_action_processed(action_id)
                continue

            summary = runner.run()
            append_audit_log(self.config.audit_path, summary)
            self._pending_confirmation = False
            self._mark_action_processed(action_id)
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
        signature = confirmation_signature(summary)
        if not self._should_notify(signature):
            return
        token = confirmation_action_token(signature)
        self._current_confirmation_token = token
        if self._is_confirmation_suppressed(token):
            LOGGER.info("Skipping update confirmation because action token %s is suppressed", token)
            return

        self._pending_confirmation = True
        backup_age = (
            "unknown age"
            if backup.age_days is None
            else f"{backup.age_days:.1f} days old"
        )
        result = self.notify_func(
            "Home Assistant updates available",
            (
                f"{summary.discovered_count} update(s): "
                + "; ".join(update_lines)
                + extra
                + f". Backup is {backup_age}."
            ),
            tag=UPDATE_CONFIRM_TAG,
            group=UPDATE_GROUP,
            url="/config/updates",
            buttons=[
                {"title": "Update now", "action": f"{CONFIRM_ACTION}::{token}"},
                {"title": "Snooze 24h", "action": f"{SNOOZE_ACTION}::{token}"},
                {"title": "Dismiss this version", "action": f"{DISMISS_ACTION}::{token}"},
            ],
        )
        self._last_notification_sent_at = datetime.now(timezone.utc)
        notification_id = result.get("notification_id")
        self._last_notification_id = notification_id if isinstance(notification_id, int) else None
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
            self.ledger_action_check_forever(),
        )

    @staticmethod
    def _load_notify_func():
        from homelab import notify_joe

        return notify_joe

    @staticmethod
    def _load_record_action_func():
        from homelab import record_notification_action

        return record_notification_action

    @staticmethod
    def _load_list_notifications_func():
        from homelab import list_notifications

        return list_notifications

    def _notify_via_homelab(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            if self._loaded_notify_func is None:
                self._loaded_notify_func = self._load_notify_func()
            return self._loaded_notify_func(*args, **kwargs)
        except Exception as exc:
            LOGGER.warning("Failed to send Home Assistant update notification: %s", exc)
            return {"status": "failed", "error": str(exc)}

    def _record_notification_action(self, data: dict[str, Any]) -> dict[str, Any] | None:
        action = str(data.get("action") or "").strip()
        if not action.startswith("HASS_JANITOR_"):
            return None

        reply_text = data.get("reply_text")
        return self.record_action_func(
            action,
            tag=str(data.get("tag") or UPDATE_CONFIRM_TAG).strip(),
            group=str(data.get("group") or UPDATE_GROUP).strip(),
            reply_text=reply_text if isinstance(reply_text, str) and reply_text.strip() else None,
            event=data,
        )

    def _record_action_via_homelab(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            if self._loaded_record_action_func is None:
                self._loaded_record_action_func = self._load_record_action_func()
            return self._loaded_record_action_func(*args, **kwargs)
        except Exception as exc:
            LOGGER.warning("Failed to record notification action: %s", exc)
            return {"status": "failed", "error": str(exc)}

    def _list_notifications_via_homelab(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            if self._loaded_list_notifications_func is None:
                self._loaded_list_notifications_func = self._load_list_notifications_func()
            return self._loaded_list_notifications_func(*args, **kwargs)
        except Exception as exc:
            LOGGER.warning("Failed to read notification ledger: %s", exc)
            return {"notifications": []}

    def _is_confirmation_suppressed(self, token: str) -> bool:
        history = self.list_notifications_func(
            group=UPDATE_GROUP,
            tag=UPDATE_CONFIRM_TAG,
            limit=100,
        )
        notifications = history.get("notifications")
        if not isinstance(notifications, list):
            return False

        now = datetime.now(timezone.utc)
        for notification in notifications:
            actions = notification.get("actions") if isinstance(notification, dict) else None
            if not isinstance(actions, list):
                continue
            for action_event in actions:
                if not isinstance(action_event, dict):
                    continue
                action_name, action_token = split_action_token(str(action_event.get("action") or ""))
                if action_token != token:
                    continue
                if action_name == DISMISS_ACTION:
                    return True
                if action_name == SNOOZE_ACTION:
                    try:
                        created_at = parse_ha_datetime(str(action_event.get("created_at") or ""))
                    except ValueError:
                        continue
                    if now - created_at < SNOOZE_DURATION:
                        return True
        return False

    def health_status(self) -> dict[str, Any]:
        return {
            "pending_confirmation": self._pending_confirmation,
            "last_notification_sent_at": (
                self._last_notification_sent_at.isoformat(timespec="seconds")
                if self._last_notification_sent_at is not None
                else None
            ),
            "last_notification_id": self._last_notification_id,
            "current_confirmation_token": self._current_confirmation_token,
            "last_ledger_action_seen_id": self._last_ledger_action_seen_id,
            "last_processed_action_id": self._last_processed_action_id,
            "ha_listener_connected": self._ha_listener_connected,
            "ha_listener_last_error": self._ha_listener_last_error,
            "action_state_path": str(self._action_state_path),
        }

    def _load_processed_action_ids(self) -> set[int]:
        try:
            payload = json.loads(self._action_state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return set()
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.warning("Could not load notification action state: %s", exc)
            return set()

        action_ids = payload.get("processed_action_ids") if isinstance(payload, dict) else None
        if not isinstance(action_ids, list):
            return set()
        return {int(action_id) for action_id in action_ids if isinstance(action_id, int)}

    def _save_processed_action_ids(self) -> None:
        self._action_state_path.parent.mkdir(parents=True, exist_ok=True)
        action_ids = sorted(self._processed_action_ids)[-500:]
        tmp_path = self._action_state_path.with_suffix(self._action_state_path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps({"processed_action_ids": action_ids}, separators=(",", ":")),
            encoding="utf-8",
        )
        tmp_path.replace(self._action_state_path)

    def _mark_action_processed(self, action_id: int) -> None:
        self._processed_action_ids.add(action_id)
        self._last_processed_action_id = action_id
        self._save_processed_action_ids()

    def _mark_recorded_action_processed(self, result: dict[str, Any] | None) -> None:
        if not isinstance(result, dict):
            return
        action_id = result.get("action_id")
        if isinstance(action_id, int):
            self._mark_action_processed(action_id)

    @staticmethod
    def _action_id(action_event: dict[str, Any]) -> int | None:
        raw_id = action_event.get("id")
        if isinstance(raw_id, int):
            return raw_id
        if isinstance(raw_id, str) and raw_id.isdigit():
            return int(raw_id)
        return None


def parse_ha_datetime(value: str) -> datetime:
    if value in {"", "unknown", "unavailable", "none"}:
        raise ValueError("empty timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def confirmation_action_token(signature: str) -> str:
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()[:16]


def confirmation_signature(summary: RunSummary) -> str:
    update_lines = [
        f"{attempt.name}: {attempt.from_version} -> {attempt.to_version}"
        for attempt in summary.updates[:4]
    ]
    extra = "" if len(summary.updates) <= 4 else f" +{len(summary.updates) - 4} more"
    return "confirm:" + "|".join(update_lines) + extra


def split_action_token(action: str) -> tuple[str, str]:
    if "::" not in action:
        return action, ""
    name, token = action.split("::", 1)
    return name, token


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


def update_event_signature(log_payload: dict[str, Any]) -> str:
    meaningful = {
        "entity_id": log_payload.get("entity_id"),
        "state": log_payload.get("state"),
        "installed_version": log_payload.get("installed_version"),
        "latest_version": log_payload.get("latest_version"),
        "in_progress": log_payload.get("in_progress"),
        "release_summary": log_payload.get("release_summary"),
        "release_url": log_payload.get("release_url"),
    }
    return json.dumps(meaningful, sort_keys=True, default=str)
