"""REST client for Home Assistant."""

from __future__ import annotations

import json
import socket
from http.client import RemoteDisconnected
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class HAClientError(Exception):
    """Base exception for Home Assistant client failures."""


class HAAuthError(HAClientError):
    """Authentication or authorization failure."""


class HAConnectionError(HAClientError):
    """Network or transport failure while talking to Home Assistant."""


class HAResponseError(HAClientError):
    """Unexpected HTTP response from Home Assistant."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code
        self.message = message


class HomeAssistantClient:
    """Tiny REST client for the Home Assistant API."""

    def __init__(self, base_url: str, token: str, timeout: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def health_check(self) -> dict[str, Any]:
        """Call the API root to verify connectivity and authentication."""

        payload = self._request("GET", "/api/")
        if not isinstance(payload, dict):
            raise HAResponseError(500, "Unexpected response from API root")
        return payload

    def list_states(self) -> list[dict[str, Any]]:
        """Return all Home Assistant entity states."""

        payload = self._request("GET", "/api/states")
        if not isinstance(payload, list):
            raise HAResponseError(500, "Expected a list of states")
        return payload

    def get_state(self, entity_id: str) -> dict[str, Any]:
        """Return a single Home Assistant entity state."""

        payload = self._request("GET", f"/api/states/{quote(entity_id, safe='')}")
        if not isinstance(payload, dict):
            raise HAResponseError(500, f"Expected a state object for {entity_id}")
        return payload

    def install_update(self, entity_id: str) -> Any:
        """Request installation of the latest version for an update entity."""

        return self._request(
            "POST",
            "/api/services/update/install",
            payload={"entity_id": entity_id},
        )

    def restart_home_assistant(self) -> Any:
        """Request a Home Assistant restart."""

        return self._request("POST", "/api/services/homeassistant/restart")

    def _request(self, method: str, path: str, payload: Any | None = None) -> Any:
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")

        request = Request(
            url=f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )

        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw_body = response.read()
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace").strip()
            message = body or str(exc.reason or exc)
            if exc.code in (401, 403):
                raise HAAuthError(message) from exc
            raise HAResponseError(exc.code, message) from exc
        except (
            URLError,
            TimeoutError,
            socket.timeout,
            ConnectionError,
            ConnectionResetError,
            RemoteDisconnected,
            OSError,
        ) as exc:
            raise HAConnectionError(str(exc)) from exc

        if not raw_body:
            return None

        try:
            return json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise HAResponseError(500, "Response body was not valid JSON") from exc

