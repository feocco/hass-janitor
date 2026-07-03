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
SERVICE_NAME = "hass-janitor"
OPENAPI_VERSION = "3.1.0"


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
        self.ledger_action_poll_seconds = int(
            os.environ.get("HASS_JANITOR_LEDGER_ACTION_POLL_SECONDS", "30")
        )
        self.action_state_path = Path(
            os.environ.get(
                "HASS_JANITOR_ACTION_STATE_PATH",
                str(self.audit_path.parent / "processed-notification-actions.json"),
            )
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
        if self.path == "/health":
            config: Config = self.server.config  # type: ignore[attr-defined]
            monitor = getattr(self.server, "monitor", None)
            monitor_health = monitor.health_status() if monitor is not None else None
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "service": SERVICE_NAME,
                    "ha_base_url_configured": bool(config.ha_base_url),
                    "ha_token_configured": bool(config.ha_token),
                    "api_token_configured": bool(config.api_token),
                    "audit_path": str(config.audit_path),
                    "monitor": monitor_health,
                },
            )
            return

        if self.path == "/docs":
            self._send_html(HTTPStatus.OK, self._render_docs_html())
            return

        if self.path == "/openapi.json":
            config: Config = self.server.config  # type: ignore[attr-defined]
            self._send_json(HTTPStatus.OK, self._openapi_spec(config))
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

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

    def _send_html(self, status: HTTPStatus, body_text: str) -> None:
        body = body_text.encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: HTTPStatus, code: str, message: str) -> None:
        self._send_json(status, {"error": {"code": code, "message": message}})

    def _render_docs_html(self) -> str:
        return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{SERVICE_NAME} API docs</title>
    <style>
      :root {{
        color-scheme: light;
        font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }}
      body {{
        margin: 0;
        padding: 24px;
        line-height: 1.5;
        color: #111827;
        background: #f9fafb;
      }}
      main {{
        max-width: 920px;
        margin: 0 auto;
      }}
      h1, h2 {{
        line-height: 1.2;
      }}
      section {{
        margin-top: 24px;
        padding: 16px;
        border: 1px solid #d1d5db;
        border-radius: 8px;
        background: #ffffff;
      }}
      code, pre {{
        font-family: ui-monospace, SFMono-Regular, SF Mono, Consolas, monospace;
        font-size: 0.95em;
      }}
      pre {{
        margin: 0;
        padding: 12px;
        background: #f3f4f6;
        border-radius: 6px;
        overflow-x: auto;
      }}
      a {{
        color: #1d4ed8;
      }}
      ul {{
        padding-left: 20px;
      }}
    </style>
  </head>
  <body>
    <main>
      <h1>{SERVICE_NAME} API docs</h1>
      <p>Self-contained API documentation for the Home Assistant janitor service.</p>
      <section>
        <h2>Reference</h2>
        <ul>
          <li><a href="/openapi.json">OpenAPI JSON</a></li>
          <li><code>GET /health</code> for service health</li>
          <li><code>POST /v1/home-assistant/update</code> for update runs</li>
        </ul>
      </section>
      <section>
        <h2>Auth</h2>
        <p>The update endpoint requires a bearer token in the <code>Authorization</code> header.</p>
        <pre>Authorization: Bearer &lt;token&gt;</pre>
        <p>Browser docs do not store or execute tokens.</p>
      </section>
      <section>
        <h2>Example</h2>
        <pre>curl -X POST http://localhost:8092/v1/home-assistant/update \\
  -H "Authorization: Bearer $HASS_JANITOR_API_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{{"mode":"preflight"}}'</pre>
      </section>
    </main>
  </body>
</html>
"""

    def _openapi_spec(self, config: Config) -> dict[str, Any]:
        return {
            "openapi": OPENAPI_VERSION,
            "info": {
                "title": SERVICE_NAME,
                "version": "1.0.0",
                "description": (
                    "HTTP wrapper for Home Assistant update workflows with audit logging."
                ),
            },
            "paths": {
                "/health": {
                    "get": {
                        "summary": "Health check",
                        "responses": {
                            "200": {
                                "description": "Service health response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HealthResponse"
                                        }
                                    }
                                },
                            }
                        },
                    }
                },
                "/docs": {
                    "get": {
                        "summary": "HTML documentation",
                        "responses": {
                            "200": {
                                "description": "Self-contained HTML docs",
                                "content": {
                                    "text/html": {
                                        "schema": {
                                            "type": "string"
                                        }
                                    }
                                },
                            }
                        },
                    }
                },
                "/openapi.json": {
                    "get": {
                        "summary": "OpenAPI document",
                        "responses": {
                            "200": {
                                "description": "OpenAPI specification",
                                "content": {
                                    "application/json": {
                                        "schema": {"type": "object"}
                                    }
                                },
                            }
                        },
                    }
                },
                "/v1/home-assistant/update": {
                    "post": {
                        "summary": "Run Home Assistant updates",
                        "security": [{"bearerAuth": []}],
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/UpdateRequest"
                                    }
                                }
                            },
                        },
                        "responses": {
                            "200": {
                                "description": "Update run result",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/UpdateResponse"
                                        }
                                    }
                                },
                            },
                            "400": {
                                "description": "Invalid request",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/ErrorResponse"
                                        }
                                    }
                                },
                            },
                            "401": {
                                "description": "Unauthorized",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/ErrorResponse"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
            },
            "components": {
                "securitySchemes": {
                    "bearerAuth": {
                        "type": "http",
                        "scheme": "bearer",
                    }
                },
                "schemas": {
                    "HealthResponse": {
                        "type": "object",
                        "required": [
                            "status",
                            "service",
                            "ha_base_url_configured",
                            "ha_token_configured",
                            "api_token_configured",
                            "audit_path",
                            "monitor",
                        ],
                        "properties": {
                            "status": {"type": "string"},
                            "service": {"type": "string"},
                            "ha_base_url_configured": {"type": "boolean"},
                            "ha_token_configured": {"type": "boolean"},
                            "api_token_configured": {"type": "boolean"},
                            "audit_path": {"type": "string"},
                            "monitor": {"type": ["object", "null"]},
                        },
                    },
                    "UpdateRequest": {
                        "type": "object",
                        "properties": {
                            "mode": {
                                "type": "string",
                                "enum": ["dry-run", "preflight", "run"],
                            },
                            "confirm": {"type": "boolean"},
                        },
                    },
                    "UpdateResponse": {
                        "type": "object",
                        "required": ["status", "mode", "exit_code", "summary"],
                        "properties": {
                            "status": {"type": "string"},
                            "mode": {"type": "string"},
                            "exit_code": {"type": "integer"},
                            "summary": {"type": "object"},
                        },
                    },
                    "ErrorResponse": {
                        "type": "object",
                        "required": ["error"],
                        "properties": {
                            "error": {
                                "type": "object",
                                "required": ["code", "message"],
                                "properties": {
                                    "code": {"type": "string"},
                                    "message": {"type": "string"},
                                },
                            }
                        },
                    },
                },
            },
            "x-configured-audit-path": str(config.audit_path),
        }


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
                ledger_action_poll_seconds=config.ledger_action_poll_seconds,
                action_state_path=config.action_state_path,
            )
        )
        monitor.start()
        LOGGER.info("hass-janitor monitor started")
    server.monitor = monitor  # type: ignore[attr-defined]
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
