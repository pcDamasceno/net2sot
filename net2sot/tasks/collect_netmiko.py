"""
collect_netmiko.py - Collect device facts over SSH with Netmiko.

Output is shaped exactly like collect_napalm() / collect_scrapli() so
tasks/process.py stays platform-agnostic.

Supported platforms:
  - srlinux  Nokia SR Linux (netmiko device_type ``nokia_srl``) — NAPALM has
             no driver at all
  - iosxr    Cisco IOS-XR (netmiko device_type ``cisco_xr``) — the NAPALM
             driver needs the router's XML agent (`xml agent tty iteration
             off`), which most boxes don't run
  - ios      Cisco IOS/IOS-XE (``cisco_ios``)
  - eos      Arista EOS (``arista_eos``)
  - nxos     Cisco NX-OS (``cisco_nxos``)
  - linux    Linux servers (netmiko device_type ``linux``) — NAPALM/Scrapli
             have no Linux driver. Facts come from raw commands (hostname,
             /etc/os-release, DMI sysfs, /proc/uptime); interfaces and IPs from
             ``ip address show`` parsed with ntc-templates. See _collect_linux().

ios/eos/nxos also collect fine over NAPALM or Scrapli; the Netmiko path lets a
single ``--collector netmiko`` cover a whole mixed inventory (e.g. a lab where
NAPALM/Scrapli are blocked). See _collect_ios_like().

Palo Alto PAN-OS and F5 BIG-IP were collected here too, and are now collector
plugins of their own -- net2sot/collectors/paloalto.py and f5.py. They
moved because each is a self-contained vendor implementation with nothing to
share but the transport, which is what a plugin is; see docs/plugins.md. The
transport helpers they use are public in collectors/netmiko_support.py.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from nornir.core.exceptions import NornirSubTaskError
from nornir.core.task import Task
from nornir_netmiko.tasks import netmiko_send_command

from net2sot.tasks.collect import (
    _convert_cdp,
    _convert_facts,
    _convert_interfaces,
    _convert_interfaces_ip,
    _convert_lldp,
    _unwrap,
    _upper_keys,
)

logger = logging.getLogger("discovery.collect")

_READ_TIMEOUT = 60


def _ensure_net_textfsm() -> None:
    """
    Point netmiko at the installed ntc-templates via NET_TEXTFSM. Without it,
    netmiko finds the template directory through importlib.resources.path(), which
    it calls with keyword args ('package'/'resource') that several Python versions
    reject -- surfacing as "path() got an unexpected keyword argument 'package'"
    the moment a Netmiko command is parsed with use_textfsm. Setting NET_TEXTFSM
    makes netmiko skip that lookup entirely. A user-provided value always wins.
    """
    if os.environ.get("NET_TEXTFSM"):
        return
    try:
        import ntc_templates
    except ImportError:
        return
    templates = os.path.join(os.path.dirname(ntc_templates.__file__), "templates")
    if os.path.isdir(templates):
        os.environ["NET_TEXTFSM"] = templates


def collect_netmiko(task: Task, platform: str) -> dict[str, Any]:
    """Dispatch to the per-platform Netmiko collector."""
    _ensure_net_textfsm()
    key = platform.lower()
    moved = _MOVED_TO_PLUGINS.get(key)
    if moved:
        raise ValueError(
            f"Platform '{platform}' is collected by the '{moved}' plugin, not by "
            f"'netmiko'. Drop the `collector: netmiko` pin from its group in "
            f"inventory/groups.yaml -- the platform is then matched automatically "
            f"-- or change the pin to `collector: {moved}`."
        )
    collector = _COLLECTORS.get(key)
    if collector is None:
        supported = ", ".join(sorted(_COLLECTORS))
        raise ValueError(
            f"No Netmiko collector for platform '{platform}' (supported: {supported})"
        )
    try:
        return collector(task)
    except NornirSubTaskError as e:
        # netmiko wraps the driver error as an opaque "Subtask ... (failed)";
        # surface the real cause (often a ReadTimeout when the shell prompt was
        # never matched) so the run log says what actually broke.
        raise RuntimeError(f"netmiko command failed: {_unwrap(e)}") from e


def _send(task: Task, command: str, expect_string: str | None = None) -> str:
    # expect_string overrides netmiko's default prompt auto-detection, which is
    # unreliable against a dynamic Linux shell PS1 (see _LINUX_PROMPT).
    kwargs: dict[str, Any] = {"read_timeout": _READ_TIMEOUT}
    if expect_string is not None:
        kwargs["expect_string"] = expect_string
    result = task.run(task=netmiko_send_command, command_string=command, **kwargs)
    return result[0].result


# ── Nokia SR Linux ───────────────────────────────────────────────────

_SRL_VERSION = "show version"
_SRL_INTERFACES = "info from state /interface * | as json"
_SRL_IPV4 = "info from state /interface * subinterface * ipv4 | as json"
_SRL_IPV6 = "info from state /interface * subinterface * ipv6 | as json"
_SRL_LLDP = "info from state /system lldp interface * neighbor * | as json"

# 'show version' banner label → NAPALM facts key
_SRL_FACT_LABELS = {
    "Hostname": "hostname",
    "Chassis Type": "model",
    "Serial Number": "serial_number",
    "Software Version": "os_version",
}

_SPEED_MULTIPLIERS = {"M": 1, "G": 1000, "T": 1_000_000}


def _srl_json(output: str, command: str) -> dict[str, Any]:
    """
    SR Linux answers an unknown path by printing "Parsing error: ..." or
    "Error: ..." on stdout and still exiting 0, so the command looks like it
    succeeded. Anything that isn't a JSON object is one of those messages.
    """
    text = output.strip()
    if not text.startswith("{"):
        raise ValueError(f"'{command}' returned no JSON: {text[:200]}")
    return json.loads(text)


def _srl_speed_mbps(port_speed: str | None) -> int:
    """'100G' → 100000. Mbps, matching what NAPALM reports."""
    if not port_speed:
        return 0
    match = re.fullmatch(r"(\d+)([MGT])", port_speed.strip().upper())
    if not match:
        return 0
    return int(match.group(1)) * _SPEED_MULTIPLIERS[match.group(2)]


def _srl_facts(output: str) -> dict[str, Any]:
    """Parse the 'Label : Value' banner emitted by 'show version'."""
    facts: dict[str, Any] = {
        "hostname": "",
        "model": "Unknown",
        "serial_number": "",
        "os_version": "",
        "vendor": "Nokia",
        "fqdn": "",
        "uptime": 0,
    }
    for line in output.splitlines():
        label, sep, value = line.partition(":")
        if not sep:
            continue
        key = _SRL_FACT_LABELS.get(label.strip())
        if key:
            facts[key] = value.strip()

    facts["fqdn"] = facts["hostname"]
    return facts


def _srl_interfaces(payload: dict[str, Any]) -> dict[str, Any]:
    interfaces: dict[str, Any] = {}
    for intf in payload.get("interface", []) or []:
        name = intf.get("name")
        if not name:
            continue
        ethernet = intf.get("ethernet") or {}
        interfaces[name] = {
            "is_enabled": intf.get("admin-state") == "enable",
            "is_up": intf.get("oper-state") == "up",
            # SR Linux emits JSON null, not "", for an unset description.
            "description": intf.get("description") or "",
            "mac_address": ethernet.get("hw-mac-address") or "",
            "mtu": intf.get("mtu") or 1500,
            "speed": _srl_speed_mbps(ethernet.get("port-speed")),
        }
    return interfaces


def _srl_addresses(payload: dict[str, Any], family: str) -> dict[str, Any]:
    """
    SR Linux carries addresses on subinterfaces (mgmt0.0, ethernet-1/1.0), but
    only the parent interfaces exist in NetBox — they are what /interface
    returns. Collapse each address onto its parent.

    IPv6 link-local is dropped: every interface has one, it is derived from the
    MAC, and it says nothing a NetBox user would want to read.
    """
    result: dict[str, Any] = {}
    for intf in payload.get("interface", []) or []:
        name = intf.get("name")
        if not name:
            continue
        for subinterface in intf.get("subinterface", []) or []:
            for entry in (subinterface.get(family) or {}).get("address", []) or []:
                prefix = entry.get("ip-prefix", "")
                address, sep, length = prefix.partition("/")
                if not sep:
                    continue
                if family == "ipv6" and address.lower().startswith("fe80:"):
                    continue
                result.setdefault(name, {}).setdefault(family, {})[address] = {
                    "prefix_length": int(length)
                }
    return result


def _srl_lldp(payload: dict[str, Any]) -> tuple[dict[str, list], dict[str, list]]:
    neighbors: dict[str, list] = {}
    details: dict[str, list] = {}

    lldp_interfaces = (
        payload.get("system", {}).get("lldp", {}).get("interface", []) or []
    )
    for intf in lldp_interfaces:
        local = intf.get("name")
        for neighbor in intf.get("neighbor", []) or []:
            remote = neighbor.get("system-name") or ""
            port = neighbor.get("port-id") or ""
            if not local or not remote or not port:
                continue
            neighbors.setdefault(local, []).append({"hostname": remote, "port": port})
            details.setdefault(local, []).append({
                "remote_system_name": remote,
                "remote_port_id": port,
                "remote_system_description": neighbor.get("system-description") or "",
                "remote_chassis_id": neighbor.get("chassis-id") or "",
                "remote_port_description": neighbor.get("port-description") or "",
            })

    return neighbors, details


def _collect_srlinux(task: Task) -> dict[str, Any]:
    hostname = task.host.name

    napalm_data: dict[str, Any] = {
        "facts": {},
        "interfaces": {},
        "interfaces_ip": {},
        "lldp_neighbors": {},
        "lldp_neighbors_detail": {},
    }

    napalm_data["facts"] = _srl_facts(_send(task, _SRL_VERSION))
    napalm_data["interfaces"] = _srl_interfaces(
        _srl_json(_send(task, _SRL_INTERFACES), _SRL_INTERFACES)
    )

    # An unaddressed or LLDP-less device answers these with an error banner
    # rather than an empty object, so a failure here is not a collection
    # failure.
    for family, command in (("ipv4", _SRL_IPV4), ("ipv6", _SRL_IPV6)):
        try:
            payload = _srl_json(_send(task, command), command)
        except Exception as e:
            logger.debug(f"[{hostname}] no {family} addresses: {e}")
            continue
        for name, families in _srl_addresses(payload, family).items():
            napalm_data["interfaces_ip"].setdefault(name, {}).update(families)

    try:
        payload = _srl_json(_send(task, _SRL_LLDP), _SRL_LLDP)
        neighbors, details = _srl_lldp(payload)
        napalm_data["lldp_neighbors"] = neighbors
        napalm_data["lldp_neighbors_detail"] = details
    except Exception as e:
        logger.debug(f"[{hostname}] no LLDP neighbors: {e}")

    facts = napalm_data["facts"]
    logger.info(
        f"[{hostname}] Netmiko OK: "
        f"hostname={facts.get('hostname', '?')}, "
        f"model={facts.get('model', '?')}, "
        f"{len(napalm_data['interfaces'])} interfaces, "
        f"{len(napalm_data['interfaces_ip'])} with IPs, "
        f"{len(napalm_data['lldp_neighbors'])} LLDP neighbors"
    )
    return napalm_data


# ── Cisco IOS-XR ─────────────────────────────────────────────────────

_XR_VERSION = "show version"
_XR_INTERFACES = "show interfaces"
_XR_IPV4 = "show ipv4 interface"
_XR_LLDP = "show lldp neighbors detail"
_XR_CDP = "show cdp neighbors detail"


def _send_textfsm(
    task: Task, command: str, expect_string: str | None = None
) -> list[dict[str, Any]]:
    """
    Run a command with Netmiko's ntc-templates parsing. Netmiko hands back the
    raw string when no template matches or nothing parses (including protocol
    disabled banners like '% LLDP is not enabled') — collapse that to [].
    """
    kwargs: dict[str, Any] = {"use_textfsm": True, "read_timeout": _READ_TIMEOUT}
    if expect_string is not None:
        kwargs["expect_string"] = expect_string
    result = task.run(task=netmiko_send_command, command_string=command, **kwargs)
    parsed = result[0].result
    if not isinstance(parsed, list):
        return []
    return _upper_keys(parsed)


def _prompt_hostname(task: Task) -> str:
    """
    Hostname from the CLI prompt, for platforms whose 'show version' carries none
    (IOS-XR, Arista EOS): 'RP/0/RP0/CPU0:core-rtr01#' → 'core-rtr01',
    'pe-emea-01#' → 'pe-emea-01'.
    """
    try:
        conn = task.host.get_connection("netmiko", task.nornir.config)
        prompt = conn.find_prompt().strip().rstrip("#> ")
        return prompt.rsplit(":", 1)[-1]
    except Exception:
        return ""


def _collect_iosxr(task: Task) -> dict[str, Any]:
    hostname = task.host.name

    napalm_data: dict[str, Any] = {
        "facts": {},
        "interfaces": {},
        "interfaces_ip": {},
        "lldp_neighbors": {},
        "lldp_neighbors_detail": {},
    }

    napalm_data["facts"] = _convert_facts(
        _send_textfsm(task, _XR_VERSION), "textfsm", "iosxr"
    )
    # Processing rejects a device without a hostname — recover it from the
    # prompt, which is the only place XR shows it.
    if not napalm_data["facts"].get("hostname"):
        prompt_hostname = _prompt_hostname(task)
        napalm_data["facts"]["hostname"] = prompt_hostname
        napalm_data["facts"]["fqdn"] = napalm_data["facts"].get("fqdn") or prompt_hostname

    napalm_data["interfaces"] = _convert_interfaces(
        _send_textfsm(task, _XR_INTERFACES), "textfsm"
    )
    napalm_data["interfaces_ip"] = _convert_interfaces_ip(
        _send_textfsm(task, _XR_IPV4), "textfsm"
    )

    # Neighbor discovery is best-effort: with the protocol disabled the router
    # answers '% LLDP is not enabled', which the ntc-templates parser turns
    # into a TextFSMError rather than an empty parse.
    try:
        lldp, details = _convert_lldp(_send_textfsm(task, _XR_LLDP), "textfsm")
    except Exception as e:
        logger.debug(f"[{hostname}] no LLDP neighbors: {e}")
        lldp, details = {}, {}
    napalm_data["lldp_neighbors"] = lldp
    napalm_data["lldp_neighbors_detail"] = details

    # Merge CDP into the LLDP dicts; LLDP wins on the same interface.
    try:
        cdp, cdp_details = _convert_cdp(_send_textfsm(task, _XR_CDP), "textfsm")
    except Exception as e:
        logger.debug(f"[{hostname}] no CDP neighbors: {e}")
        cdp, cdp_details = {}, {}
    for intf, nbrs in cdp.items():
        if intf not in napalm_data["lldp_neighbors"]:
            napalm_data["lldp_neighbors"][intf] = nbrs
            napalm_data["lldp_neighbors_detail"][intf] = cdp_details.get(intf, [])

    facts = napalm_data["facts"]
    logger.info(
        f"[{hostname}] Netmiko OK: "
        f"hostname={facts.get('hostname', '?')}, "
        f"model={facts.get('model', '?')}, "
        f"{len(napalm_data['interfaces'])} interfaces, "
        f"{len(napalm_data['interfaces_ip'])} with IPs, "
        f"{len(napalm_data['lldp_neighbors'])} LLDP neighbors"
    )
    return napalm_data


# ── Cisco IOS / Arista EOS / Cisco NX-OS ─────────────────────────────
#
# These already collect fine over NAPALM or Scrapli; the Netmiko path exists so
# a single `--collector netmiko` can cover a whole mixed inventory (e.g. a lab
# where NAPALM/Scrapli are blocked). Parsing is ntc-templates (TextFSM); the
# shared _convert_* helpers absorb the per-platform field-name differences.

_IOS_LIKE_COMMANDS = {
    "ios": {
        "version": "show version",
        "interfaces": "show interfaces",
        "lldp": "show lldp neighbors detail",
        "cdp": "show cdp neighbors detail",
    },
    "eos": {
        "version": "show version",
        "interfaces": "show interfaces",
        "lldp": "show lldp neighbors detail",
        "cdp": None,  # Arista EOS does not run CDP
    },
    "nxos": {
        "version": "show version",
        "interfaces": "show interface",  # singular on NX-OS
        "lldp": "show lldp neighbors detail",
        "cdp": "show cdp neighbors detail",
    },
}


def _interfaces_ip_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Pull interface IPv4s straight out of the 'show interfaces' rows, so no extra
    command is needed (Arista ships no 'show ip interface' ntc-template at all).
    Handles both shapes the templates produce: a bare IP_ADDRESS plus a separate
    PREFIX_LENGTH (ios/nxos), or an IP_ADDRESS that already carries the mask —
    e.g. "10.0.0.1/24" (eos).
    """
    interfaces_ip: dict[str, Any] = {}
    for row in rows:
        name = row.get("INTERFACE", "")
        ip = row.get("IP_ADDRESS", "")
        if isinstance(ip, list):
            ip = ip[0] if ip else ""
        if not name or not ip or str(ip).lower() == "unassigned":
            continue
        address, _, embedded = str(ip).partition("/")
        raw_len = embedded or row.get("PREFIX_LENGTH", "") or "32"
        try:
            prefix_len = int(raw_len)
        except (ValueError, TypeError):
            prefix_len = 32
        interfaces_ip.setdefault(name, {}).setdefault("ipv4", {})[address] = {
            "prefix_length": prefix_len
        }
    return interfaces_ip


def _collect_ios_like(task: Task, platform: str) -> dict[str, Any]:
    """
    Collect ios/eos/nxos over Netmiko + ntc-templates, shaping the output like
    collect_napalm()/collect_scrapli(). Interface IPs come from the interfaces
    output itself (see _interfaces_ip_from_rows), not a separate command.
    """
    hostname = task.host.name
    commands = _IOS_LIKE_COMMANDS[platform]

    napalm_data: dict[str, Any] = {
        "facts": {},
        "interfaces": {},
        "interfaces_ip": {},
        "lldp_neighbors": {},
        "lldp_neighbors_detail": {},
    }

    napalm_data["facts"] = _convert_facts(
        _send_textfsm(task, commands["version"]), "textfsm", platform
    )
    # Arista 'show version' carries no hostname; recover it from the prompt, since
    # processing rejects a device without one.
    if not napalm_data["facts"].get("hostname"):
        prompt_hostname = _prompt_hostname(task)
        napalm_data["facts"]["hostname"] = prompt_hostname
        napalm_data["facts"]["fqdn"] = napalm_data["facts"].get("fqdn") or prompt_hostname

    interface_rows = _send_textfsm(task, commands["interfaces"])
    napalm_data["interfaces"] = _convert_interfaces(interface_rows, "textfsm")
    napalm_data["interfaces_ip"] = _interfaces_ip_from_rows(interface_rows)

    # Neighbor discovery is best-effort: a disabled protocol makes the parser
    # raise rather than return an empty list.
    try:
        lldp, details = _convert_lldp(_send_textfsm(task, commands["lldp"]), "textfsm")
    except Exception as e:
        logger.debug(f"[{hostname}] no LLDP neighbors: {e}")
        lldp, details = {}, {}
    napalm_data["lldp_neighbors"] = lldp
    napalm_data["lldp_neighbors_detail"] = details

    # Merge CDP into the LLDP dicts (LLDP wins on the same interface), where the
    # platform runs it.
    if commands["cdp"]:
        try:
            cdp, cdp_details = _convert_cdp(
                _send_textfsm(task, commands["cdp"]), "textfsm"
            )
        except Exception as e:
            logger.debug(f"[{hostname}] no CDP neighbors: {e}")
            cdp, cdp_details = {}, {}
        for intf, nbrs in cdp.items():
            if intf not in napalm_data["lldp_neighbors"]:
                napalm_data["lldp_neighbors"][intf] = nbrs
                napalm_data["lldp_neighbors_detail"][intf] = cdp_details.get(intf, [])

    facts = napalm_data["facts"]
    logger.info(
        f"[{hostname}] Netmiko OK: "
        f"hostname={facts.get('hostname', '?')}, "
        f"model={facts.get('model', '?')}, "
        f"{len(napalm_data['interfaces'])} interfaces, "
        f"{len(napalm_data['interfaces_ip'])} with IPs, "
        f"{len(napalm_data['lldp_neighbors'])} LLDP neighbors"
    )
    return napalm_data


# ── Linux servers ────────────────────────────────────────────────────
#
# Netmiko device_type ``linux``. NAPALM and Scrapli have no Linux driver, so
# everything comes over a plain SSH shell:
#   - facts        raw commands (hostname, /etc/os-release, DMI sysfs, uptime),
#                  parsed minimally here (ntc-templates has no facts template)
#   - interfaces   ``ip address show`` parsed with ntc-templates
#                  (linux_ip_address_show.textfsm) — one command carries both the
#                  links and their IPv4/IPv6 addresses.
# No LLDP: ntc-templates ships no Linux neighbour template, so a Linux host is
# discovered as device + interfaces + IPs, never as a cable endpoint.

_LINUX_IP_ADDR = "ip address show"

# Stop reading at the shell prompt terminator ($ for a user, # for root),
# anchored to end-of-line. netmiko's default prompt auto-detection re.escapes the
# literal PS1 it captured at login, which fails the moment the prompt is dynamic
# (cwd, timestamp, git branch, exit-status colour) — the ReadTimeout that makes
# the first command fail even though auth succeeded. LinuxSSH strips ANSI codes
# before matching, so this holds up against a colourised prompt.
_LINUX_PROMPT = r"[#$]\s*$"

# PRETTY_NAME="Ubuntu 22.04.4 LTS" in /etc/os-release.
_OS_PRETTY_RE = re.compile(r'^PRETTY_NAME=(.*)$', re.MULTILINE)

# Placeholders motherboards report through DMI when a field was never programmed
# (common on VMs and white-box hardware) — treated as "unknown", not real data.
_DMI_PLACEHOLDERS = {
    "", "not specified", "none", "default string", "system serial number",
    "system product name", "system manufacturer", "to be filled by o.e.m.",
    "o.e.m.", "not applicable", "unknown",
}

# Linux interface-name prefixes → NetBox interface type. Physical NICs (eth0,
# ens1f0, eno1, enp3s0, ...) match none of these and fall through to "other":
# ``ip`` reports no media/speed, so a specific base-T slug would be a guess.
_LINUX_BOND_PREFIXES = ("bond", "team")
_LINUX_BRIDGE_PREFIXES = ("br", "virbr", "docker", "cni", "cbr", "ovs")
_LINUX_VIRTUAL_PREFIXES = (
    "vlan", "dummy", "gre", "gretap", "tun", "tap", "sit", "ip6tnl",
    "vxlan", "wg", "ifb", "veth", "nlmon", "macvlan", "ipvlan",
)


def _send_raw(task: Task, command: str) -> str:
    """
    Run a shell command and return cleaned single-line stdout, or "" when it
    failed (missing file, permission denied). Used for the raw fact commands
    that have no ntc-template.
    """
    out = (_send(task, command, expect_string=_LINUX_PROMPT) or "").strip()
    low = out.lower()
    if not out or "permission denied" in low or "no such file" in low or "cannot open" in low:
        return ""
    return out.splitlines()[0].strip()


def _linux_facts(task: Task) -> dict[str, Any]:
    """
    Assemble NAPALM-shaped facts for a Linux host from raw commands, since
    ntc-templates has no Linux facts template. Hardware fields come from DMI
    sysfs and are best-effort: product_serial usually needs root and comes back
    empty, and VMs report placeholder strings that are normalised away.
    """
    hostname = _send_raw(task, "hostname") or _send_raw(task, "uname -n")

    os_version = ""
    os_release = _send(task, "cat /etc/os-release 2>/dev/null", expect_string=_LINUX_PROMPT) or ""
    match = _OS_PRETTY_RE.search(os_release)
    if match:
        os_version = match.group(1).strip().strip('"')
    if not os_version:
        os_version = _send_raw(task, "uname -sr")

    vendor = _send_raw(task, "cat /sys/class/dmi/id/sys_vendor 2>/dev/null")
    model = _send_raw(task, "cat /sys/class/dmi/id/product_name 2>/dev/null")
    serial = _send_raw(task, "cat /sys/class/dmi/id/product_serial 2>/dev/null")

    if vendor.lower() in _DMI_PLACEHOLDERS:
        vendor = "Linux"
    if model.lower() in _DMI_PLACEHOLDERS:
        model = "Unknown"
    if serial.lower() in _DMI_PLACEHOLDERS:
        serial = ""

    uptime = 0
    proc_uptime = _send_raw(task, "cat /proc/uptime 2>/dev/null")
    if proc_uptime:
        try:
            uptime = int(float(proc_uptime.split()[0]))
        except (ValueError, IndexError):
            uptime = 0

    return {
        "hostname": hostname,
        "model": model or "Unknown",
        "serial_number": serial,
        "os_version": os_version or "Linux",
        "vendor": vendor or "Linux",
        "fqdn": hostname,
        "uptime": uptime,
    }


def _linux_interface_type(name: str, tmpl_type: str) -> str:
    """Map a Linux interface (name + `ip` link type) to a NetBox type slug."""
    lname = name.lower()
    if tmpl_type == "loopback" or lname == "lo":
        return "virtual"
    if "." in name:                       # VLAN sub-interface, e.g. eth0.100
        return "virtual"
    if lname.startswith(_LINUX_BOND_PREFIXES):
        return "lag"
    if lname.startswith(_LINUX_BRIDGE_PREFIXES):
        return "bridge"
    if lname.startswith(_LINUX_VIRTUAL_PREFIXES) or lname.endswith("-vrf"):
        return "virtual"
    return "other"


def _convert_linux_interfaces(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Turn parsed `ip address show` rows into NAPALM-shaped interfaces and
    interfaces_ip dicts. One command carries both, so they are built together.

    Admin state is the IFF_UP flag; operational state is STATE==UP (or LOWER_UP,
    which is how a loopback in state UNKNOWN still reads as up). Each interface
    also carries an explicit NetBox type (`ip` gives us the link type, unlike the
    Cisco name heuristics) so process.py doesn't have to guess from the name.
    IPv6 link-local (fe80::/10) is dropped: every link has one and it says
    nothing a NetBox user would read.
    """
    interfaces: dict[str, Any] = {}
    interfaces_ip: dict[str, Any] = {}

    for row in rows:
        name = row.get("INTERFACE", "")
        if not name:
            continue
        # `ip` shows stacked/virtual links as "eth0.100@eth0" / "veth7@if5":
        # the part before '@' is the real name, the rest just names the parent.
        name = name.split("@", 1)[0]

        flags = (row.get("FLAGS", "") or "").split(",")
        state = (row.get("STATE", "") or "").upper()
        tmpl_type = (row.get("TYPE", "") or "").lower()

        mac = row.get("MAC_ADDRESS", "") or ""
        if not mac.replace(":", "").strip("0"):   # all-zero MAC (loopback) → none
            mac = ""

        try:
            mtu = int(row.get("MTU", 1500) or 1500)
        except (ValueError, TypeError):
            mtu = 1500

        nb_type = _linux_interface_type(name, tmpl_type)
        interfaces[name] = {
            "is_enabled": "UP" in flags,
            "is_up": state == "UP" or "LOWER_UP" in flags,
            "description": "",
            "mac_address": mac,
            "mtu": mtu,
            "speed": 0,               # `ip` reports no speed; needs ethtool
            "netbox_type": nb_type,
            "is_virtual": nb_type in ("virtual", "lag", "bridge"),
        }

        entry: dict[str, Any] = {}
        # Addresses and masks are parallel captures from the same template row,
        # so they normally pair up one to one. strict=False on purpose: if the
        # template missed a mask, dropping the unpaired tail costs one address,
        # while raising would cost the whole device.
        ipv4 = {
            addr: {"prefix_length": _int_or(mask, 32)}
            for addr, mask in zip(
                row.get("IP_ADDRESSES", []) or [], row.get("IP_MASKS", []) or [],
                strict=False,
            )
        }
        if ipv4:
            entry["ipv4"] = ipv4
        ipv6 = {
            addr: {"prefix_length": _int_or(mask, 128)}
            for addr, mask in zip(
                row.get("IPV6_ADDRESSES", []) or [], row.get("IPV6_MASKS", []) or [],
                strict=False,
            )
            if not addr.lower().startswith("fe80:")
        }
        if ipv6:
            entry["ipv6"] = ipv6
        if entry:
            interfaces_ip[name] = entry

    return interfaces, interfaces_ip


def _int_or(value: Any, default: int) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _collect_linux(task: Task) -> dict[str, Any]:
    hostname = task.host.name

    napalm_data: dict[str, Any] = {
        "facts": {},
        "interfaces": {},
        "interfaces_ip": {},
        "lldp_neighbors": {},
        "lldp_neighbors_detail": {},
    }

    napalm_data["facts"] = _linux_facts(task)
    interfaces, interfaces_ip = _convert_linux_interfaces(
        _send_textfsm(task, _LINUX_IP_ADDR, expect_string=_LINUX_PROMPT)
    )
    napalm_data["interfaces"] = interfaces
    napalm_data["interfaces_ip"] = interfaces_ip

    facts = napalm_data["facts"]
    logger.info(
        f"[{hostname}] Netmiko OK: "
        f"hostname={facts.get('hostname', '?')}, "
        f"model={facts.get('model', '?')}, "
        f"{len(napalm_data['interfaces'])} interfaces, "
        f"{len(napalm_data['interfaces_ip'])} with IPs"
    )
    return napalm_data


_COLLECTORS = {
    "srlinux": _collect_srlinux,
    "iosxr": _collect_iosxr,
    "ios": lambda task: _collect_ios_like(task, "ios"),
    "eos": lambda task: _collect_ios_like(task, "eos"),
    "nxos": lambda task: _collect_ios_like(task, "nxos"),
    "linux": _collect_linux,
}

# Platforms this module used to dispatch, now collector plugins of their own
# (net2sot/collectors/). Named here so an inventory that still pins
# `collector: netmiko` for one of them gets told where its collector went
# instead of "no Netmiko collector for platform 'f5'", which is true but
# unhelpful -- the platform is still supported, just not from here.
_MOVED_TO_PLUGINS = {
    "paloalto": "paloalto",
    "f5": "f5",
}
