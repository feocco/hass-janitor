"""HTTP service wrapper for the Home Assistant janitor."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import logging
import os
from pathlib import Path
from typing import Any

from .audit import append_audit_log
from .client import HomeAssistantClient
from .monitor import JanitorMonitor, MonitorConfig
from .runner import UpdateRunner


DEFAULT_AUDIT_PATH = Path("/app/logs/ha-update-audit.md")
LOGGER = logging.getLogger("hass-janitor")


class ConfigError(RuntimeError):
    pass


class Config:
    def __init__(self) -> None:
        self.ha_base_url = required_env("HA_BASE_URL").rstrip("/")
        self.ha_token = required_env("HA_TOKEN")
        self.api_token = required_env("HASS_JANITOR_API_TOKEN")
        self.service_host = os.environ.get("SERVICE_HOST", "0.0.0.0")
        self.service_port = int(os.environ.get("SERVICE_PORT", "8092"))
        self.audit_path = Path(os.environ.get("AUDIT_PATH", str(DEFAULT_AUDIT_PATH)))
        self.monitor_enabled = env_bool("HASS_JANITOR_MONITOR_ENABLED", default=True)
        self.backup_entity_id = os.environ.get(
            "HASS_JANITOR_BACKUP_ENTITY_ID",
            "sensor.backup_state",
        )
        self.backup_timestamp_attribute = os.environ.get(
            "HASS_JANITOR_BACKUP_TIMESTAMP_ATTRIBUTE",
            "last_backup",
        )
        self.backup_max_age_days = int(os.environ.get("HASS_JANITOR_BACKUP_MAX_AGE_DAYS", "7"))
        self.check_interval_seconds = int(
            os.environ.get("HASS_JANITOR_CHECK_INTERVAL_SECONDS", str(24 * 60 * 60))
        )
        self.notification_cooldown_seconds = int(
            os.environ.get("HASS_JANITOR_NOTIFICATION_COOLDOWN_SECONDS", str(6 * 60 * 60))
        )


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required")
    return value


def env_bool(name: str, *, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return

    with open(path, encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


class Handler(BaseHTTPRequestHandler):
    server_version = "hass-janitor/1.0"

    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

        config: Config = self.server.config  # type: ignore[attr-defined]
        self._send_json(
            HTTPStatus.OK,
            {
                "status": "ok",
                "service": "hass-janitor",
                "ha_base_url_configured": bool(config.ha_base_url),
                "ha_token_configured": bool(config.ha_token),
                "api_token_configured": bool(config.api_token),
                "audit_path": str(config.audit_path),
            },
        )

    def do_POST(self) -> None:
        if self.path != "/v1/home-assistant/update":
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

        config: Config = self.server.config  # type: ignore[attr-defined]
        if not self._authorized(config.api_token):
            self._send_error(
                HTTPStatus.UNAUTHORIZED,
                "unauthorized",
                "Invalid or missing bearer token",
            )
            return

        try:
            payload = self._read_json()
            mode = parse_mode(payload)
        except ValueError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            return

        summary = run_update(mode=mode, config=config)
        response_status = "ok" if summary.exit_code == 0 else "failed"
        self._send_json(
            HTTPStatus.OK,
            {
                "status": response_status,
                "mode": summary.mode,
                "exit_code": summary.exit_code,
                "summary": to_jsonable(summary),
            },
        )

    def log_message(self, fmt: str, *args: Any) -> None:
        print("%s - - %s" % (self.address_string(), fmt % args), flush=True)

    def _authorized(self, expected_token: str) -> bool:
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        return hmac.compare_digest(header[len(prefix) :], expected_token)

    def _read_json(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0") or "0")
        raw_body = self.rfile.read(content_length) if content_length else b"{}"
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("Request body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object")
        return payload

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: HTTPStatus, code: str, message: str) -> None:
        self._send_json(status, {"error": {"code": code, "message": message}})


def parse_mode(payload: dict[str, Any]) -> str:
    mode = payload.get("mode", "preflight")
    if mode not in {"dry-run", "preflight", "run"}:
        raise ValueError("mode must be one of: dry-run, preflight, run")

    if mode == "run" and payload.get("confirm") is not True:
        raise ValueError("confirm must be true when mode is run")

    return str(mode)


def run_update(*, mode: str, config: Config):
    client = HomeAssistantClient(base_url=config.ha_base_url, token=config.ha_token)
    runner = UpdateRunner(client, base_url=config.ha_base_url)
    if mode == "dry-run":
        summary = runner.dry_run()
    elif mode == "preflight":
        summary = runner.preflight()
    else:
        summary = runner.run()

    append_audit_log(config.audit_path, summary)
    return summary


def to_jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if is_dataclass(value):
        return {key: to_jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    return value


def main() -> None:
    load_dotenv()
    config = Config()
    logging.basicConfig(level=logging.INFO)
    server = ThreadingHTTPServer((config.service_host, config.service_port), Handler)
    server.config = config  # type: ignore[attr-defined]
    monitor = None
    if config.monitor_enabled:
        monitor = JanitorMonitor(
            MonitorConfig(
                ha_base_url=config.ha_base_url,
                ha_token=config.ha_token,
                audit_path=config.audit_path,
                backup_entity_id=config.backup_entity_id,
                backup_max_age_days=config.backup_max_age_days,
                check_interval_seconds=config.check_interval_seconds,
                notification_cooldown_seconds=config.notification_cooldown_seconds,
                backup_timestamp_attribute=config.backup_timestamp_attribute,
            )
        )
        monitor.start()
        LOGGER.info("hass-janitor monitor started")
    print(
        f"hass-janitor listening on {config.service_host}:{config.service_port}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if monitor is not None:
            monitor.stop()
        server.server_close()


if __name__ == "__main__":
    main()
