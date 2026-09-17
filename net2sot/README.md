# net2sot — the discovery pipeline

Collects facts from network devices (NAPALM, Scrapli, Netmiko or a plugin) and
syncs sites, devices, interfaces, IPs, VRFs and LLDP cables into a source of
truth. NetBox is the sink that ships today.

## Quick start

Dependencies are declared in the repo-root `pyproject.toml` and locked in
`uv.lock`, installed with [uv](https://docs.astral.sh/uv/). Run `discovery.py`
from this directory — it resolves `config.yaml` and `inventory/` relative to
the cwd.

```bash
uv sync                                              # from the repo root
cd net2sot
uv run python discovery.py                           # full inventory (inventory/hosts.yaml)
uv run python discovery.py --hosts pe-emea-01        # subset of the inventory
uv run python discovery.py --site-name lab --debug
```

## Collectors

| Backend   | Platforms             | Notes                                  |
|-----------|-----------------------|----------------------------------------|
| `napalm`  | ios, eos, iosxr, nxos        | Default. Also paloalto with the optional `panos` extra. |
| `scrapli` | ios, eos, iosxr, nxos        | Parses with Genie, falls back to TextFSM. |
| `netmiko` | srlinux, linux, + ios/eos/nxos/iosxr | NAPALM ships no SR Linux or Linux driver. |
| `paloalto`| paloalto, panos       | PAN-OS over Netmiko + ntc-templates. |
| `f5`      | f5, bigip, tmos       | BIG-IP over tmsh; nothing on PyPI can reach it. |
| *a plugin* | whatever it claims   | Installed from its own package; see [docs/plugins.md](../docs/plugins.md). |

A collector that names the platforms it claims is picked automatically for a host
on one of them: `paloalto` and `f5` do, which is why their groups carry no
`data.collector` pin and a mixed-vendor inventory still runs in one pass. The
`srlinux` and `linux` groups do pin `netmiko` — it is a multi-platform backend
that claims those platforms among others, so pinning is how they say which
backend they mean. A pin always wins over `--collector`.

Linux servers are discovered over a plain SSH shell: facts come from raw commands
(`hostname`, `/etc/os-release`, DMI sysfs, `/proc/uptime`) and interfaces/IPs from
`ip address show` parsed with ntc-templates (`linux_ip_address_show`). Kernel
interface names (`eth0`, `ens1f0`, `bond0`) are stored verbatim, not
canonicalized. There is no Linux LLDP template, so a Linux host is synced as
device + interfaces + IPs, never as a cable endpoint.

Palo Alto firewalls are collected by the `paloalto` plugin
(`collectors/paloalto.py`) over Netmiko (`paloalto_panos`) with ntc-templates:
facts from `show system info`, interfaces and their addresses from
`show interface all` (its hardware and logical sections are merged per
interface), and neighbours from `show lldp neighbors all`. PAN-OS names
(`ethernet1/1`, `ae1.100`, `loopback.1`) are stored verbatim, not canonicalized.
Two PAN-OS specifics are worth knowing: the management port is not in
`show interface all`, so it is rebuilt from `show system info` — that is what
gives the firewall its primary IP in NetBox — and `show interface all` reports
link state only, so every port is synced as administratively enabled and a dark
port is recorded as down rather than filtered out by `exclude_disabled`.

To collect a firewall with NAPALM instead, install the optional driver and flip
the group's pin:

```bash
uv sync --extra panos           # adds napalm-panos (pins lxml<6 for the env)
```

```yaml
# inventory/groups.yaml
paloalto:
  data:
    collector: napalm           # the napalm connection options are already there
```

F5 BIG-IP appliances are collected by the `f5` plugin (`collectors/f5.py`) over
tmsh (`f5_tmsh`). No NAPALM F5 driver is published on PyPI and ntc-templates has
no F5 templates, so tmsh output is parsed directly — which tmsh makes reasonable, because most `show` commands will emit
brace blocks on request (`show sys hardware field-fmt`) and every `list` command
already does, so one parser covers hardware, interfaces, VLANs and addresses.
Facts come from `show sys version`, `show sys hardware field-fmt` and the
configured hostname; ports from `list net interface` merged with
`show net interface field-fmt` (the first has the MAC, MTU and configured media,
the second the link status). Two F5 specifics:

- **Self-IPs belong to a VLAN, not to a port**, so each VLAN is synced as a
  virtual interface for its addresses from `list net self` to attach to. The
  management address (`list sys management-ip`) goes on `mgmt` and becomes the
  device's primary IP.
- A port that is merely dark reads `uninit`, which is not the same as disabled:
  admin state follows tmsh's explicit `disabled` flag, so unwired ports are
  inventoried rather than dropped by `exclude_disabled`.

Interface names (`1.1`, `mgmt`, VLAN names) are stored verbatim — canonicalizing
would turn `mgmt` into a `Management` port the appliance does not have. There is
no LLDP on this path, so a BIG-IP is synced as device + interfaces + IPs, never
as a cable endpoint.

### Adding a platform

Two ways, depending on how close the platform is to one already supported.

**A new vendor**: write a collector plugin. `collectors/paloalto.py` and
`collectors/f5.py` are the two to read first — each is one self-contained vendor
in one file, owning its commands and its parsing, reaching the transport through
the public helpers in `collectors/netmiko_support.py`. Both were part of
`tasks/collect_netmiko.py` before the plugin system existed, so the diff between
those two shapes is exactly what the plugin API buys.

A plugin can equally live in your own repository, depend on this project only for
the contract, and be wired in by an entry point — nothing here changes. See
[docs/plugins.md](../docs/plugins.md), and `examples/net2sot-mikrotik/`
for a complete installable one.

**A close variant of a platform `netmiko` already handles**, where plain SSH plus
ntc-templates will do: add a collector function to the `_COLLECTORS` registry in
`tasks/collect_netmiko.py` returning the same shape as the others, list the
platform in `NetmikoCollector.platforms` (`collectors/netmiko.py`), and add a
matching group in `inventory/groups.yaml`.

```bash
python discovery.py --list-plugins     # what is installed, built-in or not
```

A collector that declares the platforms it claims is selected automatically for
a host on one of them, so an installed plugin needs no `--collector` flag and no
`groups.yaml` pin. Resolution order is: the host's own `collector` (an inventory
pin always wins) → the run's collector if it supports the platform → any
installed collector that claims it.

### What this cannot reach

Collection is SSH-only, on every built-in collector. A device with no SSH
daemon — a containerized FRR or Alpine node in a lab topology, an appliance
that only speaks an API — has no CLI to log into, so no Netmiko platform will
ever reach it. That needs a different transport (`docker exec` + `vtysh`,
gNMI, a REST API), which is exactly what a collector plugin is free to bring:
the contract is about the facts you return, not about how you got them.

## Sinks

Where a run writes its results. `netbox` is the only one shipped, and the
default.

```bash
python discovery.py --sink netbox         # or SINK=netbox, or sink: in settings.yaml
```

A sink owns its own connection, creates whatever schema it needs once per run
(for NetBox: the custom-field definitions in `settings.yaml` that set
`enforce_creation`), and reports per device what landed and what did not. It may
also record whether the device answered at all — the NetBox sink moves a device
between `active` and `failed` for that, leaving a hand-set
`staged`/`planned`/`decommissioning` alone.

To write somewhere else — Infrahub, a CMDB, a directory of YAML files — write a
sink plugin: it receives the same normalized `DiscoveryResult` the NetBox one
does. See [docs/plugins.md](../docs/plugins.md).

## The discovery contract

Both plugin ends exchange Pydantic models, in `net2sot/schemas/`:

| Model | Role |
|---|---|
| `CollectedFacts` | what a collector returns — still in the device's dialect |
| `DiscoveryResult` | what a sink receives — normalized, vendor- and target-neutral |
| `SyncReport` | what a sink returns — what landed and what did not |

Two things this buys beyond type hints. Values are coerced at the boundary, so a
collector may return `speed="1000"`, `mtu=""` and `is_up="up"` without cleaning
them up first; and addresses are checked there, so a mis-parsed CLI line is
rejected at the plugin that produced it rather than surfacing as an opaque API
error mid-run. A single bad interface, address or neighbour is logged and
skipped — it costs that object, not the device.

`DiscoveryResult` also round-trips losslessly through JSON, so collection and
sync do not have to happen in the same process:

```python
data.model_dump_json()                      # archive, ship, replay
DiscoveryResult.model_validate_json(text)
```

## Importing a single new device (no inventory edit needed)

```bash
uv run python discovery.py \
  --device-name r9 \
  --device-ip 172.20.20.19 \
  --device-platform ios \      # ios | eos | iosxr | nxos | srlinux | linux | paloalto | f5 (inventory/groups.yaml)
  --site-name lab \            # optional: NetBox site (default: settings.yaml)
  --device-role core-router    # optional: NetBox device role (default: settings.yaml)
```

Site and role are created in NetBox if they don't exist yet.

The device is injected into the Nornir inventory at runtime, inherits
connection options from the chosen platform group, and the run is limited
to just that device.

## Re-discovering devices already in NetBox

`--from-netbox` sources the inventory from NetBox instead of
`inventory/hosts.yaml`. Each device is reached at its **NetBox primary IP** and
slotted into the platform group (`ios`/`eos`/`iosxr`/`nxos`/`srlinux`/`linux`/`paloalto`/`f5`) matching
its NetBox platform, so it inherits that group's connection options and collector
exactly like a static host — `inventory/groups.yaml` and `inventory/defaults.yaml`
still supply those. (The stock `NetBoxInventory2` plugin isn't used because it
invents `platform__<slug>` groups that carry none of that.)

```bash
uv run python discovery.py --from-netbox                    # every device in NetBox
uv run python discovery.py --filter-site emea               # any --filter-* implies --from-netbox
uv run python discovery.py --filter-platform eos ios
uv run python discovery.py --filter-device pe-emea-01 pe-emea-02
uv run python discovery.py --filter-location rack-a1
```

Filters map straight to the NetBox device API. Different `--filter-*` kinds are
**AND**-ed; multiple values within one kind are **OR**-ed (`--filter-platform eos
ios` matches eos *or* ios). `--filter-device` is a case-insensitive exact name
match. Any `--filter-*` implies `--from-netbox`.

A device with no primary IP (unreachable) or a platform that maps to no inventory
group (uncollectable) is skipped with a warning. When a NetBox platform slug does
not already match an inventory group name, bridge it with an optional
`netbox_platform_map` in `settings.yaml`:

```yaml
netbox_platform_map:
  cisco-ios-xe: ios
  arista-eos: eos
```

## Pushing NetBox-rendered configurations

`push_config.py` fetches each selected device's rendered configuration from
NetBox and loads it through Nornir's NAPALM connection. It is a dry run by
default: NAPALM calculates and prints the candidate diff but does not commit it.

```bash
# Preview the rendered configs for devices tagged "monitoring"
uv run python push_config.py --filter-tag monitoring

# Preview one device using replacement semantics
uv run python push_config.py --filter-device pe-emea-01 --replace

# Commit the rendered configs (merge is the default strategy)
uv run python push_config.py --filter-tag monitoring --apply
```

The script supports the same site, location, platform and device filters as
NetBox-sourced discovery, plus `--filter-tag`. Different filter kinds are
AND-ed and multiple values within one filter are OR-ed. At least one filter is
required unless `--all` is supplied explicitly, preventing an accidental
inventory-wide run. Use `--replace` only when the rendered configuration is a
complete replacement; otherwise the default `--merge` behavior is safer.

Devices must have a NetBox config template/context that produces non-empty
rendered content, a primary IP, and a platform mapped to a Nornir group with a
working NAPALM connection. `DEVICE_USERNAME` and `DEVICE_PASSWORD` override the
inventory defaults exactly as they do for discovery.

## Discovering into a NetBox branch

`--netbox-branch <name>` scopes the **entire run** — both the reads that source a
`--from-netbox` inventory and the writes that sync results — to a NetBox branch
(the [netbox-branching](https://github.com/netboxlabs/netbox-branching) plugin)
instead of committing straight to main. Nothing on main changes until you review
the branch's diff and merge it in NetBox.

```bash
uv run python discovery.py --from-netbox --netbox-branch rediscovery
uv run python discovery.py --filter-site emea --netbox-branch rediscovery
```

The branch must **already exist** in NetBox (create it under the Branching
plugin; a name that doesn't resolve fails the run before anything is collected).
The run resolves the branch to its `schema_id` and pins the `X-NetBox-Branch`
header on every API call, so a re-run reads the branch's own view (which equals
main until it diverges) and writes back into it. It works with any run mode, but
pairs most naturally with `--from-netbox` re-discovery: iterate against the
branch, eyeball the diff, then merge.

Also settable with `NETBOX_BRANCH` (CI) or `netbox_branch` in `settings.yaml`.
Omit it entirely to keep the previous behaviour of writing directly to main.

## Configuration precedence

`CLI flags` → `environment variables` → `settings.yaml`

| Environment variable    | settings.yaml key      | Notes                        |
|-------------------------|------------------------|------------------------------|
| `NETBOX_URL`            | `netbox_url`           |                              |
| `NETBOX_TOKEN`          | `netbox_token`         | keep out of git — use env    |
| `NETBOX_BRANCH`         | `netbox_branch`        | branch name; also `--netbox-branch` |
| `NETBOX_VALIDATE_CERTS` | `netbox_validate_certs`| `true`/`false`               |
| `SITE_NAME`             | `site_name`            |                              |
| `TENANT`                | `tenant`               |                              |
| `DEVICE_ROLE`           | `device_role`          |                              |
| `RAW_DATA_PATH`         | `raw_data_path`        | report/raw-data output dir   |
| `COLLECTOR`             | `collector`            | backend name; also `--collector` |
| `SINK`                  | `sink`                 | sink name; also `--sink`     |
| `CREATE_CABLES`         | `create_cables`        | `true`/`false`, default off  |
| `UPDATE_EXISTING`       | `update_existing`      | `true`/`false`, default on   |
| `LLDP_ENABLED`          | `lldp_enabled`         | `true`/`false`               |
| `SAVE_RAW_DATA`         | `save_raw_data`        | `true`/`false`               |
| `DEBUG`                 | `debug`                | `true`/`false`               |
| `LOG_FILE`              | —                      | log file path; also `--log-file`; `AUTO` to auto-name |
| `OUTPUT_FILE`           | —                      | run-report JSON path; also `--output-file`; `AUTO` to auto-name |
| `DEVICE_USERNAME`       | —                      | overrides inventory defaults |
| `DEVICE_PASSWORD`       | —                      | overrides inventory defaults |

`DEVICE_USERNAME` / `DEVICE_PASSWORD` replace `inventory/defaults.yaml`, not
per-host credentials. A host that sets its own `username`/`password` keeps them,
which is how one run covers devices that don't share a password.

## Cables

`create_cables` is **false by default** (`--create-cables` / `CREATE_CABLES=true`
to enable). A cable is a physical claim, and LLDP only proves that two ports can
hear each other, so cabling is opt-in per run. Enable it once both ends of the
links you care about are already in NetBox — a neighbour whose device is missing
is skipped rather than cabled, unless `lldp_create_missing_devices` is also on.

Settings without an environment override:

- `lldp_exclude_management` (default `true`) — LLDP heard on a management port
  comes from the shared OOB network, so it is not turned into a cable. Set it
  `false` only if your management ports are genuinely cabled point-to-point.
- `lldp_create_missing_devices` (default `false`) — create a placeholder NetBox
  device for a neighbour that isn't there yet, so its cable can be made.

## Device types

The NetBox device type is resolved in order:

1. A pattern from `device_type_mapping` in `settings.yaml`, so you can pin an
   exact device-type slug you already maintain in NetBox.
2. Otherwise **the model the device reported** — an `ISR4331/K9` is created in
   NetBox as `ISR4331/K9` (slug `isr4331-k9`) rather than bucketed into
   `cisco-generic`.
3. The vendor `default` only when the device reported no usable model.

### Updating devices already in NetBox

`update_existing` (default `true`, `--no-update-existing` to opt out) lets a
re-run refresh what discovery owns on a device already in NetBox:

- **device_type** — re-pointed at the type this run resolved, so a device created
  before the mapping knew better (stuck on `cisco-generic`) gets corrected.
- **comments** — the auto-discovered block (timestamp, OS version, uptime,
  serial) is rewritten, since it goes stale between runs.
- **interface descriptions** — brought in line with what the device reports,
  including clearing one the device no longer carries.
- **stale IPs** — any IP still attached to a synced interface that this run did
  not rediscover is deleted, so a changed management IP leaves no orphan behind.
  An old IP still pinned as the device's primary is unpinned first, then removed.

Serial, platform, role and site on an existing device are left alone: they may
have been curated by hand in NetBox, and discovery is not the authority on them.
Under `--no-update-existing` a re-run is purely additive — nothing existing is
modified or deleted. LLDP placeholder devices are never updated this way, because
that path resolves facts from the *neighbour* rather than the device's own.

## Primary IP selection

The device's primary IPv4 in NetBox is chosen in order:

1. The first IPv4 found on a management interface (`Management*`, `mgmt*`,
   `ma0`, `fxp0`, `em0`, ...).
2. If the device has no management interface, the IP address used to log in
   (the inventory `hostname`), provided it was discovered on one of the
   device's interfaces.

## Logging and run reports

By default logs go to the console only. To keep a record of a run (production
runs especially), add `--log-file` and/or dump the structured result with
`--output-file`:

```bash
# Console + a timestamped log file under logs/
uv run python discovery.py --from-netbox --filter-site nyc --log-file

# Log to an explicit file and write the run result as JSON
uv run python discovery.py --from-netbox --filter-site nyc \
  --log-file logs/nyc.log --output-file logs/nyc.json

# Give either flag alone to auto-name it under logs/:
#   logs/discovery_run_<timestamp>.log
#   logs/discovery_report_<timestamp>.json
uv run python discovery.py --from-netbox --filter-site nyc -o
```

`--log-file` appends, so re-runs never truncate an earlier log. `--output-file`
writes a JSON document you can diff between runs to verify results and
behaviour: run metadata (command, filters, NetBox URL/branch, timing),
aggregate totals, the failed hosts (each with its failure reason and, for a
partial sync, what did land), and a per-host block with the discovered device
summary and full sync stats. A duplicate IP (the same address configured twice,
or already owned in its VRF) is counted separately and does not fail the host or
the run. Both land in `logs/`, which is git-ignored.

For CI, set `LOG_FILE` and `OUTPUT_FILE` instead of the flags (the flag wins if
both are given). Point them at explicit paths, or set the value to `AUTO` to
auto-name under `logs/`:

```bash
LOG_FILE=logs/nyc.log OUTPUT_FILE=AUTO \
  uv run python discovery.py --from-netbox --filter-site nyc
```

## GitLab CI

`.gitlab-ci.yml` (repo root) runs this import in CI. To onboard a new device,
trigger a pipeline with the inputs declared in its `spec:` header:

```bash
curl -X POST \
  -F "token=$TRIGGER_TOKEN" \
  -F "ref=main" \
  -F "inputs[device_name]=edge-01" \
  -F "inputs[device_ip]=192.0.2.10" \
  -F "inputs[device_platform]=ios" \
  -F "inputs[site_name]=lab" \
  -F "inputs[device_role]=edge-router" \
  "https://gitlab.example.com/api/v4/projects/<project-id>/trigger/pipeline"
```

Set `NETBOX_URL`, `NETBOX_TOKEN`, `DEVICE_USERNAME` and `DEVICE_PASSWORD` as
masked CI/CD variables (Settings → CI/CD → Variables). Discovery reports are
kept as pipeline artifacts. There is also a `rediscover:from-netbox` job driven
by `filter_*` inputs or `FILTER_*` variables (so it works from a schedule), and
a manual `import:all-devices` job on the default branch that re-imports the
whole static inventory.

Full details in [docs/gitlab-ci.md](../docs/gitlab-ci.md).
