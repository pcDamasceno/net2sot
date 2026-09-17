# Running the import

`net2sot/discovery.py` runs the discovery pipeline: **validate →
collect (NAPALM, Scrapli, Netmiko or a plugin) → process → sync → report**.
Hosts run in parallel (20 workers by default — see `config.yaml`).

All commands below run from the `net2sot/` directory, which is where
the script resolves `config.yaml` and `inventory/` from.

## Full inventory run

Devices are defined in `inventory/hosts.yaml` and grouped by platform in
`inventory/groups.yaml`:

```bash
python discovery.py                          # everything in hosts.yaml
python discovery.py --hosts core-rr-01 pe-emea-01   # a subset
python discovery.py --collector scrapli      # a different collection backend
python discovery.py --debug --save-raw       # verbose + keep raw collected data
python discovery.py --list-plugins           # installed collectors and sinks
```

## Import a single new device (no inventory edit)

```bash
python discovery.py \
  --device-name edge-01 \
  --device-ip 192.0.2.10 \
  --device-platform ios \        # a group in inventory/groups.yaml
  --site-name lab \              # optional NetBox site
  --device-role edge-router      # optional NetBox device role
```

The device is injected into the inventory at runtime, inherits connection
options from the platform group, and the run is limited to just that device.
Site and role are created in NetBox if they don't exist.

## Re-discover devices already in NetBox

`--from-netbox` sources the inventory from NetBox instead of `hosts.yaml`, and
reaches each device at its NetBox primary IP:

```bash
python discovery.py --from-netbox                    # every device in NetBox
python discovery.py --filter-site emea               # any --filter-* implies --from-netbox
python discovery.py --filter-platform eos ios
python discovery.py --filter-device pe-emea-01 pe-emea-02
python discovery.py --filter-location rack-a1
```

Filters map straight to the NetBox device API. Different `--filter-*` kinds are
**AND**-ed; multiple values within one kind are **OR**-ed. `--filter-device` is
a case-insensitive exact name match.

A device with no primary IP, or with a platform that maps to no inventory
group, is skipped with a warning. See the
[Pipeline reference](../net2sot/README.md#re-discovering-devices-already-in-netbox)
for `netbox_platform_map`, which bridges a NetBox platform slug to a group name
that doesn't match it.

## Write into a NetBox branch instead of main

`--netbox-branch <name>` scopes the whole run — the reads that source the
inventory and the writes that sync results — to an existing
[netbox-branching](https://github.com/netboxlabs/netbox-branching) branch.
Nothing on main changes until you review the diff and merge it in NetBox:

```bash
python discovery.py --filter-site emea --netbox-branch rediscovery
```

The branch must already exist; a name that doesn't resolve fails the run before
anything is collected.

## Configuration

Precedence: **CLI flags → environment variables → `settings.yaml`**.

Values can also come from `net2sot/.env` (git-ignored; see
[`.env.example`](../net2sot/.env.example)), which is loaded at startup
and never overrides a variable already set in the real environment.

| Environment variable    | settings.yaml key       | CLI flag        |
|-------------------------|-------------------------|-----------------|
| `NETBOX_URL`            | `netbox_url`            | —               |
| `NETBOX_TOKEN`          | `netbox_token`          | —               |
| `NETBOX_BRANCH`         | `netbox_branch`         | `--netbox-branch` |
| `NETBOX_VALIDATE_CERTS` | `netbox_validate_certs` | —               |
| `SITE_NAME`             | `site_name`             | `--site-name`   |
| `DEVICE_ROLE`           | `device_role`           | `--device-role` |
| `TENANT`                | `tenant`                | —               |
| `COLLECTOR`             | `collector`             | `--collector`   |
| `SINK`                  | `sink`                  | `--sink`        |
| `RAW_DATA_PATH`         | `raw_data_path`         | —               |
| `CREATE_CABLES`         | `create_cables`         | `--create-cables` |
| `UPDATE_EXISTING`       | `update_existing`       | `--no-update-existing` |
| `LLDP_ENABLED`          | `lldp_enabled`          | —               |
| `SAVE_RAW_DATA`         | `save_raw_data`         | `--save-raw`    |
| `DEBUG`                 | `debug`                 | `--debug`       |
| `LOG_FILE`              | —                       | `--log-file`    |
| `OUTPUT_FILE`           | —                       | `--output-file` / `-o` |
| `DEVICE_USERNAME`       | — (inventory defaults)  | —               |
| `DEVICE_PASSWORD`       | — (inventory defaults)  | —               |

`settings.yaml` additionally controls interface exclusion patterns, the
model-string → NetBox device-type mapping, VRF syncing, LLDP cable defaults,
collection retries and the custom fields kept up to date.

Cable creation is **off by default** — see `create_cables` in the
[Pipeline reference](../net2sot/README.md#cables).

## Output

- A console summary per host and for the whole run.
- With `--save-raw` / `SAVE_RAW_DATA=true`: raw getter output and a JSON report
  per device under `RAW_DATA_PATH` (default `/tmp/netbox_discovery`).
- With `--log-file` and/or `--output-file`: an appended log file and a
  structured JSON run report. Pass either flag alone to auto-name it under
  `logs/` (git-ignored), or set `LOG_FILE` / `OUTPUT_FILE` — including to the
  literal value `AUTO` — in CI.
- The exit code is non-zero if any host failed, so it is usable in CI.

## Troubleshooting

- **Collection failed / timeout** — check SSH reachability and credentials
  (`DEVICE_USERNAME` / `DEVICE_PASSWORD`, or `inventory/defaults.yaml`); IOS
  devices use the paramiko transport (see `inventory/groups.yaml`).
- **Cannot reach NetBox API** — the pre-flight validation tests `NETBOX_URL/api/`
  before touching any device.
- **Unknown platform group** — `--device-platform` must name a group defined in
  `inventory/groups.yaml`.
- **NAPALM fails on IOS-XR** — its driver needs `xml agent tty iteration off`
  configured on the router. Use `--collector scrapli` or `netmiko` instead.
- **A plugin isn't picked up** — `python discovery.py --list-plugins` answers
  "is it actually installed" without starting a run.
