"""
collectors/f5.py - F5 BIG-IP (TMOS) appliances, as a collector plugin.

Netmiko device_type ``f5_tmsh``. There is no NAPALM F5 driver on PyPI at all and
ntc-templates ships no F5 templates, so every command is parsed here. tmsh helps:
most of what we need is available as brace blocks, either natively (``list ...``)
or by asking a ``show`` command for ``field-fmt``, so one parser (_tmsh_blocks)
covers hardware, interfaces, VLANs and addresses.

  - facts        ``show sys version`` (the one command with no field-fmt, so its
                 label/value lines are read directly) plus
                 ``show sys hardware field-fmt`` and the configured hostname.
  - interfaces   ``list net interface`` (MAC, media, MTU) merged with
                 ``show net interface field-fmt`` (link status), plus each VLAN
                 as a virtual interface for its self-IPs to sit on.
  - IPs          ``list net self`` (self-IPs, which belong to a VLAN rather than
                 to a port) and ``list sys management-ip``.

No LLDP: an unlicensed VE reports none, and rather than guess at a format this
platform is synced as device + interfaces + IPs, never as a cable endpoint --
the same deal as Linux. Interface names (1.1, mgmt, VLAN names) are stored
verbatim; canonicalizing would turn "mgmt" into a "Management" port the appliance
does not have. See VERBATIM_INTERFACE_PLATFORMS in discovery.py.

This was part of tasks/collect_netmiko.py until it became a plugin. Nothing about
the parsing changed in the move; what changed is that a vendor now owns a file,
declares the platform it claims, and reaches the transport through the public
helpers in collectors/netmiko_support.py -- which is the shape a plugin from
another repository takes. See docs/plugins.md.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from typing import Any

from net2sot.collectors.netmiko_support import send
from net2sot.plugins import CollectContext, Collector
from net2sot.schemas import CollectedFacts

logger = logging.getLogger("discovery.collect.f5")

_VERSION = "show sys version"
_HARDWARE = "show sys hardware field-fmt"
_HOSTNAME = "list sys global-settings hostname"
_INTERFACES = "list net interface"
_INTERFACE_STATUS = "show net interface field-fmt"
_VLANS = "list net vlan"
_SELFS = "list net self"
_MGMT_IP = "list sys management-ip"

# BIG-IP's own name for the management port, which is also what
# helpers.is_management_interface() matches -- the match that makes its address
# the device's primary IP in NetBox.
_MGMT_INTERFACE = "mgmt"

# What tmsh prints for a field that was never set ('reg-key -').
_NA_VALUES = {"", "-", "none", "null", "unknown", "n/a"}

# 'Product     BIG-IP' / 'Version     17.5.0' in 'show sys version', which is
# the only command here that has no field-fmt form.
_VERSION_FIELD_RE = re.compile(r"^\s+(\S+)\s{2,}(\S.*?)\s*$")

# Media reads '<speed><medium>-<duplex>': 10000T-FD, 1000SX-FD, 100TX-FD. The
# digits are Mbps and the letters the medium, T/TX being twisted pair -- so
# unlike most platforms F5 tells us copper vs fibre instead of leaving it to a
# name heuristic.
_MEDIA_RE = re.compile(r"^(\d+)([A-Za-z]*)")

_COPPER_TYPES = {
    100: "100base-tx",
    1000: "1000base-t",
    10000: "10gbase-t",
}
_FIBER_TYPES = {
    1000: "1000base-x-sfp",
    10000: "10gbase-x-sfpp",
    25000: "25gbase-x-sfp28",
    40000: "40gbase-x-qsfpp",
    100000: "100gbase-x-qsfp28",
}


def _tmsh_blocks(output: str) -> dict[str, Any]:
    """
    Parse tmsh brace output into nested dicts.

        net vlan /Common/internal {          {"net vlan /Common/internal": {
            interfaces {                         "interfaces": {"1.2": {"tagged": ""}},
                1.2 { tagged }                   "tag": "4093",
            }                                }}
            tag 4093
        }

    Each block becomes a dict keyed by its full header line (the header carries
    the object's name, e.g. 'net interface 1.1'), each 'key value' line a string
    entry, and each bare word -- tmsh's way of writing a flag, like 'disabled'
    or 'tagged' -- an entry with an empty value.
    """
    root: dict[str, Any] = {}
    stack: list[dict[str, Any]] = [root]

    for raw in (output or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("}"):
            if len(stack) > 1:
                stack.pop()
            continue
        if line.endswith("{"):
            child: dict[str, Any] = {}
            stack[-1][line[:-1].strip()] = child
            stack.append(child)
            continue
        # A one-line block ('1.2 { }') closes on the same line it opened.
        inline = re.fullmatch(r"(.+?)\s*\{\s*(.*?)\s*\}", line)
        if inline:
            body: dict[str, Any] = {}
            if inline.group(2):
                body[inline.group(2)] = ""
            stack[-1][inline.group(1).strip()] = body
            continue
        key, _, value = line.partition(" ")
        stack[-1][key.strip()] = value.strip()

    return root


def _body(blocks: dict[str, Any], prefix: str) -> dict[str, Any]:
    """The body of the first block whose header starts with `prefix`, else {}."""
    for header, body in blocks.items():
        if header.startswith(prefix) and isinstance(body, dict):
            return body
    return {}


def _strip_partition(name: str) -> str:
    """
    '/Common/internal' → 'internal'.

    tmsh qualifies a configuration object with the partition it lives in, which
    is not the name an operator uses, nor the one a self-IP's `vlan` pointer has
    to be matched against. Only a real partition path (one that starts at the
    root) is stripped, so a name that merely contains a slash -- a management
    address carrying its prefix length, '10.10.10.111/24' -- survives intact.
    """
    name = name.strip()
    return name.rsplit("/", 1)[-1] if name.startswith("/") else name


def _named(blocks: dict[str, Any], prefix: str) -> dict[str, dict[str, Any]]:
    """
    {object name: body} for every block headed `<prefix> <name>`, e.g.
    'net interface 1.1' under the prefix 'net interface'.
    """
    found: dict[str, dict[str, Any]] = {}
    for header, body in blocks.items():
        if not header.startswith(prefix + " ") or not isinstance(body, dict):
            continue
        name = _strip_partition(header[len(prefix):])
        if name:
            found[name] = body
    return found


def _clean(value: Any) -> str:
    """Trim a tmsh value, treating its 'never set' placeholders as empty."""
    text = str(value or "").strip()
    return "" if text.lower() in _NA_VALUES else text


def _media_speed(*media: str) -> tuple[int, str]:
    """
    (Mbps, medium code) from the first media string that carries a speed:
    '10000T-FD' → (10000, 'T'), 'none' → (0, ''). Several are tried because a
    port that has never come up reports media-active 'none' while its
    media-fixed still says what it is wired for.
    """
    for value in media:
        match = _MEDIA_RE.match(_clean(value))
        if match:
            return int(match.group(1)), match.group(2).upper()
    return 0, ""


def _interface_type(speed: int, medium: str) -> str:
    """NetBox interface type from the speed and medium F5 reports."""
    if speed <= 0:
        return "other"
    if medium.startswith("T"):                    # T, TX -> twisted pair
        return _COPPER_TYPES.get(speed) or _FIBER_TYPES.get(speed, "other")
    return _FIBER_TYPES.get(speed) or _COPPER_TYPES.get(speed, "other")


def _facts(version_output: str, hardware_output: str, hostname_output: str) -> dict[str, Any]:
    """
    NAPALM-shaped facts from the three fact commands.

    Uptime stays 0: tmsh has no 'show sys uptime', and the value is not worth a
    second connection through bash to read /proc/uptime.
    """
    fields: dict[str, str] = {}
    for line in (version_output or "").splitlines():
        match = _VERSION_FIELD_RE.match(line)
        if match:
            fields.setdefault(match.group(1), match.group(2))

    hardware = _tmsh_blocks(hardware_output)
    platform = _body(hardware, "sys hardware platform")
    system = _body(hardware, "sys hardware system-info")

    hostname = _clean(
        _body(_tmsh_blocks(hostname_output), "sys global-settings").get("hostname")
    )

    return {
        # 'marketing-name' is the model an operator would recognise ("BIG-IP
        # Virtual Edition"); the system-info 'platform' code (Z100) and the
        # product name are the fallbacks.
        "model": (
            _clean(platform.get("marketing-name"))
            or _clean(system.get("platform"))
            or _clean(fields.get("Product"))
            or "Unknown"
        ),
        "serial_number": _clean(system.get("bigip-chassis-serial-num")),
        "os_version": _clean(fields.get("Version")),
        "vendor": "F5 Networks",
        "hostname": hostname,
        "fqdn": hostname,
        "uptime": 0,
    }


def _interfaces(
    list_output: str, status_output: str, vlan_output: str
) -> dict[str, Any]:
    """
    NAPALM-shaped interfaces: the physical ports, plus every VLAN as a virtual
    interface.

    The ports come from two commands because neither is complete on its own --
    'list net interface' holds the MAC, MTU and configured media, 'show net
    interface field-fmt' the link status and the media actually negotiated.

    VLANs are included because a BIG-IP puts its self-IPs on a VLAN rather than
    on a port; without them those addresses would have nothing in NetBox to
    attach to. Admin state follows tmsh's explicit 'disabled' flag, so a port
    that is merely dark ('uninit', the state of every unwired VE port) is still
    inventoried rather than dropped by exclude_disabled.
    """
    interfaces: dict[str, Any] = {}
    status_blocks = _named(_tmsh_blocks(status_output), "net interface")

    for name, body in _named(_tmsh_blocks(list_output), "net interface").items():
        status = status_blocks.get(name, {})
        speed, medium = _media_speed(
            body.get("media-fixed", ""),
            body.get("media-active", ""),
            status.get("media-active", ""),
        )
        try:
            mtu = int(_clean(body.get("mtu")) or 0)
        except ValueError:
            mtu = 0

        interfaces[name] = {
            "is_enabled": "disabled" not in body,
            "is_up": _clean(status.get("status")).lower() == "up",
            "description": _clean(body.get("description")),
            "mac_address": _clean(body.get("mac-address")),
            "mtu": mtu,
            "speed": speed,
            "netbox_type": _interface_type(speed, medium),
            "is_virtual": False,
        }

    for name, body in _named(_tmsh_blocks(vlan_output), "net vlan").items():
        try:
            mtu = int(_clean(body.get("mtu")) or 0)
        except ValueError:
            mtu = 0
        interfaces[name] = {
            "is_enabled": True,
            "is_up": True,
            "description": _clean(body.get("description")),
            "mac_address": "",
            "mtu": mtu,
            "speed": 0,
            "netbox_type": "virtual",
            "is_virtual": True,
        }

    return interfaces


def _address(value: str) -> tuple[str, str, int] | None:
    """
    ('10.1.1.5', 'ipv4', 24) from a tmsh address. A self-IP carries its route
    domain in the address ('10.1.1.5%1/24'), which is stripped -- NetBox models
    that as a VRF, and nothing here reads route domains. None if unparsable.
    """
    text = _clean(value)
    if not text:
        return None
    address, _, prefix = text.partition("/")
    address = address.split("%", 1)[0]
    try:
        parsed = ipaddress.ip_interface(f"{address}/{prefix}" if prefix else address)
    except ValueError:
        return None
    family = "ipv4" if parsed.version == 4 else "ipv6"
    return str(parsed.ip), family, parsed.network.prefixlen


def _interfaces_ip(self_output: str, mgmt_output: str) -> dict[str, Any]:
    """
    NAPALM-shaped interfaces_ip: each self-IP on the VLAN it belongs to, and the
    management address on the management port.
    """
    interfaces_ip: dict[str, Any] = {}

    def add(interface: str, value: str) -> None:
        parsed = _address(value)
        if not interface or parsed is None:
            return
        address, family, prefix_len = parsed
        interfaces_ip.setdefault(interface, {}).setdefault(family, {})[address] = {
            "prefix_length": prefix_len
        }

    for body in _named(_tmsh_blocks(self_output), "net self").values():
        add(_strip_partition(_clean(body.get("vlan"))), body.get("address", ""))

    # The address is in the block header ('sys management-ip 10.10.10.111/24'),
    # not in its body, so the name _named() extracted is the address itself.
    for address in _named(_tmsh_blocks(mgmt_output), "sys management-ip"):
        add(_MGMT_INTERFACE, address)

    return interfaces_ip


class F5Collector(Collector):
    """Collect a BIG-IP appliance over tmsh."""

    name = "f5"
    platforms = ("f5", "bigip", "tmos")
    description = "F5 BIG-IP / TMOS over SSH (tmsh brace blocks)"

    def collect(self, ctx: CollectContext) -> CollectedFacts:
        task, hostname = ctx.task, ctx.name
        facts = CollectedFacts()

        facts.facts = _facts(
            send(task, _VERSION),
            send(task, _HARDWARE),
            send(task, _HOSTNAME),
        )
        # The tmsh prompt carries the appliance's hostname, but only inside
        # '(cfg-sync Standalone)(NO LICENSE)' decorations, so an unset hostname
        # is reported rather than guessed at. require_hostname() below falls
        # back to the inventory name so the device is still syncable.
        if not facts.facts.hostname:
            logger.warning(f"[{hostname}] no hostname configured in sys global-settings")

        vlan_output = send(task, _VLANS)
        facts.interfaces = _interfaces(
            send(task, _INTERFACES), send(task, _INTERFACE_STATUS), vlan_output
        )
        facts.interfaces_ip = _interfaces_ip(
            send(task, _SELFS), send(task, _MGMT_IP)
        )
        # lldp_neighbors stays empty: no LLDP on this path, so cable creation
        # simply finds none.

        facts.require_hostname(fallback=hostname)
        logger.info(f"[{hostname}] BIG-IP collection done: {facts.summary()}")
        # The interface dicts above were built by hand, so hand back a model
        # whose nested values have actually been through validation.
        return facts.normalized()
