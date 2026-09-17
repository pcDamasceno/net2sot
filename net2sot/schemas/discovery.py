"""
schemas/discovery.py - The sync contract: what a sink receives.

This is the *normalized* layer. Where `CollectedFacts` still speaks the
device's dialect, a `DiscoveryResult` is vendor-neutral: interface names are
canonical, filtered and typed, addresses carry their VRF, neighbours are
resolved to (local port, remote device, remote port) pairs. Nothing in here
mentions a particular source of truth, which is the point -- the NetBox sink and
a future Infrahub sink consume the same object, and so does any sink somebody
else writes.

It is also the project's archive format. A `DiscoveryResult` round-trips through
``model_dump_json()`` with no loss, so a run can collect on a jump host, ship
JSON, and sync from somewhere with API reachability -- two halves that no longer
have to happen in the same process.
"""

from __future__ import annotations

import ipaddress
from datetime import datetime, timezone
from typing import Any

from pydantic import Field, model_validator

from net2sot.schemas.base import (
    SCHEMA_VERSION,
    CleanStr,
    CoercedInt,
    DiscoverySchema,
    OptionalInt,
    StrList,
)

# ── Device ───────────────────────────────────────────────────────────


class DiscoveredDevice(DiscoverySchema):
    """The device itself, after facts have been normalized and typed."""

    hostname: CleanStr = ""
    model: CleanStr = "Unknown"
    serial_number: CleanStr = ""
    os_version: CleanStr = ""
    uptime: CoercedInt = 0
    vendor: CleanStr = ""
    fqdn: CleanStr = ""
    # Resolved against settings' device_type_mapping: the slug a source of truth
    # should file this device under ("cisco_iol", "bigip-generic"), not the raw
    # model string.
    device_type: CleanStr = "unknown"

    # Anything a plugin wants a sink to see deliberately. Addressable from
    # settings.yaml: a custom_fields entry whose `source` is not one of the
    # built-in names is looked up here, so a plugin can publish its own field
    # without either side knowing about the other.
    custom: dict[str, Any] = Field(default_factory=dict)


# ── Interfaces ───────────────────────────────────────────────────────


class DiscoveredInterface(DiscoverySchema):
    """
    One interface, named as the source of truth should name it.

    `name` is canonical (GigabitEthernet0/0, Loopback0) on platforms whose names
    are rewritten, and verbatim on platforms whose own names are the real
    identifiers -- Linux kernel names, PAN-OS names, BIG-IP names. `original_name`
    always holds what the device called it, which is what you need to go back and
    configure the thing.
    """

    name: CleanStr = Field(min_length=1)
    original_name: CleanStr = ""
    description: CleanStr = ""
    enabled: bool = True
    mac_address: str | None = None
    mtu: OptionalInt = None
    speed: OptionalInt = None
    # Source-of-truth port type: "1000base-t", "virtual", "lag", "other".
    type: CleanStr = "other"
    is_virtual: bool = False
    is_management: bool = False
    admin_status: CleanStr = "up"
    oper_status: CleanStr = "up"
    # Sub-interface parent ("GigabitEthernet0/0" for ".100"), or a LAG member's
    # bundle. None for a top-level interface.
    parent: str | None = None
    # None means the global/default routing table, not "no VRF information".
    vrf: str | None = None

    custom: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _default_original_name(self) -> DiscoveredInterface:
        # A collector that did no renaming may leave original_name unset; the
        # field is meant to always answer "what is this called on the box".
        if not self.original_name:
            # Assigning under validate_assignment would re-enter this validator,
            # so write through __dict__ -- the value is already validated.
            self.__dict__["original_name"] = self.name
        return self


# ── Addresses ────────────────────────────────────────────────────────


class DiscoveredIP(DiscoverySchema):
    """
    One address on one interface, in CIDR form.

    `ip_version` and `prefix_length` are *derived* from `address` rather than
    trusted from the caller: a collector that reports an IPv6 address with
    ip_version="ipv4", or a /24 address carrying prefix_length=32, is a bug that
    used to surface as a confusing rejection from the source of truth. An address
    that is not parseable raises here, at the plugin's own boundary.
    """

    address: CleanStr = Field(min_length=1)  # "10.0.0.1/24"
    interface: CleanStr = Field(min_length=1)
    ip_version: CleanStr = "ipv4"
    prefix_length: CoercedInt = 32
    # "loopback", "management", or "" -- advisory, sinks may map it to a role.
    role: CleanStr = ""
    is_management: bool = False
    vrf: str | None = None

    custom: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _derive_from_address(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        raw = data.get("address")
        if not isinstance(raw, str) or not raw.strip():
            return data
        address = raw.strip()
        try:
            # strict=False so a host address inside a subnet ("10.0.0.1/24") is
            # accepted -- that is the normal case, not an error.
            interface = ipaddress.ip_interface(address)
        except ValueError as exc:
            raise ValueError(f"{address!r} is not a valid IP address in CIDR form: {exc}") from exc
        data = dict(data)
        data["address"] = str(interface)
        data["ip_version"] = f"ipv{interface.version}"
        data["prefix_length"] = interface.network.prefixlen
        return data

    @property
    def ip(self) -> str:
        """The address without its mask -- "10.0.0.1" for "10.0.0.1/24"."""
        return self.address.split("/")[0]


# ── VRFs ─────────────────────────────────────────────────────────────


class DiscoveredVRF(DiscoverySchema):
    """
    A named routing instance. The global/default table is not one of these: an
    interface in it carries vrf=None, so a sink never has to guess which of
    "default", "global" and "master" a platform meant.
    """

    name: CleanStr = Field(min_length=1)
    # Route distinguisher, e.g. "65000:100". Empty when the platform reports
    # none -- sources of truth generally enforce RD uniqueness, so a placeholder
    # would collide across every VRF that has no real one.
    rd: CleanStr = ""
    # OpenConfig instance type, e.g. "L3VRF".
    type: CleanStr = ""
    import_targets: StrList = Field(default_factory=list)
    export_targets: StrList = Field(default_factory=list)

    custom: dict[str, Any] = Field(default_factory=dict)


# ── Neighbours ───────────────────────────────────────────────────────


class DiscoveredLLDPNeighbor(DiscoverySchema):
    """
    One resolved adjacency: a local port, and the device and port heard on it.

    A neighbour is only useful to a sink if both ends are named, so both are
    required -- half-parsed neighbours are rejected at construction instead of
    being filtered out silently later.
    """

    local_interface: CleanStr = Field(min_length=1)
    remote_hostname: CleanStr = Field(min_length=1)
    remote_interface: CleanStr = Field(min_length=1)
    remote_system_name: CleanStr = ""
    remote_system_description: CleanStr = ""
    remote_chassis_id: CleanStr = ""
    remote_port_id: CleanStr = ""
    remote_port_description: CleanStr = ""

    custom: dict[str, Any] = Field(default_factory=dict)


# ── The envelope ─────────────────────────────────────────────────────


class DiscoveryResult(DiscoverySchema):
    """
    Everything known about one device after normalization: the input to
    `Sink.sync()`, and the unit this project archives and replays.
    """

    schema_version: str = SCHEMA_VERSION

    device: DiscoveredDevice = Field(default_factory=DiscoveredDevice)
    interfaces: list[DiscoveredInterface] = Field(default_factory=list)
    ip_addresses: list[DiscoveredIP] = Field(default_factory=list)
    lldp_neighbors: list[DiscoveredLLDPNeighbor] = Field(default_factory=list)
    vrfs: list[DiscoveredVRF] = Field(default_factory=list)

    # The name this device should carry in the source of truth. Usually the
    # discovered hostname, but kept separate because a sink may be told to file
    # a device under its inventory name instead.
    device_name: CleanStr = ""
    # "10.0.0.1/24", chosen by process.py: a management interface's address,
    # else the address the device was actually reached at.
    primary_ipv4: str | None = None
    # The address Nornir connected to, for that fallback.
    login_ip: str | None = None

    # Provenance. `platform` rides on the result rather than being read out of
    # run settings at sync time: one settings dict is shared by every worker
    # thread, so a mixed-platform inventory would otherwise race and label
    # devices with whatever platform another thread wrote last.
    platform: CleanStr = ""
    collector: CleanStr = ""
    collected_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    custom: dict[str, Any] = Field(default_factory=dict)

    # ── Compatibility ────────────────────────────────────────────────

    @property
    def netbox_device_name(self) -> str:
        """Former name of `device_name`, from when NetBox was the only sink."""
        return self.device_name

    @netbox_device_name.setter
    def netbox_device_name(self, value: str) -> None:
        self.device_name = value

    # ── Convenience ──────────────────────────────────────────────────

    def interface(self, name: str) -> DiscoveredInterface | None:
        """The interface by canonical name, or None."""
        return next((i for i in self.interfaces if i.name == name), None)

    def addresses_for(self, interface: str) -> list[DiscoveredIP]:
        return [ip for ip in self.ip_addresses if ip.interface == interface]

    def is_usable(self) -> bool:
        """
        Whether there is enough here to be worth syncing. A device with no name
        or no interfaces means collection produced nothing usable, however
        cleanly it reported success.
        """
        return bool(self.device.hostname and self.interfaces)

    def summary(self) -> str:
        physical = sum(1 for i in self.interfaces if not i.is_virtual)
        virtual = len(self.interfaces) - physical
        return (
            f"{len(self.interfaces)} interfaces ({physical} physical, {virtual} virtual), "
            f"{len(self.ip_addresses)} IPs, {len(self.vrfs)} VRFs, "
            f"{len(self.lldp_neighbors)} LLDP neighbours, "
            f"device_type={self.device.device_type}"
        )


# The name this model carried before sinks were pluggable. Kept so existing
# imports and type hints keep resolving.
ProcessedData = DiscoveryResult
