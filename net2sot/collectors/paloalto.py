"""
collectors/paloalto.py - Palo Alto PAN-OS firewalls, as a collector plugin.

Netmiko device_type ``paloalto_panos``. NAPALM's PAN-OS driver is the separate,
optional ``napalm-panos`` package and Scrapli's PAN-OS platform is a community
one, so this is the path a firewall takes on a plain install:

  - facts        ``show system info``, which also carries the management port's
                 address and MAC (see _management()).
  - interfaces   ``show interface all`` -- one command whose two sections
                 (hardware ports, logical interfaces) both parse from the same
                 ntc-template, so the addresses need no extra command.
  - LLDP         ``show lldp neighbors all``, best-effort.

PAN-OS interface names (ethernet1/1, ae1.100, loopback.1) are the device's own
identifiers, so discovery.py stores them verbatim instead of rewriting them into
Cisco spellings that do not exist on the firewall -- see
VERBATIM_INTERFACE_PLATFORMS there.

This was part of tasks/collect_netmiko.py until it became a plugin. Nothing
about the parsing changed in the move; what changed is that a vendor now owns a
file, declares the platform it claims, and reaches the transport through the
public helpers in collectors/netmiko_support.py -- which is the shape a plugin
from another repository takes. See docs/plugins.md.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from typing import Any

from net2sot.collectors.netmiko_support import (
    convert_facts,
    convert_lldp,
    ensure_net_textfsm,
    prompt_hostname,
    send_textfsm,
)
from net2sot.plugins import CollectContext, Collector
from net2sot.schemas import CollectedFacts

logger = logging.getLogger("discovery.collect.paloalto")

_SYSTEM_INFO = "show system info"
_INTERFACES = "show interface all"
_HARDWARE = "show interface hardware"
_LOGICAL = "show interface logical"
_LLDP = "show lldp neighbors all"

# PAN-OS keeps the management port off the dataplane, so 'show interface all'
# never lists it. It is rebuilt from 'show system info' under this name, which is
# what PAN-OS itself calls it and what helpers.is_management_interface() matches
# — that match is what makes its address the device's primary IP in NetBox.
_MGMT_INTERFACE = "management"

# What PAN-OS prints in a column that does not apply to the row: a logical
# interface has no speed/duplex, an unplugged port no negotiated speed.
_NA_VALUES = {"", "[n/a]", "n/a", "ukn", "unknown", "none"}

# Link speed (Mbps) → NetBox interface type. PAN-OS reports the negotiated speed
# but never the media, so this keeps the same copper-under-10G assumption as
# helpers._INTERFACE_TYPE_PATTERNS; change the values if your 1G ports are SFP.
_SPEED_TYPES = {
    100: "100base-tx",
    1000: "1000base-t",
    10000: "10gbase-x-sfpp",
    25000: "25gbase-x-sfp28",
    40000: "40gbase-x-qsfpp",
    100000: "100gbase-x-qsfp28",
}

# Logical interfaces PAN-OS names by function rather than by port position.
_VIRTUAL_PREFIXES = ("loopback", "tunnel", "vlan")

# Link states, as they read in the 'speed/duplex/state[/fec]' column.
_LINK_STATES = {"up", "down"}

# The 'uptime: 0 days, 2:50:27' field of 'show system info'.
_UPTIME_RE = re.compile(r"(?:(\d+)\s+days?,\s*)?(\d+):(\d+):(\d+)")


def _clean(value: Any) -> str:
    """
    Flatten a TextFSM List value to its first entry and drop the placeholders
    PAN-OS prints for an inapplicable column, so callers see "" rather than the
    literal '[n/a]'.
    """
    if isinstance(value, list):
        value = value[0] if value else ""
    text = str(value or "").strip()
    return "" if text.lower() in _NA_VALUES else text


def _speed_mbps(value: Any) -> int:
    """'1000' → 1000 (Mbps, as NAPALM reports speed). 0 when PAN-OS said '[n/a]'."""
    try:
        return int(_clean(value))
    except ValueError:
        return 0


def _uptime_seconds(value: Any) -> int:
    """'38 days, 2:11:15' → seconds. 0 when the field is absent or unparsable."""
    match = _UPTIME_RE.search(_clean(value))
    if not match:
        return 0
    days, hours, minutes, seconds = match.groups()
    return int(days or 0) * 86400 + int(hours) * 3600 + int(minutes) * 60 + int(seconds)


def _addresses(value: Any) -> list[str]:
    """
    The addresses of one interface row. 'show interface all' captures them as a
    List (an interface can hold several), 'show interface logical' as a single
    value, and an unnumbered interface reads 'N/A'.
    """
    candidates = value if isinstance(value, list) else [value]
    return [
        addr
        for addr in (str(c or "").strip() for c in candidates)
        if addr and addr.upper() != "N/A"
    ]


def _link_state(row: dict[str, Any]) -> str:
    """
    The link state out of the 'speed/duplex/state[/fec]' column, "" if the row
    carries none (a logical interface has no link).

    The 'show interface all' template splits that column into four values, which
    mis-slices what an unnegotiated port reports: '[n/a]/[n/a]/up' is torn into
    ('[n/a]', '[n', 'a]', 'up'), leaving the real state in the FEC value. So the
    state is whichever of the two actually reads as one -- a port that does
    report FEC ('10000/full/up/rs-fec') still has its state in STATE, and the
    narrower 'show interface hardware' template slices the same line correctly.
    """
    for key in ("STATE", "FEC"):
        value = _clean(row.get(key)).lower()
        if value in _LINK_STATES:
            return value
    return ""


def _interface_type(name: str, speed: int) -> str:
    """
    NetBox interface type for a PAN-OS interface name.

    PAN-OS names carry their own structure: 'ethernet1/1' is a physical port,
    'ae1' an aggregate, a '.<unit>' suffix marks a sub-interface, and
    loopback/tunnel/vlan are the logical interfaces. Only a physical port falls
    through to the speed → media guess.
    """
    lname = name.lower()
    if lname.startswith(_MGMT_INTERFACE):
        return "1000base-t"
    if lname.startswith(_VIRTUAL_PREFIXES) or "." in name:
        return "virtual"
    if re.fullmatch(r"ae\d+", lname):
        return "lag"
    return _SPEED_TYPES.get(speed, "other")


def _convert_interfaces(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Turn parsed PAN-OS interface rows into NAPALM-shaped interfaces and
    interfaces_ip dicts.

    A configured port appears in *both* sections of 'show interface all': the
    hardware row carries its MAC, speed and link state, the logical row its zone
    and addresses. Rows are therefore merged by interface name — overwriting
    would blank out whichever half arrived first.

    Admin state is reported as enabled for every interface: 'show interface all'
    gives the link state only (PAN-OS keeps the administrative state in the
    config, not in this output), and treating a dark port as disabled would make
    interface_filters.exclude_disabled drop every unpatched port on the firewall
    instead of inventorying it. The link state becomes the operational state.
    """
    interfaces: dict[str, Any] = {}
    interfaces_ip: dict[str, Any] = {}

    for row in rows:
        name = _clean(row.get("INTERFACE"))
        if not name:
            continue

        intf = interfaces.setdefault(name, {
            "is_enabled": True,
            "is_up": True,
            "description": "",
            "mac_address": "",
            "mtu": 0,          # not in this output; NetBox keeps its own value
            "speed": 0,
        })

        mac = _clean(row.get("MAC_ADDRESS"))
        if mac:
            intf["mac_address"] = mac
        speed = _speed_mbps(row.get("SPEED"))
        if speed:
            intf["speed"] = speed
        state = _link_state(row)
        if state:
            intf["is_up"] = state == "up"

        for address in _addresses(row.get("IP_ADDRESS")):
            try:
                parsed = ipaddress.ip_interface(address)
            except ValueError:
                logger.debug(f"Skipping unparsable address '{address}' on {name}")
                continue
            family = "ipv4" if parsed.version == 4 else "ipv6"
            interfaces_ip.setdefault(name, {}).setdefault(family, {})[str(parsed.ip)] = {
                "prefix_length": parsed.network.prefixlen
            }

    # Typed only once both rows of an interface have been seen, so a physical
    # port is typed from the speed its hardware row reported.
    for name, intf in interfaces.items():
        nb_type = _interface_type(name, intf["speed"])
        intf["netbox_type"] = nb_type
        intf["is_virtual"] = nb_type in ("virtual", "lag")

    return interfaces, interfaces_ip


def _management(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Rebuild the management port from 'show system info', which is the only place
    PAN-OS reports it. Without this the firewall lands in NetBox with no
    management interface and no primary IP — the very address the run reached it
    on would be missing from the device it belongs to.

    Returns (interface entry, addresses entry); both empty when system info
    carried neither an address nor a MAC.
    """
    row = rows[0] if rows else {}
    ip = _clean(row.get("IP_ADDRESS"))
    netmask = _clean(row.get("NETMASK"))
    mac = _clean(row.get("MAC_ADDRESS"))
    if not ip and not mac:
        return {}, {}

    interface = {
        "is_enabled": True,
        "is_up": True,
        "description": "",
        "mac_address": mac,
        "mtu": 0,
        "speed": 0,
        "netbox_type": "1000base-t",
        "is_virtual": False,
    }

    addresses: dict[str, Any] = {}
    if ip:
        # PAN-OS prints a dotted netmask ('255.255.255.0'); ip_interface takes
        # that as happily as a prefix length.
        try:
            parsed = ipaddress.ip_interface(f"{ip}/{netmask or 32}")
        except ValueError:
            logger.debug(f"Unparsable management address '{ip}/{netmask}'")
        else:
            addresses = {
                "ipv4": {str(parsed.ip): {"prefix_length": parsed.network.prefixlen}}
            }
    return interface, addresses


def _facts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """
    NAPALM-shaped facts from 'show system info'. _convert_facts already knows the
    field names its template uses (HOSTNAME / MODEL / SERIAL / OS); vendor and
    the human-readable uptime are PAN-OS-specific and filled in here.
    """
    facts = convert_facts(rows, "textfsm", "paloalto")
    facts["vendor"] = "Palo Alto Networks"
    facts["uptime"] = _uptime_seconds((rows[0] if rows else {}).get("UPTIME"))
    # An unlicensed VM firewall answers 'serial: unknown'; store nothing rather
    # than stamping the device in NetBox with the literal placeholder.
    facts["serial_number"] = _clean(facts.get("serial_number"))
    return facts


def _prompt_hostname(task: Any) -> str:
    """
    Hostname from the PAN-OS prompt ('admin@FW-DC1>' → 'FW-DC1'), for the case
    where 'show system info' did not parse. prompt_hostname strips the
    terminator; PAN-OS additionally prefixes the logged-in user.
    """
    return prompt_hostname(task).rsplit("@", 1)[-1]


class PaloAltoCollector(Collector):
    """Collect a PAN-OS firewall over Netmiko, parsing with ntc-templates."""

    name = "paloalto"
    platforms = ("paloalto", "panos")
    description = "Palo Alto PAN-OS over SSH (Netmiko + ntc-templates)"

    def collect(self, ctx: CollectContext) -> CollectedFacts:
        ensure_net_textfsm()
        task, hostname = ctx.task, ctx.name
        facts = CollectedFacts()

        system_rows = send_textfsm(task, _SYSTEM_INFO)
        facts.facts = _facts(system_rows)
        # PAN-OS prefixes its prompt with the logged-in user, so the inventory
        # name is a second-best fallback behind the prompt itself.
        if not facts.facts.hostname:
            facts.facts.hostname = _prompt_hostname(task)

        # The 'show interface all' template rejects any line it does not expect,
        # and netmiko answers a failed parse with the raw string (which
        # send_textfsm collapses to []). Fall back to the two narrower commands
        # -- whose templates simply ignore what they don't recognise -- rather
        # than syncing a firewall with no interfaces at all.
        rows = send_textfsm(task, _INTERFACES)
        if not rows:
            logger.debug(
                f"[{hostname}] '{_INTERFACES}' produced no rows; "
                f"falling back to '{_HARDWARE}' + '{_LOGICAL}'"
            )
            rows = send_textfsm(task, _HARDWARE) + send_textfsm(task, _LOGICAL)

        interfaces, interfaces_ip = _convert_interfaces(rows)

        mgmt_interface, mgmt_addresses = _management(system_rows)
        if mgmt_interface:
            # setdefault: if this PAN-OS build did list the management port among
            # the interfaces, what it reported there wins.
            interfaces.setdefault(_MGMT_INTERFACE, mgmt_interface)
            if mgmt_addresses:
                interfaces_ip.setdefault(_MGMT_INTERFACE, mgmt_addresses)

        facts.interfaces = interfaces
        facts.interfaces_ip = interfaces_ip

        # Neighbour discovery is best-effort: with LLDP off the firewall answers
        # a banner the parser turns into an error rather than an empty list.
        try:
            lldp, details = convert_lldp(send_textfsm(task, _LLDP), "textfsm")
        except Exception as e:
            logger.debug(f"[{hostname}] no LLDP neighbors: {e}")
            lldp, details = {}, {}
        facts.lldp_neighbors = lldp
        facts.lldp_neighbors_detail = details

        # Nothing downstream can create a device with no name; the inventory
        # name stands in as the last resort.
        facts.require_hostname(fallback=hostname)
        logger.info(f"[{hostname}] PAN-OS collection done: {facts.summary()}")
        # The interface dicts above were built by hand, so hand back a model
        # whose nested values have actually been through validation.
        return facts.normalized()
