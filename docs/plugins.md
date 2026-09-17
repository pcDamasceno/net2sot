# Writing a plugin

This project is extensible at two points, and a plugin for either lives in your
own repository and installs with `pip`. Nothing here has to change to accept it.

| You want to… | Write a | Which returns |
|---|---|---|
| add a vendor, platform or transport | **collector** | `CollectedFacts` |
| write to another source of truth | **sink** | `SyncReport` |

The shape of the pipeline:

```
   your collector                    this project                      your sink
┌───────────────────┐        ┌────────────────────────┐        ┌──────────────────┐
│ talk to the device│  ───►  │ canonical names,       │  ───►  │ write to NetBox, │
│ parse its output  │        │ filtering, typing,     │        │ Infrahub, a CMDB │
│                   │        │ VRFs, primary IP       │        │                  │
└───────────────────┘        └────────────────────────┘        └──────────────────┘
    CollectedFacts                                                DiscoveryResult
```

The middle is the part you do not write. Canonical interface names, interface
filtering, device-type mapping, VRF membership, primary-IP selection and
reporting are neither vendor- nor target-specific, so they are done once, for
every platform alike.

## The contract

Three Pydantic models, all importable from `net2sot.schemas`:

```python
from net2sot.schemas import CollectedFacts, DiscoveryResult, SyncReport
```

| Model | Defined in | Role |
|---|---|---|
| `CollectedFacts` | `schemas/facts.py` | what a collector returns — still in the device's dialect |
| `DiscoveryResult` | `schemas/discovery.py` | what a sink receives — normalized and vendor-neutral |
| `SyncReport` | `schemas/sync.py` | what a sink returns — what landed and what did not |

Two properties of every model in the contract are worth knowing before you start.

**Undeclared keys survive.** A vendor knows things this project did not model, so
validation keeps them rather than rejecting them:

```python
facts = CollectedFacts.from_napalm({"interfaces": {"ether1": {"is_up": True, "poe_class": 4}}})
facts.interfaces["ether1"].poe_class            # 4
```

That is an escape hatch, not the front door. When you want a *sink* to see
something deliberately, put it in the `custom` dict that the normalized models
carry — those are addressable from `settings.yaml`, see
[Publishing your own fields](#publishing-your-own-fields).

**Values are coerced at the boundary.** CLI output is strings and a parser that
found nothing returns `""`. You do not have to clean that up:

```python
InterfaceFacts(speed="1000", mtu="", is_up="up").speed     # 1000
InterfaceFacts(speed="1000", mtu="", is_up="up").mtu       # None
InterfaceFacts(speed="1000", mtu="", is_up="up").is_up     # True
DeviceFacts(serial_number="N/A").serial_number             # ""
```

`"up"`, `"enabled"`, `"yes"`, `"active"` and their opposites all resolve to
booleans; `"N/A"`, `"none"`, `"not set"` and `"-"` resolve to absence. `"unknown"`
deliberately does not — it is this project's own default for an unidentified
model, so it is kept as a value.

Some fields are checked rather than coerced, because getting them wrong is a bug
worth hearing about at your own boundary rather than three steps later as a
confusing API rejection:

```python
DiscoveredIP(address="2001:db8::1/64", interface="Lo0", ip_version="ipv4")
# ip_version and prefix_length are re-derived from the address: "ipv6", 64

DiscoveredIP(address="10.0.0.1/Vlan10", interface="Gi0/0")
# ValidationError: '10.0.0.1/Vlan10' is not a valid IP address in CIDR form
```

A single malformed interface, address or neighbour costs you that one object
(logged and skipped), not the whole device.

**One caveat about building the models by hand.** Pydantic validates assignment
to a *field*, not mutation of a container a field already holds — so this puts an
unvalidated dict inside an otherwise valid model:

```python
facts.interfaces["ether1"] = {"is_up": "up"}              # not validated
facts.interfaces_ip["ether1"].ipv4["10.0.0.1"] = {...}    # not validated
```

It will not break your run — `process_facts()` re-validates everything it is
given, so a collector may hand back plain dicts throughout if that is easier —
but the values are not coerced until then. Use `facts.add_address(...)` for
addresses, construct `InterfaceFacts(...)` explicitly for interfaces, or call
`facts.normalized()` before returning, and your own assertions see clean values.

## Two real ones to read first

Before the template below, the two worked examples in this repository:

| File | Shows |
|---|---|
| `net2sot/collectors/paloalto.py` | a vendor over Netmiko + ntc-templates: a command that needs a fallback, a management port rebuilt from a different command, best-effort LLDP |
| `net2sot/collectors/f5.py` | a platform nothing on PyPI can reach: its own brace-block parser, addresses that belong to a VLAN rather than a port, no LLDP at all |

Both were sections of `tasks/collect_netmiko.py` before the plugin system
existed. The parsing did not change when they moved; what changed is that each
vendor now owns a file, declares the platforms it claims, and reaches the
transport through public helpers instead of a neighbouring module's private
functions. That diff is what the plugin API is for.

### Transport helpers

A collector that talks SSH usually wants `collectors/netmiko_support.py` rather
than its own Netmiko boilerplate:

```python
from net2sot.collectors.netmiko_support import (
    send,              # raw output
    send_textfsm,      # ntc-templates rows, [] when nothing parsed
    prompt_hostname,   # for platforms whose 'show version' names no host
    ensure_net_textfsm,
    convert_facts, convert_interfaces, convert_interfaces_ip,
    convert_lldp, convert_cdp,   # parsed rows -> the contract's shapes
)
```

`send_textfsm` is worth singling out: Netmiko hands back the raw *string* when no
template matches or nothing parses — including protocol-disabled banners like
`% LLDP is not enabled`. That is collapsed to `[]`, so "no rows" and "no data"
are the same thing and you never have to type-check the result.

## Writing a collector

Subclass `Collector` and implement one method.

```python
# net2sot_mikrotik/collector.py
from net2sot.plugins import CollectContext, Collector
from net2sot.schemas import CollectedFacts, DeviceFacts, InterfaceFacts
from nornir_netmiko.tasks import netmiko_send_command


class MikroTikCollector(Collector):
    name = "mikrotik"                 # --collector mikrotik
    platforms = ("routeros",)         # matched against the host's platform
    description = "RouterOS over SSH"

    def collect(self, ctx: CollectContext) -> CollectedFacts:
        facts = CollectedFacts()

        output = ctx.task.run(
            task=netmiko_send_command, command_string="/system resource print"
        )[0].result
        parsed = parse_resource(output)                      # your parser

        facts.facts = DeviceFacts(
            hostname=parsed["hostname"],
            vendor="MikroTik",
            model=parsed["board-name"],
            os_version=parsed["version"],
            uptime=parsed["uptime-seconds"],                 # "3600" is fine
        )

        for row in parse_interfaces(ctx.task):
            facts.interfaces[row["name"]] = InterfaceFacts(
                is_up=row["running"],                        # "true"/"yes"/"up"
                is_enabled=not row["disabled"],
                description=row["comment"],
                mac_address=row["mac-address"],
                mtu=row["mtu"],                              # "" is fine
            )

        for row in parse_addresses(ctx.task):
            # add_address() rather than building facts.interfaces_ip by hand:
            # it works out the address family, and it validates. See below.
            facts.add_address(row["interface"], row["ip"], row["len"])

        # A device with no name cannot be written to a source of truth. Fail
        # here, where the cause has a name, rather than three steps later.
        facts.require_hostname(fallback=ctx.name)
        return facts
```

### The context

`CollectContext` is what your collector is given for one device.

| | |
|---|---|
| `ctx.task` | the Nornir task — `ctx.task.run(...)`, `ctx.task.host.get_connection(...)` |
| `ctx.host` | the Nornir host object |
| `ctx.name` | the device's **inventory** name, which may differ from its hostname |
| `ctx.address` | the address or DNS name Nornir connected to |
| `ctx.platform` | the platform as the inventory spells it |
| `ctx.settings` | the whole run's settings |
| `ctx.option(key, default)` | one setting, host data first, then run settings |

Prefer `ctx.option()` over `ctx.settings[...]`. It implements the precedence the
rest of the project uses — a group's own data wins over the run-wide value —
which is what lets an operator pin behaviour for one group while a CLI flag
still sets the default for everything else.

### What to raise, and what not to

**Raise** when the device is undiscoverable: unreachable, refused the login, on a
platform you do not support. The run marks that one device failed, tells the sink
it did not answer, and carries on with the rest of the inventory.

**Do not raise** for a section that simply is not available. A platform with no
LLDP, or a command this software version does not have, leaves that part of
`CollectedFacts` empty and the pipeline degrades to what it was given — a device
and its interfaces is a perfectly good outcome.

### Lifecycle

Your collector is constructed **once per run** and `collect()` is called **once
per device**, from a worker thread, in parallel with others. Anything expensive
and device-independent — loading templates, reading a config file — belongs in
`__init__`, which receives the run's settings.

## Writing a sink

Subclass `Sink` and implement `sync()`. `open()` and `close()` are optional and
bracket the whole run.

```python
from net2sot.plugins import Sink, SyncContext
from net2sot.schemas import SyncReport


class InfrahubSink(Sink):
    name = "infrahub"                 # --sink infrahub
    description = "Infrahub over GraphQL"

    def open(self) -> None:
        # Connect, authenticate, make sure the schema exists. Once per run.
        # Raising here aborts the run, which is right: discovering a whole
        # inventory into a target that cannot accept it is wasted work.
        self.client = InfrahubClient(address=self.settings["infrahub_url"])

    def sync(self, ctx: SyncContext) -> SyncReport:
        report = SyncReport(sink=self.name)
        data = ctx.result

        device = self.client.upsert_device(
            name=ctx.device_name,
            platform=ctx.platform,          # from the result, not from settings
            model=data.device.model,
            serial=data.device.serial_number,
        )
        report.device_created = True

        for interface in data.interfaces:
            try:
                self.client.upsert_interface(device, interface)
                report.interfaces_created += 1
            except ApiError as exc:
                # A failure goes in the report, not into an exception: a device
                # that landed by halves is a different outcome from one that was
                # never collected, and the run has to tell them apart.
                report.errors.append(f"{interface.name}: {exc}")

        return report

    def close(self) -> None:
        self.client.close()
```

`SyncContext` carries `ctx.result` (the `DiscoveryResult`), `ctx.settings`,
`ctx.start_time`, and the two shortcuts `ctx.device_name` and `ctx.platform`.

Read `ctx.platform` from the context, never `settings["collector"]`-style out of
the run settings: one settings dict is shared by every worker thread, so a
mixed-platform inventory would race there.

`sync()` runs on a worker thread, one device at a time, in parallel — anything it
touches on `self` has to tolerate that.

### Counters

`SyncReport`'s counters are named after objects any DCIM/IPAM has — sites,
devices, interfaces, addresses, VRFs, cables — so the run report prints your
sink's numbers without knowing anything about it. For something the vocabulary
has no name for, use `report.counters`:

```python
report.counters["tags_applied"] = 3
```

`report.succeeded` is false when `errors` is non-empty or `ip_addresses_failed`
is non-zero, and that is what fails the host. `ip_addresses_duplicate` is
counted separately and deliberately does *not* fail the run: the same address
configured twice is a data condition on the device, not a sync failure.

### Reachability

Discovery knows something a source of truth usually does not: whether the device
answered. Two optional hooks record it, and both default to doing nothing:

```python
    def mark_unreachable(self, device_name: str) -> None: ...
    def mark_reachable(self, device_name: str) -> None: ...
```

The NetBox sink uses them to move a device between `active` and `failed`, while
leaving a hand-set `staged`/`planned`/`decommissioning` status alone. A target
with nowhere to put this just does not implement them.

## Packaging it

Advertise the class from your package's entry points. This is exactly how the
built-in collectors and the NetBox sink are registered — nothing about them is
special-cased, so if the mechanism breaks it breaks for this project first.

```toml
# your own pyproject.toml
[project]
name = "net2sot-mikrotik"
dependencies = ["net2sot>=0.1", "nornir-netmiko>=1.0"]

[project.entry-points."net2sot.collectors"]
mikrotik = "net2sot_mikrotik.collector:MikroTikCollector"

# ...or, for a sink:
[project.entry-points."net2sot.sinks"]
infrahub = "net2sot_infrahub.sink:InfrahubSink"
```

Then:

```bash
pip install net2sot-mikrotik
python discovery.py --list-plugins        # confirm it is actually installed
```

A plugin that fails to import is logged and skipped, never fatal — one broken
package on the system does not take down a run that does not use it.

### Getting your collector chosen

In order:

1. **The host's own `collector`**, inherited from its groups. An inventory pin
   always wins.
   ```yaml
   # inventory/groups.yaml
   mikrotik:
     platform: routeros
     data:
       collector: mikrotik
   ```
2. **The run's collector** (`--collector`, or `settings.yaml`) if it claims the
   host's platform.
3. **Any installed collector that claims the platform.** This is why
   `platforms = ("routeros",)` matters: with it, a host on `platform: routeros`
   is collected by your plugin on installation alone — no `groups.yaml` pin, no
   `settings.yaml` entry.
4. The run's collector anyway, so the failure comes from the backend, which can
   say what it does support.

Declare `platforms = ("*",)` only for a genuinely platform-agnostic backend. It
means "I am a usable default", and it is excluded from the automatic match in
step 3 so that a plugin which names a platform always wins it.

## Publishing your own fields

Every normalized model carries a `custom` dict, and it connects to the
`custom_fields` block in `settings.yaml`. Your collector fills it:

```python
    facts.facts.custom = {"bgp_asn": 65000}     # survives into DiscoveryResult
```

…and an operator addresses it by name, without either side knowing about the
other:

```yaml
custom_fields:
  - name: bgp_asn
    label: "BGP ASN"
    type: integer
    object_types: [dcim.device]
    source: bgp_asn          # not a built-in source → looked up in device.custom
    enforce_creation: true
```

## Testing your plugin

Neither contract needs a device or a NetBox to exercise. Build the facts your
parser would produce and push them through the real pipeline:

```python
from net2sot.tasks.process import process_facts

def test_normalizes():
    facts = CollectedFacts(
        facts=DeviceFacts(hostname="rb1", vendor="MikroTik", model="RB5009UG"),
        interfaces={"ether1": InterfaceFacts(is_up="up", speed="1000")},
    )
    result = process_facts(facts, platform="routeros", normalize_names=False)

    assert result.device.vendor == "MikroTik"
    assert result.interfaces[0].speed == 1000
```

`tests/test_plugins.py` in this repository runs a whole `discover_device()` —
collect, process, sync — on a collector and a sink it does not ship, with a
stand-in Nornir task. Copy it. `tests/test_collectors_paloalto.py` and
`tests/test_collectors_f5.py` show the other half: real parsed rows and real CLI
output as fixtures, so a change in the vendor's output format fails a test rather
than a device.

When you do have a device to hand, the check worth running is the one that says
your plugin changed nothing it should not have: collect through the old path and
the new one and diff the results. That is how the PAN-OS and F5 plugins were
verified — every collected value identical, the only differences being fields the
contract declares and the devices' own uptime.

Set `normalize_names=False` for a platform whose own interface names are the real
identifiers. Linux (`eth0`, `bond0`), PAN-OS (`ethernet1/1`) and BIG-IP (`1.1`,
`mgmt`) all do: canonicalizing would rewrite them into interfaces that do not
exist on the device. Add your platform to `VERBATIM_INTERFACE_PLATFORMS` in
`discovery.py` if yours is one of them.

## Contract stability

`net2sot.schemas.SCHEMA_VERSION` versions the declared fields, and
`CollectedFacts.schema_version` stamps it onto collected data so an archived
payload stays interpretable.

- **Major** — a declared field is removed, renamed, or its type narrowed.
- **Minor** — fields are added.

Adding a field is not a breaking change, because every model has defaults for
everything and accepts keys it does not declare. A plugin written against 1.0
keeps working across the whole 1.x line.

## Checklist

- [ ] `name` is unique, lowercase, and the thing you would type after `--collector`
- [ ] `platforms` names the platforms you actually support (not `("*",)`)
- [ ] `collect()` raises on an unreachable device, and leaves sections empty for
      data that is merely unavailable
- [ ] `require_hostname(fallback=ctx.name)` before returning
- [ ] expensive setup is in `__init__`, not `collect()`
- [ ] the entry point is declared, and `--list-plugins` shows it
- [ ] tests drive `process_facts()` with hand-built `CollectedFacts` — no device needed
- [ ] if you replaced an existing collection path, you diffed old against new on
      a real device before deleting the old one
