"""
netbox_sync.py - Push discovered data to NetBox.

Creates/updates, in order: site, device, interfaces, IP addresses and
LLDP-derived cables.
"""

from __future__ import annotations

import logging

from net2sot.helpers import extract_interface_type, is_management_interface
from net2sot.netbox_client import NetboxClient
from net2sot.schemas import DiscoveryResult, SyncReport

logger = logging.getLogger("discovery.netbox_sync")


# The counter vocabulary is part of the sink contract (schemas/sync.py) rather
# than private to this module, so a sink for another source of truth reports in
# the same shape and the run report needs no special case for it. SyncStats is
# the name this project used when NetBox was the only target.
SyncStats = SyncReport

# `ProcessedData` was this model's name before sinks were pluggable.
ProcessedData = DiscoveryResult


def _custom_field_value(source: str | None, data: ProcessedData, start_time: str):
    """
    Resolve the value discovery supplies for a custom field's `source` key. This
    is the "we bring this info" side of the custom_fields config: each field
    names a source, and this maps it to a value from the freshly discovered data.
    Unknown sources return None and are skipped by the caller.
    """
    if not source:
        return None
    dev = data.device
    if source == "facts_summary":
        return {
            "hostname": dev.hostname,
            "model": dev.model,
            "serial": dev.serial_number,
            "os_version": dev.os_version,
            "vendor": dev.vendor,
            "device_type": dev.device_type,
            "uptime": dev.uptime,
        }
    builtin = {
        "sync_time": start_time,
        "hostname": dev.hostname,
        "model": dev.model,
        "serial": dev.serial_number,
        "os_version": dev.os_version,
        "vendor": dev.vendor,
        "device_type": dev.device_type,
        "uptime": dev.uptime,
        "fqdn": dev.fqdn,
        "primary_ipv4": data.primary_ipv4,
    }
    if source in builtin:
        return builtin[source]
    # A source this project has no name for is looked up in the device's `custom`
    # bucket, which is how a collector plugin publishes a value of its own: the
    # plugin fills custom["bgp_asn"], the operator adds a custom field whose
    # source is "bgp_asn", and neither has to know about the other.
    return dev.custom.get(source)


def _apply_custom_fields(
    nb: NetboxClient, device, data: ProcessedData, settings: dict,
    start_time: str, hostname: str,
) -> None:
    """
    Write the configured custom-field values onto the just-synced device.

    For each entry in settings["custom_fields"] that applies to devices, the
    value is resolved from its `source` and written unless the field already has
    a value and its `update_existing` is false (which preserves a hand-set
    value). Unchanged values are skipped so a re-run doesn't churn the changelog.
    Definitions are created up front by ensure_custom_fields(), not here.
    """
    cf_configs = settings.get("custom_fields") or []
    if not cf_configs:
        return

    current = getattr(device, "custom_fields", None) or {}
    to_set: dict = {}
    for cf in cf_configs:
        name = cf.get("name")
        if not name:
            continue
        # Only fields that attach to devices can be written on a device.
        object_types = cf.get("object_types") or ["dcim.device"]
        if "dcim.device" not in object_types:
            continue

        value = _custom_field_value(cf.get("source"), data, start_time)
        if value is None:
            continue

        existing = current.get(name)
        if not cf.get("update_existing", True) and existing not in (None, ""):
            continue  # keep the hand-set value
        if existing == value:
            continue  # no change

        to_set[name] = value

    if to_set and nb.set_custom_fields(device, to_set):
        logger.info(f"[{hostname}] Custom fields set: {', '.join(sorted(to_set))}")


def sync_to_netbox(
    nb: NetboxClient,
    data: ProcessedData,
    settings: dict,
    start_time: str,
    platform: str,
) -> SyncStats:
    """
    Push all discovered data to NetBox.
    Orchestrates: site → device → interfaces → IPs → cables.

    `platform` is the host's own platform and must be passed per call, not read
    back out of `settings`: every worker thread shares one settings dict, so a
    mixed-platform inventory would race and label devices with whatever platform
    another thread wrote last.
    """
    stats = SyncStats()
    hostname = data.netbox_device_name
    site_name = settings.get("site_name", "prod")
    debug = settings.get("debug", False)
    # Whether discovery may modify/remove things that already exist in NetBox
    # (device comments & type, interface descriptions, stale IPs). Off under
    # --no-update-existing, which keeps a re-run purely additive.
    update_existing = settings.get("update_existing", True)

    # ── 1. Site (create_site.yml) ────────────────────────────────────
    # A --from-netbox re-discovery already has the device -- and its site -- in
    # NetBox, so ensuring settings' site_name here is meaningless work: it only
    # matters when a YAML-inventory run imports a brand-new device. Reuse the
    # device's own site in that case; fall back to ensure_site only if the device
    # has gone missing (e.g. deleted between inventory load and sync).
    site = None
    if settings.get("from_netbox"):
        existing = nb.get_device(hostname)
        site = getattr(existing, "site", None) if existing else None
    if site is None:
        logger.info(f"[{hostname}] Ensuring site '{site_name}'...")
        site = nb.ensure_site(site_name, tenant=settings.get("tenant") or None)
        stats.site_created = True

    # ── 2. Create device (create_device.yml) ─────────────────────────
    logger.info(f"[{hostname}] Ensuring manufacturer, device type, device...")

    # Manufacturer
    vendor = data.device.vendor or "Unknown"
    manufacturer = nb.ensure_manufacturer(vendor.title())

    # Device type
    device_type = nb.ensure_device_type(data.device.device_type, manufacturer.id)
    if debug:
        logger.debug(
            f"Device Type: {data.device.device_type} | "
            f"Manufacturer: {vendor.title()}"
        )

    # Device role & platform
    device_role = nb.ensure_device_role(settings.get("device_role", "Unknown"))
    nb_platform = nb.ensure_platform((platform or "unknown").upper())

    # Device itself
    comments = (
        f"Auto-discovered on {start_time}\n"
        f"Hostname: {data.device.hostname}\n"
        f"Model: {data.device.model}\n"
        f"OS Version: {data.device.os_version}\n"
        f"Uptime: {data.device.uptime}\n"
        f"FQDN: {data.device.fqdn}\n"
        f"Serial: {data.device.serial_number}"
    )
    device = nb.ensure_device(
        name=hostname,
        site_id=site.id,
        device_type_id=device_type.id,
        device_role_id=device_role.id,
        platform_id=nb_platform.id,
        serial=data.device.serial_number,
        comments=comments,
        # Re-type a device that predates this run's device-type resolution (e.g.
        # one stuck on "cisco-generic") and refresh its auto-discovered comments.
        # Only ever safe here, where the facts came from this device itself.
        update_existing=update_existing,
    )
    stats.device_created = True
    logger.info(
        f"[{hostname}] Device synced (id={device.id}), "
        f"type={data.device.device_type}, serial={data.device.serial_number}"
    )

    # Stamp/update the configured custom fields (e.g. last_synced_at) on the
    # device now that it exists. Definitions were ensured once before the run.
    _apply_custom_fields(nb, device, data, settings, start_time, hostname)

    # ── 3. Create VRFs, then interfaces that carry them ──────────────
    # Empty/unset means no tenant is assigned, and none is created -- see
    # the "tenant" key in settings.yaml.
    tenant = settings.get("tenant") or None
    # Each named VRF discovered on the device gets its own NetBox VRF, so its
    # IPs stop colliding with the shared default one -- the point of making IPs
    # VRF-aware. Created before interfaces so an interface can reference its VRF.
    default_vrf = nb.ensure_vrf(f"default_{site_name}", tenant=tenant)
    vrf_by_name: dict[str, object] = {}
    for discovered_vrf in data.vrfs:
        logger.debug(
            f"[{hostname}] Ensuring VRF '{discovered_vrf.name}' "
            f"(rd={discovered_vrf.rd or 'none'}, type={discovered_vrf.type or '?'})"
        )
        try:
            vrf_by_name[discovered_vrf.name] = nb.ensure_vrf(
                discovered_vrf.name,
                rd=discovered_vrf.rd or None,
                tenant=tenant,
                description=f"Auto-discovered {discovered_vrf.type or 'VRF'}",
                import_targets=discovered_vrf.import_targets,
                export_targets=discovered_vrf.export_targets,
                update_existing=update_existing,
            )
        except Exception as e:
            # One unresolvable VRF (e.g. an RD conflict we can't reconcile)
            # shouldn't sink the whole device: log which name/RD collapsed, note
            # it, and let its interfaces/IPs fall back to the default VRF below.
            msg = (
                f"VRF '{discovered_vrf.name}' (rd={discovered_vrf.rd or 'none'}) "
                f"could not be synced: {e}"
            )
            logger.warning(f"[{hostname}] {msg}")
            stats.errors.append(msg)
    stats.vrfs_created = len(vrf_by_name)

    # An interface routes in the VRF it is a member of, so stamp that VRF on it.
    # Only interfaces in a named VRF get one; global-table and L2 ports stay
    # VRF-less (intf.vrf is None). A VRF that failed to sync above resolves to
    # None too, leaving the interface blank rather than failing it.
    wanted = [
        {
            "name": intf.name,
            "type": intf.type,
            "enabled": intf.enabled,
            "mac_address": intf.mac_address,
            "mtu": intf.mtu,
            "description": intf.description,
            "speed": intf.speed,
            "vrf": vrf_by_name[intf.vrf].id if intf.vrf in vrf_by_name else None,
        }
        for intf in data.interfaces
        if intf.enabled
    ]
    logger.info(f"[{hostname}] Syncing {len(wanted)} interfaces...")

    # name → nb interface obj, for every interface that exists in NetBox after
    # this call. Anything missing from it failed and is reported below.
    created_interfaces: dict[str, object] = nb.ensure_interfaces(
        device.id, wanted, update_existing=update_existing, label=hostname
    )
    stats.interfaces_created = len(created_interfaces)

    failed_interfaces = {i["name"] for i in wanted} - created_interfaces.keys()
    for name in sorted(failed_interfaces):
        stats.errors.append(f"Failed to create interface {name}")

    logger.info(f"[{hostname}] {stats.interfaces_created} interfaces synced")

    # ── 4. Create IP addresses (create_ip_addresses.yml) ─────────────
    # VRFs were created in section 3; the IPs reuse vrf_by_name/default_vrf so
    # each IP lands in the same VRF as the interface it sits on.
    logger.info(
        f"[{hostname}] Creating {len(data.ip_addresses)} IP addresses "
        f"across {len(vrf_by_name) + 1} VRF(s)..."
    )

    # NAPALM can report one interface under different casing across getters
    # (get_interfaces -> "Loopback78", get_interfaces_ip -> "loopback78"), so an
    # IP's interface reference is matched to the synced interface
    # case-insensitively. Names genuinely lowercase on the box (SR Linux
    # "ethernet-1/1") were created under that same spelling, so this only bridges
    # the casing gap between getters and never changes what is stored.
    interfaces_by_lower = {n.lower(): obj for n, obj in created_interfaces.items()}
    failed_lower = {n.lower() for n in failed_interfaces}

    # The NetBox IP object for the login/management address, captured while the
    # IPs are created so it can be pinned as the device's primary below.
    primary_ip_obj = None

    for ip in data.ip_addresses:
        nb_intf = interfaces_by_lower.get(ip.interface.lower())
        if nb_intf is None:
            # An interface we tried and failed to create takes its IPs down with
            # it, so that is a reported error, not a debug detail. An interface
            # simply not discovered (disabled, filtered out) stays quiet.
            if ip.interface.lower() in failed_lower:
                logger.warning(
                    f"[{hostname}] Skipping IP {ip.address} - interface "
                    f"'{ip.interface}' could not be created"
                )
                stats.ip_addresses_failed += 1
            elif debug:
                logger.debug(
                    f"[{hostname}] Skipping IP {ip.address} - "
                    f"interface '{ip.interface}' not in NetBox"
                )
            continue

        # An IP on a named VRF interface lands in that VRF; everything else
        # stays in the default VRF. vrf_by_name always has an entry when ip.vrf
        # is set (both come from the same discovery pass), but fall back anyway.
        target_vrf = vrf_by_name.get(ip.vrf, default_vrf) if ip.vrf else default_vrf
        ip_obj, outcome = nb.ensure_ip_address(
            address=ip.address,
            interface_id=nb_intf.id,
            vrf_id=target_vrf.id,
            role=ip.role,
            description=f"{hostname} - {ip.interface}",
        )
        if ip_obj:
            stats.ip_addresses_created += 1
            if data.primary_ipv4 and ip.address == data.primary_ipv4:
                primary_ip_obj = ip_obj
        elif outcome == "duplicate":
            # Already owned in this VRF (configured twice / on another device):
            # a data condition, not a write failure, so it must not fail the host.
            stats.ip_addresses_duplicate += 1
        else:
            stats.ip_addresses_failed += 1

    # Pin the device's primary IPv4 to the address discovery reached it on (its
    # management/login IP), so it is reachable from NetBox. Only set it when the
    # device has none yet, or when update_existing lets discovery re-point a
    # stale one; a hand-curated primary under --no-update-existing is preserved.
    # The id guard keeps a re-run from rewriting an already-correct pointer.
    if primary_ip_obj is not None:
        current_primary = getattr(device, "primary_ip4", None)
        if current_primary is None or update_existing:
            if getattr(current_primary, "id", None) != primary_ip_obj.id:
                nb.set_primary_ip(device.id, primary_ip_obj.id)
                logger.info(f"[{hostname}] Primary IPv4 set to {data.primary_ipv4}")

    # ── 4b. Remove stale IPs so an IP change leaves no leftovers ──────
    # For each interface we just synced, delete any IP still attached in NetBox
    # that this run did not rediscover — e.g. a management IP that changed.
    # An old primary IP still pinned to the device is unpinned by _delete_ip
    # before it is removed. Skipped under --no-update-existing.
    if update_existing:
        # Keyed by lower-cased interface name for the same cross-getter casing
        # reason as above, so an interface's freshly discovered IPs are matched
        # to it and not deleted as stale.
        discovered_by_intf: dict[str, set[str]] = {}
        for ip in data.ip_addresses:
            discovered_by_intf.setdefault(ip.interface.lower(), set()).add(ip.address)

        keep_by_interface = {
            nb_intf.id: discovered_by_intf.get(intf_name.lower(), set())
            for intf_name, nb_intf in created_interfaces.items()
        }
        stats.ip_addresses_deleted = nb.prune_device_ips(device.id, keep_by_interface)
        if stats.ip_addresses_deleted:
            logger.info(f"[{hostname}] Removed {stats.ip_addresses_deleted} stale IP(s)")

    logger.info(
        f"[{hostname}] {stats.ip_addresses_created} IPs created, "
        f"{stats.ip_addresses_deleted} deleted, "
        f"{stats.ip_addresses_failed} failed"
        + (f", {stats.ip_addresses_duplicate} duplicate" if stats.ip_addresses_duplicate else "")
    )

    # ── 5. Create cables via LLDP (create_cables.yml) ────────────────
    # Off unless asked for: a cable is a physical claim, and LLDP only proves
    # two ports can hear each other. Enable per run once the inventory is
    # complete enough that both ends of a link are actually in NetBox.
    if not settings.get("create_cables", False):
        logger.info(f"[{hostname}] Cable creation disabled (create_cables=false)")
        return stats

    if not settings.get("lldp_enabled", True) or not data.lldp_neighbors:
        logger.info(f"[{hostname}] LLDP cable creation skipped")
        return stats

    logger.info(f"[{hostname}] Processing {len(data.lldp_neighbors)} LLDP neighbors for cables...")
    cable_defaults = settings.get("cable_defaults", {})
    exclude_management = settings.get("lldp_exclude_management", True)

    for neighbor in data.lldp_neighbors:
        local_intf_name = neighbor.local_interface
        remote_hostname = neighbor.remote_hostname
        remote_port = neighbor.remote_interface

        # Neighbours heard on a management port sit on the shared OOB network,
        # so they are reachable, not cabled. Turning them into point-to-point
        # cables invents links that don't exist.
        if exclude_management and is_management_interface(local_intf_name):
            if debug:
                logger.debug(
                    f"[{hostname}] Skipping cable on management interface "
                    f"'{local_intf_name}' → {remote_hostname}:{remote_port}"
                )
            stats.cables_skipped += 1
            continue

        # Get local interface
        local_nb_intf = created_interfaces.get(local_intf_name)
        if not local_nb_intf:
            local_nb_intf = nb.get_interface(device.id, local_intf_name)
        if not local_nb_intf:
            if debug:
                logger.debug(
                    f"[{hostname}] Skipping cable - "
                    f"local intf '{local_intf_name}' not found"
                )
            stats.cables_skipped += 1
            continue

        # Check if remote device exists
        remote_device = nb.get_device(remote_hostname)
        if not remote_device:
            if settings.get("lldp_create_missing_devices", False):
                # Create placeholder device so the cable has something to land on
                logger.info(f"[{hostname}] Creating placeholder device '{remote_hostname}'")
                remote_device = nb.ensure_device(
                    name=remote_hostname,
                    site_id=site.id,
                    device_type_id=device_type.id,
                    device_role_id=device_role.id,
                    platform_id=nb_platform.id,
                    comments=(
                        f"Placeholder device created via LLDP discovery from {hostname}\n"
                        f"Discovered on {start_time}\n"
                        f"System Description: {neighbor.remote_system_description or 'Unknown'}"
                    ),
                )
            else:
                if debug:
                    logger.debug(
                        f"[{hostname}] Skipping cable - "
                        f"remote device '{remote_hostname}' not in NetBox"
                    )
                stats.cables_skipped += 1
                continue

        # Ensure remote interface exists
        remote_nb_intf = nb.get_interface(remote_device.id, remote_port)
        if not remote_nb_intf:
            remote_nb_intf = nb.ensure_interface(remote_device.id, {
                "name": remote_port,
                "type": extract_interface_type(remote_port),
                "enabled": True,
                "description": f"Auto-created for LLDP connection from {hostname}:{local_intf_name}",
            })
        if not remote_nb_intf:
            stats.cables_failed += 1
            stats.errors.append(
                f"Failed to create remote interface {remote_hostname}:{remote_port}"
            )
            continue

        # Create cable
        cable = nb.create_cable(
            a_intf_id=local_nb_intf.id,
            b_intf_id=remote_nb_intf.id,
            cable_type=cable_defaults.get("type", "cat6"),
            status=cable_defaults.get("status", "connected"),
            label=f"LLDP: {hostname}:{local_intf_name} <-> {remote_hostname}:{remote_port}",
        )
        if cable:
            stats.cables_created += 1
        else:
            stats.cables_skipped += 1  # likely already exists

    logger.info(
        f"[{hostname}] Cables: {stats.cables_created} created, "
        f"{stats.cables_skipped} skipped, {stats.cables_failed} failed"
    )

    return stats
