"""
schemas/facts.py - The collector contract: what a vendor plugin returns.

This is the *raw* layer. A collector talks to one device and hands back a
`CollectedFacts`: facts, interfaces, addresses, neighbours and routing
instances, still spelled the way the device spells them. Normalization
(canonical interface names, filtering, device-type mapping, VRF resolution) is
not the collector's job -- `process.py` does that for every platform alike, and
that division is what keeps a new vendor from having to re-implement the parts
that are not vendor-specific.

The field names are NAPALM's. That is deliberate rather than nostalgic: NAPALM's
getter output is the closest thing the industry has to a shared vocabulary for
this data, several of this project's own collectors already produce it, and it
means an existing NAPALM-based plugin satisfies the contract with

    CollectedFacts.model_validate(napalm_get_result)

and nothing else. A collector that does not use NAPALM builds the same model
field by field; see docs/plugins.md.

Anything a platform reports that this file does not model is kept (see
schemas/base.py) rather than dropped, so a plugin may attach its own keys and
read them back out in a sink it also ships.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from net2sot.schemas.base import (
    SCHEMA_VERSION,
    CleanStr,
    CoercedBool,
    CoercedInt,
    DiscoverySchema,
    OptionalInt,
    StrList,
)

# ── Device facts ─────────────────────────────────────────────────────


class DeviceFacts(DiscoverySchema):
    """
    Mirrors NAPALM ``get_facts()``.

    `hostname` is the one field the pipeline cannot proceed without -- a device
    with no name cannot be created in a source of truth -- but it is not
    required *here*, because a collector that recovers it late (IOS-XR reads it
    off the CLI prompt, because 'show version' does not print it) needs to build
    the model before it has one. The check belongs at the end of collection;
    see CollectedFacts.require_hostname().
    """

    hostname: CleanStr = ""
    fqdn: CleanStr = ""
    vendor: CleanStr = ""
    model: CleanStr = ""
    serial_number: CleanStr = ""
    os_version: CleanStr = ""
    uptime: CoercedInt = 0
    interface_list: StrList = Field(default_factory=list)


# ── Interfaces ───────────────────────────────────────────────────────


class InterfaceFacts(DiscoverySchema):
    """
    Mirrors one entry of NAPALM ``get_interfaces()``, keyed by the device's own
    interface name.

    Two fields past NAPALM's set are contract, not extension, because a
    collector frequently knows them better than a name heuristic can guess:

      netbox_type  the physical port type in source-of-truth vocabulary
                   ("1000base-t", "10gbase-x-sfpp"). Set it when the device
                   reports its media (Linux reports the link type, F5 reports
                   the configured media); leave it unset and the type is
                   inferred from the interface name instead.
      is_virtual   whether this is a logical construct rather than a port.
                   Same rule: set it when the device is authoritative, leave it
                   None to fall back to the name heuristic.
    """

    is_up: CoercedBool = False
    is_enabled: CoercedBool = True
    description: CleanStr = ""
    mac_address: CleanStr = ""
    mtu: OptionalInt = None
    speed: OptionalInt = None
    last_flapped: float = -1.0

    netbox_type: CleanStr = ""
    is_virtual: bool | None = None


class IPAddressFacts(DiscoverySchema):
    """One address under an interface's address family: its mask length."""

    prefix_length: OptionalInt = None


class InterfaceAddressFacts(DiscoverySchema):
    """
    Mirrors one entry of NAPALM ``get_interfaces_ip()``: the addresses
    configured on a single interface, grouped by family and keyed by the bare
    address ("10.0.0.1", not "10.0.0.1/24").
    """

    ipv4: dict[str, IPAddressFacts] = Field(default_factory=dict)
    ipv6: dict[str, IPAddressFacts] = Field(default_factory=dict)


# ── Neighbours ───────────────────────────────────────────────────────


class LLDPNeighborFacts(DiscoverySchema):
    """
    Mirrors one entry of NAPALM ``get_lldp_neighbors()``: who is heard on a
    local port. CDP neighbours are carried in the same structure -- the
    distinction matters to the protocol, not to the topology being recorded.
    """

    hostname: CleanStr = ""
    port: CleanStr = ""


class LLDPNeighborDetailFacts(DiscoverySchema):
    """Mirrors one entry of NAPALM ``get_lldp_neighbors_detail()``."""

    parent_interface: CleanStr = ""
    remote_chassis_id: CleanStr = ""
    remote_system_name: CleanStr = ""
    remote_port: CleanStr = ""
    remote_port_description: CleanStr = ""
    remote_system_description: CleanStr = ""
    remote_system_capab: StrList = Field(default_factory=list)
    remote_system_enable_capab: StrList = Field(default_factory=list)


# ── Routing instances / VRFs ─────────────────────────────────────────


class NetworkInstanceState(DiscoverySchema):
    route_distinguisher: CleanStr = ""


class NetworkInstanceInterfaces(DiscoverySchema):
    # NAPALM nests membership one level deeper than you would expect:
    # {"interface": {"GigabitEthernet0/0": {}}}. Kept as-is so a NAPALM payload
    # validates untouched.
    interface: dict[str, dict[str, Any]] = Field(default_factory=dict)


class NetworkInstanceFacts(DiscoverySchema):
    """Mirrors one entry of NAPALM ``get_network_instances()``."""

    name: CleanStr = ""
    type: CleanStr = ""
    state: NetworkInstanceState = Field(default_factory=NetworkInstanceState)
    interfaces: NetworkInstanceInterfaces = Field(default_factory=NetworkInstanceInterfaces)


class RouteTargets(DiscoverySchema):
    """The import/export targets of one VRF, e.g. ["65000:100"]."""

    import_targets: StrList = Field(default_factory=list, alias="import")
    export_targets: StrList = Field(default_factory=list, alias="export")


# ── The envelope ─────────────────────────────────────────────────────


class CollectedFacts(DiscoverySchema):
    """
    Everything one collector gathered from one device: the return type of
    `Collector.collect()`.

    Every section is optional. A platform with no LLDP, no VRFs or no way to
    report addresses returns the sections it can fill and leaves the rest empty;
    the pipeline degrades to "device + interfaces" rather than failing. The one
    hard requirement is a hostname and at least one interface, which is checked
    after collection, not per field.

    The three ``vrf_*`` sections exist because NAPALM's get_network_instances is
    not universally implemented and never carries route targets. A collector
    that can read 'show vrf detail' (or its equivalent) fills them and they are
    merged with whatever network_instances reported, with network_instances
    staying authoritative where the two overlap.
    """

    schema_version: str = SCHEMA_VERSION

    facts: DeviceFacts = Field(default_factory=DeviceFacts)
    interfaces: dict[str, InterfaceFacts] = Field(default_factory=dict)
    interfaces_ip: dict[str, InterfaceAddressFacts] = Field(default_factory=dict)
    lldp_neighbors: dict[str, list[LLDPNeighborFacts]] = Field(default_factory=dict)
    lldp_neighbors_detail: dict[str, list[LLDPNeighborDetailFacts]] = Field(default_factory=dict)
    network_instances: dict[str, NetworkInstanceFacts] = Field(default_factory=dict)

    # VRF data gathered outside get_network_instances, all keyed by VRF name.
    vrf_route_targets: dict[str, RouteTargets] = Field(default_factory=dict)
    vrf_interfaces: dict[str, list[str]] = Field(default_factory=dict)
    vrf_rds: dict[str, CleanStr] = Field(default_factory=dict)

    # ── Helpers for plugin authors ───────────────────────────────────

    @classmethod
    def from_napalm(cls, data: dict[str, Any]) -> CollectedFacts:
        """
        Validate a NAPALM-shaped dict -- a ``napalm_get`` result, or the same
        shape assembled by hand -- into the contract. The one adaptation is that
        NAPALM's getters are named after the getter ("get_facts"), while a
        result dict keys them plainly ("facts"); both spellings are accepted.
        """
        if not isinstance(data, dict):
            raise TypeError(f"CollectedFacts.from_napalm() expects a dict, got {type(data).__name__}")
        unprefixed = {
            (key[4:] if key.startswith("get_") else key): value for key, value in data.items()
        }
        return cls.model_validate(unprefixed)

    def to_napalm(self) -> dict[str, Any]:
        """
        The plain nested dict a NAPALM getter result would be. For code (and
        tests) that predate the contract, and for writing a payload to disk.
        """
        # warnings=False for the same reason as normalized(): a collector may
        # have mutated raw dicts into these containers, which serializes fine
        # but is exactly what Pydantic would warn about.
        return self.model_dump(
            mode="json", by_alias=True, exclude={"schema_version"}, warnings=False
        )

    def add_address(
        self, interface: str, address: str, prefix_length: int | str | None = None
    ) -> None:
        """
        Record one IP address on one interface.

        Preferable to reaching into `interfaces_ip` and assigning a dict:
        Pydantic validates what is *assigned to a field*, not what is mutated
        into a container it already holds, so building the nested structure by
        hand is the one way to get an unvalidated value into an otherwise
        validated model. This does it properly, and works out the address family
        for you.

            facts.add_address("ether1", "192.0.2.1", 24)
            facts.add_address("ether1", "2001:db8::1/64")
        """
        bare, _, inline_length = address.strip().partition("/")
        length = prefix_length if prefix_length is not None else inline_length
        entry = self.interfaces_ip.setdefault(interface, InterfaceAddressFacts())
        family = entry.ipv6 if ":" in bare else entry.ipv4
        family[bare] = IPAddressFacts(prefix_length=length)

    def normalized(self) -> CollectedFacts:
        """
        This model with every nested value validated.

        Pydantic validates assignment to a *field*, not mutation of a container
        a field already holds -- so ``facts.interfaces["eth0"] = {...}`` and
        ``entry.ipv4[ip] = {...}`` both put a raw dict inside an otherwise
        validated model, and the first thing to read it as a model crashes.
        Building the structure that way is natural enough in a collector that
        the pipeline re-validates here rather than trusting it, which also means
        a plugin may hand back plain dicts throughout if it prefers.
        """
        # warnings=False: dumping the un-validated values this exists to fix is
        # exactly the case Pydantic would warn about.
        return CollectedFacts.model_validate(
            self.model_dump(mode="python", by_alias=True, warnings=False)
        )

    def require_hostname(self, fallback: str = "") -> str:
        """
        The device's hostname, falling back to `fallback` (usually the inventory
        name) when the platform did not report one, raising when there is
        neither. Called at the end of collection: a nameless device cannot be
        written to a source of truth, and failing here names the real cause
        instead of surfacing as an empty-name API error three steps later.
        """
        hostname = self.facts.hostname or fallback
        if not hostname:
            raise ValueError(
                "collection produced no hostname: the device reported none and no "
                "inventory name was supplied as a fallback"
            )
        self.facts.hostname = hostname
        if not self.facts.fqdn:
            self.facts.fqdn = hostname
        return hostname

    def summary(self) -> str:
        """One line for the run log: what this collection actually came back with."""
        return (
            f"hostname={self.facts.hostname or '?'}, "
            f"model={self.facts.model or '?'}, "
            f"{len(self.interfaces)} interfaces, "
            f"{len(self.interfaces_ip)} with IPs, "
            f"{len(self.lldp_neighbors)} neighbours, "
            f"{len(self.network_instances) or len(self.vrf_interfaces)} VRFs"
        )
