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

## Setup

1. Copy `.env.example` to `.env`.
2. Fill in:
   - `HA_BASE_URL`
   - `HA_TOKEN`

The CLI loads `.env` from the repo root automatically.

## Usage

```powershell
python -m hass_janitor dry-run
python -m hass_janitor run
python -m hass_janitor run --confirm
python -m unittest discover -s tests -v
```

## Notes

- This project uses only the Python standard library.
- `logs/ha-update-audit.md` is created locally and is intentionally ignored by git.
- `.env` is intentionally ignored by git.
