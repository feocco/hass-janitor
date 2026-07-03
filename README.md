# hass-janitor

Small Home Assistant update runner with audit logging.

## Features

- `python -m hass_janitor dry-run`
  - discovers pending `update.*` entities
  - records installed and latest versions
  - appends a Markdown audit entry
  - makes no changes
- `python -m hass_janitor run`
  - prints a preflight summary only
  - writes a preflight audit entry
- `python -m hass_janitor run --confirm`
  - installs pending updates sequentially
  - polls each entity for completion
  - performs one restart at the end if any install was accepted
  - waits for the Home Assistant API to come back
- `python -m hass_janitor.service`
  - runs a small authenticated HTTP wrapper for deployed homelab use
  - exposes `/health`, `/docs`, `/openapi.json`, and `POST /v1/home-assistant/update`
  - listens for Home Assistant update state changes and mobile notification actions
  - polls the shared notification ledger as a durable fallback for button actions
  - blocks update prompts when the configured backup timestamp is older than 7 days

## Setup

1. Copy `.env.example` to `.env`.
2. Fill in:
   - `HA_BASE_URL`
   - `HA_TOKEN`
   - `HASS_JANITOR_API_TOKEN` when running the HTTP service
   - `HOMELAB_FUNCTIONS_URL` and `HOMELAB_FUNCTIONS_TOKEN` for phone notifications
   - `HASS_JANITOR_BACKUP_ENTITY_ID` and, when the timestamp lives in an
     attribute, `HASS_JANITOR_BACKUP_TIMESTAMP_ATTRIBUTE`

The CLI loads `.env` from the repo root automatically.

## Usage

```powershell
python -m hass_janitor dry-run
python -m hass_janitor run
python -m hass_janitor run --confirm
python -m hass_janitor.service
python -m unittest discover -s tests -v
```

HTTP service examples:

```powershell
curl http://localhost:8092/health
curl http://localhost:8092/docs
curl http://localhost:8092/openapi.json
curl -X POST http://localhost:8092/v1/home-assistant/update `
  -H "Authorization: Bearer $env:HASS_JANITOR_API_TOKEN" `
  -H "Content-Type: application/json" `
  -d "{\"mode\":\"preflight\"}"
```

The deployed service also runs a monitor by default. It subscribes to Home
Assistant `state_changed` events, logs summarized `update.*` payloads, checks
backup freshness, and sends Joe a confirmation notification before running
updates. Notification action responses are recorded in the shared
`homelab-functions` notification ledger, and the monitor polls that ledger so a
button tap can still be handled after a missed WebSocket callback or service
restart. The update prompt supports updating now, snoozing the same update
fingerprint for 24 hours, or dismissing the same version set until the available
updates change.

## Notes

- This project uses only the Python standard library.
- `logs/ha-update-audit.md` is created locally and is intentionally ignored by git.
- `.env` is intentionally ignored by git.
