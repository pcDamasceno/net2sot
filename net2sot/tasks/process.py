"""
process.py - Turn what a collector gathered into what a sink can write.

The middle of the pipeline, and the part a plugin author does not have to
re-implement: canonical interface names, filtering, device-type mapping, VRF
membership, primary-IP selection. It takes `CollectedFacts` (the collector
contract) and returns a `DiscoveryResult` (the sink contract) -- see
net2sot/schemas/.

Both models live in net2sot.schemas; they are re-exported here because
that is where they used to be defined, so existing imports keep resolving.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from net2sot.helpers import (
    determine_device_type,
    enhance_lldp_with_details,
    extract_interface_type,
    extract_vendor_from_os,
    is_management_interface,
    is_virtual_interface,
    normalize_interface_name,
    normalize_mac_address,
    should_include_interface,
)
from net2sot.schemas import (
    CollectedFacts,
    DiscoveredDevice,
    DiscoveredInterface,
    DiscoveredIP,
    DiscoveredLLDPNeighbor,
    DiscoveredVRF,
    DiscoveryResult,
    ProcessedData,
)

__all__ = [
    "process_facts",
    "process_napalm_data",
    "CollectedFacts",
    "DiscoveryResult",
    "ProcessedData",
    "DiscoveredDevice",
    "DiscoveredInterface",
    "DiscoveredIP",
    "DiscoveredVRF",
    "DiscoveredLLDPNeighbor",
]

logger = logging.getLogger("discovery.process")

# NAPALM/OpenConfig names for the global routing table (not a VRF). Interfaces
# in one of these keep vrf=None and their IPs land in the default VRF.
_DEFAULT_INSTANCE_NAMES = {"global", "default", "master"}

# RD values NAPALM reports for a VRF that has no real route distinguisher. Kept
# out of the payload so distinct VRFs don't all collide on the same fake RD in
# NetBox (which enforces RD as globally unique).
_EMPTY_RD_VALUES = {"", "0:0", "0", "none", "null"}


def _build_vrf_map(
    network_instances: dict,
    route_targets: dict | None = None,
    vrf_interfaces: dict | None = None,
    vrf_rds: dict | None = None,
) -> tuple[dict[str, str], list[DiscoveredVRF]]:
    """
    Turn NAPALM get_network_instances output into (normalized interface name →
    VRF name) plus the list of discovered VRFs. The global/default routing
    instance is skipped so its interfaces stay VRF-less; only named L3 instances
    become VRFs. Absent/empty input yields empty results, so a collector that
    didn't gather VRFs simply leaves every IP in the default VRF.

    `route_targets` is the optional {vrf_name: {"import": [...], "export": [...]}}
    map from the separate 'show vrf detail' collection; a VRF with no entry keeps
    empty target lists.

    `vrf_interfaces` ({vrf_name: [interface, ...]}) and `vrf_rds` ({vrf_name: rd})
    come from the same 'show vrf detail' output and are a *fallback* membership
    source: NAPALM stays authoritative, but on a platform whose driver reports no
    network_instances (IOS-XR ASR-9906) this is the only place a VRF and its
    interfaces come from, so they reach NetBox instead of collapsing into the
    default VRF.
    """
    route_targets = route_targets or {}
    vrf_interfaces = vrf_interfaces or {}
    vrf_rds = vrf_rds or {}
    interface_vrf: dict[str, str] = {}
    vrfs: list[DiscoveredVRF] = []
    for name, inst in (network_instances or {}).items():
        inst_type = (inst.get("type") or "").upper()
        if inst_type == "DEFAULT_INSTANCE" or name.lower() in _DEFAULT_INSTANCE_NAMES:
            continue
        rd = (inst.get("state") or {}).get("route_distinguisher") or ""
        if rd.strip().lower() in _EMPTY_RD_VALUES:
            rd = ""
        rts = route_targets.get(name, {})
        vrfs.append(DiscoveredVRF(
            name=name,
            rd=rd,
            type=inst_type,
            import_targets=list(rts.get("import", [])),
            export_targets=list(rts.get("export", [])),
        ))
        members = (inst.get("interfaces") or {}).get("interface") or {}
        for intf_name in members:
            interface_vrf[normalize_interface_name(intf_name)] = name

    # Fill VRFs/memberships NAPALM did not provide from the 'show vrf detail'
    # Interfaces: section. setdefault keeps NAPALM authoritative where it spoke.
    seen = {v.name for v in vrfs}
    for name, members in vrf_interfaces.items():
        if name.lower() in _DEFAULT_INSTANCE_NAMES:
            continue
        if name not in seen:
            rd = (vrf_rds.get(name) or "").strip()
            if rd.lower() in _EMPTY_RD_VALUES:
                rd = ""
            rts = route_targets.get(name, {})
            vrfs.append(DiscoveredVRF(
                name=name,
                rd=rd,
                import_targets=list(rts.get("import", [])),
                export_targets=list(rts.get("export", [])),
            ))
            seen.add(name)
        for intf_name in members:
            interface_vrf.setdefault(normalize_interface_name(intf_name), name)
    return interface_vrf, vrfs


def process_facts(
    facts: CollectedFacts,
    platform: str,
    interface_filters: dict | None = None,
    device_type_mapping: dict | None = None,
    exclude_disabled: bool = True,
    login_ip: str | None = None,
    normalize_names: bool = True,
    collector: str = "",
) -> DiscoveryResult:
    """
    Normalize one device's collected facts into the sink contract.

    `normalize_names` canonicalizes interface names to the NetBox/Cisco spelling
    (GigabitEthernet0/0, Loopback0). Turn it off for platforms whose own names
    are the real identifiers -- Linux kernel names (eth0, ens1f0, bond0), PAN-OS
    names, BIG-IP names -- which must be stored verbatim rather than rewritten
    into interfaces that don't exist on the device.

    An individual interface, address or neighbour that does not satisfy the
    contract is logged and skipped, not raised: one malformed line in a CLI
    parse should cost that one object, not the whole device. A device that ends
    up with nothing usable is caught by the caller via
    `DiscoveryResult.is_usable()`.
    """
    interface_filters = interface_filters or {}
    device_type_mapping = device_type_mapping or {}

    # Enforce the contract at the boundary instead of assuming it. A collector
    # that built its nested structures by mutating containers (the natural way
    # to write one) has un-validated dicts inside an otherwise valid model; this
    # is where they become models. See CollectedFacts.normalized().
    facts = facts.normalized()

    result = DiscoveryResult(
        login_ip=login_ip,
        platform=platform,
        collector=collector,
    )

    def _norm(name: str) -> str:
        return normalize_interface_name(name) if normalize_names else (name or "").strip()

    # _build_vrf_map and enhance_lldp_with_details predate the contract and work
    # on plain nested dicts; dumping by alias gives them exactly the NAPALM shape
    # they were written against, including any keys a vendor attached that this
    # project does not model.
    raw = facts.to_napalm()

    # interface → VRF membership (empty when the collector didn't gather VRFs).
    interface_vrf, result.vrfs = _build_vrf_map(
        raw.get("network_instances", {}),
        raw.get("vrf_route_targets", {}),
        raw.get("vrf_interfaces", {}),
        raw.get("vrf_rds", {}),
    )

    # ── 1. Extract device information ────────────────────────────────
    device_facts = facts.facts
    vendor = device_facts.vendor or extract_vendor_from_os(platform)
    hostname = device_facts.hostname.upper()
    model = device_facts.model or "Unknown"

    result.device = DiscoveredDevice(
        hostname=hostname,
        model=model,
        serial_number=device_facts.serial_number,
        os_version=device_facts.os_version or "Unknown",
        uptime=device_facts.uptime,
        vendor=vendor,
        fqdn=(device_facts.fqdn or hostname).upper(),
    )

    # ── 2. Determine device type from model ──────────────────────────
    vendor_key = vendor.lower() if vendor else "unknown"
    result.device.device_type = determine_device_type(model, vendor_key, device_type_mapping)
    result.device_name = hostname

    logger.debug(f"Model: {model} | Mapped Device Type: {result.device.device_type}")

    # ── 3. Process interfaces ────────────────────────────────────────
    for name, data in facts.interfaces.items():
        normalized = _norm(name)

        if not should_include_interface(normalized, interface_filters):
            continue
        if exclude_disabled and not data.is_enabled:
            continue

        try:
            intf = DiscoveredInterface(
                name=normalized,
                original_name=name,
                description=data.description,
                enabled=data.is_enabled,
                mac_address=normalize_mac_address(data.mac_address),
                # 0 is what a platform reports when it does not know, not a real
                # zero-MTU/zero-speed port.
                mtu=data.mtu or None,
                speed=data.speed or None,
                # A collector may resolve the port type itself (Linux `ip`
                # reports the link type, F5 the configured media); otherwise fall
                # back to the name heuristic.
                type=data.netbox_type or extract_interface_type(normalized),
                # Likewise for virtual-ness: None means the collector did not
                # say, so guess from the name. An explicit False is respected.
                is_virtual=(
                    data.is_virtual
                    if data.is_virtual is not None
                    else is_virtual_interface(normalized)
                ),
                is_management=is_management_interface(normalized),
                admin_status="up" if data.is_enabled else "down",
                oper_status="up" if data.is_up else "down",
                # Sub-interface parent detection.
                parent=normalized.split(".")[0] if "." in normalized else None,
                vrf=interface_vrf.get(normalized),
            )
        except ValidationError as exc:
            logger.warning(
                f"[{hostname}] Skipping interface {name!r}: it does not satisfy the "
                f"discovery contract: {_first_error(exc)}"
            )
            continue

        result.interfaces.append(intf)

    # ── 4. Process IP addresses ──────────────────────────────────────
    for name, addresses in facts.interfaces_ip.items():
        normalized = _norm(name)
        if not should_include_interface(normalized, interface_filters):
            continue

        for ip_version, family in (("ipv4", addresses.ipv4), ("ipv6", addresses.ipv6)):
            default_length = 32 if ip_version == "ipv4" else 128
            for ip_addr, ip_info in family.items():
                prefix_len = (
                    ip_info.prefix_length if ip_info.prefix_length is not None else default_length
                )
                try:
                    # ip_version and prefix_length are re-derived from the
                    # address by the model; passing them is belt and braces for
                    # the case where the address arrives without a mask.
                    result.ip_addresses.append(DiscoveredIP(
                        address=f"{ip_addr}/{prefix_len}",
                        interface=normalized,
                        ip_version=ip_version,
                        prefix_length=prefix_len,
                        role="loopback" if "loopback" in normalized.lower() else "",
                        is_management=is_management_interface(normalized),
                        vrf=interface_vrf.get(normalized),
                    ))
                except ValidationError as exc:
                    logger.warning(
                        f"[{hostname}] Skipping address {ip_addr!r} on {name!r}: "
                        f"{_first_error(exc)}"
                    )

    # ── 5. Find management IP → set primary_ipv4 ────────────────────
    mgmt_interface_names = {intf.name for intf in result.interfaces if intf.is_management}
    for ip in result.ip_addresses:
        if ip.interface in mgmt_interface_names and ip.ip_version == "ipv4":
            result.primary_ipv4 = ip.address
            break  # first management IPv4 wins

    # No management interface found: fall back to the address we logged in
    # with, so the device still gets a reachable primary IP in NetBox.
    if not result.primary_ipv4 and login_ip:
        for ip in result.ip_addresses:
            if ip.ip_version == "ipv4" and ip.ip == login_ip:
                result.primary_ipv4 = ip.address
                logger.info(
                    f"[{hostname}] No management interface IPv4 found; "
                    f"using login IP {ip.address} as primary"
                )
                break

    # ── 6. Process LLDP neighbors ────────────────────────────────────
    for local_intf, neighbors in facts.lldp_neighbors.items():
        for neighbor in neighbors:
            remote_host = neighbor.hostname.split(".")[0].upper()
            remote_port = normalize_interface_name(neighbor.port)
            if not remote_host or not remote_port:
                # A neighbour missing either end says nothing placeable about the
                # topology. Expected often enough (a partially-populated LLDP
                # table) that it is not worth a warning.
                continue
            try:
                result.lldp_neighbors.append(DiscoveredLLDPNeighbor(
                    local_interface=normalize_interface_name(local_intf),
                    remote_hostname=remote_host,
                    remote_interface=remote_port,
                    remote_system_name=remote_host,
                ))
            except ValidationError as exc:
                logger.warning(
                    f"[{hostname}] Skipping neighbour on {local_intf!r}: {_first_error(exc)}"
                )

    # Enhance with LLDP detail data
    if facts.lldp_neighbors_detail and result.lldp_neighbors:
        enhanced = enhance_lldp_with_details(
            raw.get("lldp_neighbors", {}), raw.get("lldp_neighbors_detail", {})
        )
        detail_map = {(e["local_interface"], e["remote_hostname"]): e for e in enhanced}
        for n in result.lldp_neighbors:
            detail = detail_map.get((n.local_interface, n.remote_hostname))
            if detail:
                n.remote_system_description = detail.get("remote_system_description", "")
                n.remote_chassis_id = detail.get("remote_chassis_id", "")
                n.remote_port_id = detail.get("remote_port_id", "")
                n.remote_port_description = detail.get("remote_port_description", "")

    # ── Summary ──────────────────────────────────────────────────────
    # Step 7 of the old pipeline -- dropping entries with an empty name, address
    # or neighbour -- is gone: the contract rejects those at construction above,
    # where the reason can still be logged.
    logger.info(f"[{hostname}] Processed: {result.summary()}")

    return result


def _first_error(exc: ValidationError) -> str:
    """The first validation problem, in one line fit for a log."""
    errors = exc.errors()
    if not errors:
        return str(exc)
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ())) or "value"
    return f"{location}: {first.get('msg', '')}"


def process_napalm_data(
    napalm_data: dict[str, Any] | CollectedFacts,
    platform: str,
    interface_filters: dict | None = None,
    device_type_mapping: dict | None = None,
    exclude_disabled: bool = True,
    login_ip: str | None = None,
    normalize_names: bool = True,
    collector: str = "",
) -> DiscoveryResult:
    """
    `process_facts` for callers that still hold a plain NAPALM-shaped dict.

    The dict is validated into `CollectedFacts` first, so a collector that was
    written before the contract existed -- or one that deliberately returns
    dicts -- keeps working and gains the same coercion and checking.
    """
    facts = (
        napalm_data
        if isinstance(napalm_data, CollectedFacts)
        else CollectedFacts.from_napalm(napalm_data or {})
    )
    return process_facts(
        facts,
        platform=platform,
        interface_filters=interface_filters,
        device_type_mapping=device_type_mapping,
        exclude_disabled=exclude_disabled,
        login_ip=login_ip,
        normalize_names=normalize_names,
        collector=collector,
    )
