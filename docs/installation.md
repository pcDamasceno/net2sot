# Installation

## Requirements

- Python ≥ 3.10
- A NetBox instance (4.x) and an API token with write permission
- Network reachability to NetBox **and** to the devices' management IPs (SSH)

## Install

Dependencies are declared in `pyproject.toml` and locked in `uv.lock`. With
[uv](https://docs.astral.sh/uv/):

```bash
uv sync                     # add "--extra dev" for ruff + pytest
```

or with pip:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .            # add ".[dev]" for ruff + pytest
```

### Optional extras

| Extra   | Installs | For |
|---------|----------|-----|
| `dev`   | `ruff`, `pytest` | Running the checks CI runs |
| `panos` | `napalm-panos`   | Collecting Palo Alto firewalls with NAPALM instead of the built-in `paloalto` collector |

```bash
uv sync --extra panos
```

`panos` is kept optional because it is a separate community package that pins
`lxml<6` for the whole environment. The built-in `paloalto` collector needs
none of that.

## Credentials

**Never commit real credentials.** Copy the example file and fill it in —
`.env` is git-ignored:

```bash
cp net2sot/.env.example net2sot/.env
```

`discovery.py` loads `net2sot/.env` at startup and never overrides a
variable already set in the real environment, so exporting works too:

```bash
export NETBOX_URL="https://netbox.example.com"
export NETBOX_TOKEN="..."
export DEVICE_USERNAME="admin"       # SSH login for the devices
export DEVICE_PASSWORD="..."
```

`DEVICE_USERNAME` / `DEVICE_PASSWORD` replace the inventory defaults in
`net2sot/inventory/defaults.yaml`. A host that pins its own
credentials in `inventory/hosts.yaml` keeps them — which is how one run covers
devices that don't share a password.

If your NetBox uses a self-signed certificate, set
`NETBOX_VALIDATE_CERTS=false` (or `netbox_validate_certs: false` in
`settings.yaml`).

## Verify

```bash
cd net2sot
uv run python discovery.py --help
uv run python discovery.py --list-plugins    # installed collectors and sinks
```

Neither touches a device or NetBox.
