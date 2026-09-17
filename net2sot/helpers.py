"""
Helper utilities for network discovery - interface normalization, device type mapping, etc.

Single source of truth for how a device is represented in NetBox: every task in
the pipeline normalizes through these functions, so a name, MAC or device type
looks the same no matter which collector produced it.
"""

from __future__ import annotations

import re

CANONICAL_INTERFACE_MAP = {
    "mgmt": "Management",
    "mgt": "Management",
    "eth": "Ethernet",
    "et": "Ethernet",
    "fa": "FastEthernet",
    "ge": "GigabitEthernet",
    "gi": "GigabitEthernet",
    "te": "TenGigabitEthernet",
    "xe": "TenGigabitEthernet",
    "tw": "TwentyFiveGigE",
    "fo": "FortyGigabitEthernet",
    "hu": "HundredGigabitEthernet",
    "po": "Port-channel",
    "port-channel": "Port-channel",
    "portchannel": "Port-channel",
    "pc": "Port-channel",
    "lo": "Loopback",
    "vl": "Vlan",
    "vlan": "Vlan",
    "tun": "Tunnel",
    "tu": "Tunnel",
    "br": "Bridge",
}


def normalize_interface_name(interface_name: str) -> str:
    if not interface_name:
        return interface_name

    original = interface_name.strip()

    patterns = []
    for abbr, canonical in CANONICAL_INTERFACE_MAP.items():
        pattern = rf"(^|[^a-zA-Z]){re.escape(abbr)}([^a-zA-Z]|$)"
        patterns.append((pattern, canonical))

    patterns.sort(key=lambda x: len(x[0]), reverse=True)

    result = original
    for pattern, replacement in patterns:
        def repl(match, _replacement=replacement):
            return match.group(1) + _replacement + match.group(2)

        new_result = re.sub(pattern, repl, result, count=1, flags=re.IGNORECASE)
        if new_result != result:
            result = new_result
            break

    # No .capitalize() here: it would lowercase everything past the first
    # character and undo the canonical casing this function just applied
    # ("TenGigabitEthernet0/1" -> "Tengigabitethernet0/1"). Names that match no
    # abbreviation are already the device's own spelling and are left alone.
    return result


# Values a device reports when it is not actually telling us its model.
_UNIDENTIFIED_MODELS = {"", "unknown", "none", "n/a", "null"}


def determine_device_type(model: str, network_os: str, device_type_mapping: dict) -> str:
    """
    Resolve the NetBox device type for a discovered device, in order:

      1. A curated pattern from device_type_mapping, so a site can pin an exact
         NetBox device-type slug it already maintains.
      2. The model the device reported. Anything the device actually identified
         is worth creating in NetBox as itself -- collapsing an ISR4331 and a
         Nexus 9000 into "cisco-generic" throws away the one fact we went and
         asked the device for.
      3. Only when the device told us nothing usable, the vendor default.
    """
    vendor_mapping = device_type_mapping.get((network_os or "").lower(), {})

    for pattern_config in vendor_mapping.get("patterns", []):
        if re.search(pattern_config["regex"], model or "", re.IGNORECASE):
            return pattern_config["type"]

    if (model or "").strip().lower() not in _UNIDENTIFIED_MODELS:
        return model.strip()

    return vendor_mapping.get("default", "unknown")


# Canonical interface-name prefix → NetBox interface type slug.
#
# Anchored prefixes, not substring tests: "TenGigabitEthernet0/1" contains the
# substring "gigabit", so an unanchored check types every 10/40/100G port as
# 1000base-t and never reaches its own branch.
#
# The media guesses here assume copper; if your 10G ports are SFP+ rather than
# RJ45, change "10gbase-t" to "10gbase-x-sfpp". A name alone cannot tell us.
_INTERFACE_TYPE_PATTERNS: list[tuple[str, str]] = [
    (r"Port-channel", "lag"),
    (r"(?:Loopback|Vlan|Tunnel|Nve|Vxlan|Bridge|Null)", "virtual"),
    (r"(?:Management|Mgmt|Mgt|Ma\d|Fxp\d|Em\d)", "1000base-t"),
    (r"HundredGigabitEthernet", "100gbase-x-qsfp28"),
    (r"FortyGigabitEthernet", "40gbase-x-qsfpp"),
    (r"TwentyFiveGigE", "25gbase-x-sfp28"),
    (r"TenGigabitEthernet", "10gbase-t"),
    (r"GigabitEthernet", "1000base-t"),
    (r"FastEthernet", "100base-tx"),
    (r"Ethernet", "1000base-t"),
]


def extract_interface_type(interface_name: str) -> str:
    """
    Map a *canonical* interface name (as returned by normalize_interface_name)
    to a NetBox interface type slug.
    """
    if not interface_name:
        return "other"

    # A dot means a sub-interface regardless of the parent's media.
    if "." in interface_name:
        return "virtual"

    for pattern, netbox_type in _INTERFACE_TYPE_PATTERNS:
        if re.match(pattern, interface_name, re.IGNORECASE):
            return netbox_type

    return "other"


def is_virtual_interface(interface_name: str) -> bool:
    if not interface_name:
        return False
    if "." in interface_name:
        return True
    virtual_patterns = [
        r"^loopback", r"^vlan", r"^tunnel", r"^nve", r"^vxlan", r"^bridge", r"^null"
    ]
    return any(re.match(p, interface_name, re.IGNORECASE) for p in virtual_patterns)


def is_management_interface(interface_name: str) -> bool:
    if not interface_name:
        return False
    # Matched case-insensitively: IOS-XR spells it "MgmtEth0/RP0/CPU0/0", which
    # a case-sensitive "^mgmt" misses. Getting this wrong costs the device its
    # primary IP and invents LLDP cables across the OOB network.
    management_patterns = [
        r"management", r"mgmt", r"mgt", r"ma\d+", r"fxp\d+", r"em\d+",
    ]
    return any(re.match(p, interface_name, re.IGNORECASE) for p in management_patterns)


def should_include_interface(interface_name: str, interface_filters: dict) -> bool:
    if not interface_name:
        return False

    for pattern in interface_filters.get("exclude_patterns", []):
        if re.match(pattern, interface_name, re.IGNORECASE):
            return False

    include_patterns = interface_filters.get("include_patterns", [])
    if include_patterns:
        if not any(re.match(p, interface_name, re.IGNORECASE) for p in include_patterns):
            return False

    return True


def normalize_mac_address(mac_address: str) -> str | None:
    if not mac_address:
        return None

    mac_clean = re.sub(r"[:\-\.]", "", mac_address.lower())
    if not re.match(r"^[0-9a-f]{12}$", mac_clean):
        return None

    return ":".join(mac_clean[i : i + 2] for i in range(0, 12, 2))


def enhance_lldp_with_details(lldp_neighbors: dict, lldp_details: dict) -> list[dict]:
    if not lldp_neighbors or not lldp_details:
        return []

    enhanced = []
    for local_intf, neighbors in lldp_neighbors.items():
        for neighbor in neighbors:
            entry = {
                "local_interface": local_intf,
                "remote_hostname": neighbor.get("hostname", "").upper().split(".")[0],
                "remote_port": normalize_interface_name(neighbor.get("port", "")),
            }
            if local_intf in lldp_details and lldp_details[local_intf]:
                detail = lldp_details[local_intf][0]
                entry.update({
                    "remote_system_description": detail.get("remote_system_description", ""),
                    "remote_chassis_id": detail.get("remote_chassis_id", ""),
                    "remote_port_id": detail.get("remote_port_id", ""),
                    "remote_port_description": detail.get("remote_port_description", ""),
                })
            enhanced.append(entry)

    return enhanced


def extract_vendor_from_os(network_os: str) -> str:
    """Map network_os/platform to vendor name."""
    os_lower = network_os.lower()
    # Checked first: "panos" would otherwise have to be kept clear of every
    # substring test below as they grow.
    if "palo" in os_lower or "panos" in os_lower:
        return "Palo Alto Networks"
    if "f5" in os_lower or "tmos" in os_lower or "bigip" in os_lower:
        return "F5 Networks"
    if "ios" in os_lower or "nxos" in os_lower:
        return "Cisco"
    elif "eos" in os_lower:
        return "Arista"
    elif "junos" in os_lower:
        return "Juniper"
    elif "srl" in os_lower:
        return "Nokia"
    return "Unknown"


def process_interfaces(napalm_interfaces: dict, napalm_interfaces_ip: dict,
                       interface_filters: dict, exclude_disabled: bool = True) -> tuple[list[dict], list[dict]]:
    """
    Process raw NAPALM interface data into structured lists for NetBox.
    Returns (interfaces, ip_addresses).
    """
    interfaces = []
    ip_addresses = []

    for name, data in napalm_interfaces.items():
        normalized_name = normalize_interface_name(name)

        if not should_include_interface(normalized_name, interface_filters):
            continue
        if exclude_disabled and not data.get("is_enabled", True):
            continue

        intf = {
            "name": normalized_name,
            "original_name": name,
            "type": extract_interface_type(normalized_name),
            "enabled": data.get("is_enabled", True),
            "mtu": data.get("mtu", 0) or None,
            "mac_address": normalize_mac_address(data.get("mac_address", "")),
            "description": data.get("description", ""),
            "speed": data.get("speed", 0) or None,
            "is_virtual": is_virtual_interface(normalized_name),
            "is_management": is_management_interface(normalized_name),
        }

        # Detect sub-interface parent
        if "." in normalized_name:
            intf["parent"] = normalized_name.split(".")[0]

        interfaces.append(intf)

    # Process IP addresses
    for name, families in (napalm_interfaces_ip or {}).items():
        normalized_name = normalize_interface_name(name)
        for family, addrs in families.items():
            for addr, info in addrs.items():
                prefix_length = info.get("prefix_length", 32 if family == "ipv4" else 128)
                ip_addresses.append({
                    "address": f"{addr}/{prefix_length}",
                    "interface": normalized_name,
                    "family": family,
                    "role": "loopback" if is_virtual_interface(normalized_name) and "loopback" in normalized_name.lower() else "",
                    "is_management": is_management_interface(normalized_name),
                })

    return interfaces, ip_addresses
