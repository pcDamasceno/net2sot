"""
The discovery contract: what a plugin may hand us, and what it may not.

These are the guarantees docs/plugins.md makes to plugin authors, so a change
that breaks one of them is a breaking change to the contract and needs
SCHEMA_VERSION to move with it.
"""

import pytest
from pydantic import ValidationError

from net2sot.schemas import (
    SCHEMA_VERSION,
    CollectedFacts,
    DeviceFacts,
    DiscoveredDevice,
    DiscoveredInterface,
    DiscoveredIP,
    DiscoveredLLDPNeighbor,
    DiscoveryResult,
    InterfaceAddressFacts,
    InterfaceFacts,
    ProcessedData,
    SyncReport,
    SyncStats,
)


class TestCoercion:
    """
    CLI output is strings, and a parser that found nothing returns "". The
    contract absorbs that so every plugin does not have to.
    """

    @pytest.mark.parametrize(
        "raw,expected",
        [("1000", 1000), (1000.0, 1000), ("1,000", 1000), ("", None), ("unknown", None),
         ("N/A", None), (None, None), ("garbage", None), ("unknown", None)],
    )
    def test_optional_int(self, raw, expected):
        assert InterfaceFacts(speed=raw).speed == expected

    def test_unknown_is_a_value_not_an_absence(self):
        # "Unknown" is this project's own default for an unidentified model and
        # device type; coercing it away would erase those on every round-trip.
        assert DeviceFacts(model="Unknown").model == "Unknown"
        assert DiscoveredDevice(device_type="unknown").device_type == "unknown"

    @pytest.mark.parametrize(
        "raw,expected",
        [("up", True), ("enabled", True), ("yes", True), (True, True),
         ("down", False), ("disabled", False), ("no", False), (False, False)],
    )
    def test_link_state_words(self, raw, expected):
        assert InterfaceFacts(is_up=raw).is_up is expected

    @pytest.mark.parametrize("raw", ["", "N/A", "none", "null", "-", "not set", None])
    def test_absent_strings_become_empty(self, raw):
        # A device that reports no serial has no serial; writing the literal
        # "N/A" into a source of truth is worse than writing nothing.
        assert DeviceFacts(serial_number=raw).serial_number == ""

    def test_scalars_become_strings(self):
        # SR Linux and PAN-OS speak JSON and report numbers as numbers.
        assert DeviceFacts(model=7220).model == "7220"

    def test_uptime_defaults_to_zero_not_none(self):
        assert DeviceFacts(uptime="").uptime == 0


class TestVendorExtensions:
    """extra="allow": a vendor's own keys survive, they are not dropped."""

    def test_undeclared_keys_round_trip(self):
        facts = CollectedFacts.from_napalm(
            {"interfaces": {"Gi0/0": {"is_up": True, "poe_class": 4}}}
        )
        interface = facts.interfaces["Gi0/0"]
        assert interface.poe_class == 4
        assert facts.to_napalm()["interfaces"]["Gi0/0"]["poe_class"] == 4

    def test_custom_bucket_is_the_declared_place_for_them(self):
        device = DiscoveredDevice(hostname="R1", custom={"bgp_asn": 65000})
        assert device.custom["bgp_asn"] == 65000


class TestCollectedFacts:
    def test_accepts_napalm_getter_names(self):
        # A napalm_get result keys getters plainly; some callers keep the
        # "get_" prefix. Both validate.
        facts = CollectedFacts.from_napalm({"get_facts": {"hostname": "r1"}})
        assert facts.facts.hostname == "r1"

    def test_every_section_is_optional(self):
        # A platform with no LLDP and no VRFs returns what it has.
        facts = CollectedFacts()
        assert facts.interfaces == {} and facts.lldp_neighbors == {}
        assert facts.schema_version == SCHEMA_VERSION

    def test_require_hostname_falls_back_to_inventory_name(self):
        # IOS-XR's 'show version' carries no hostname.
        facts = CollectedFacts()
        assert facts.require_hostname(fallback="core-rtr01") == "core-rtr01"
        assert facts.facts.fqdn == "core-rtr01"

    def test_require_hostname_prefers_what_the_device_said(self):
        facts = CollectedFacts.from_napalm({"facts": {"hostname": "real"}})
        assert facts.require_hostname(fallback="inventory") == "real"

    def test_require_hostname_raises_with_nothing_to_fall_back_on(self):
        with pytest.raises(ValueError, match="no hostname"):
            CollectedFacts().require_hostname()

    def test_route_targets_accept_import_export_aliases(self):
        facts = CollectedFacts.from_napalm(
            {"vrf_route_targets": {"BLUE": {"import": "65000:1", "export": ["65000:2"]}}}
        )
        # A single target arrives as a bare string from some TextFSM templates.
        assert facts.vrf_route_targets["BLUE"].import_targets == ["65000:1"]
        assert facts.to_napalm()["vrf_route_targets"]["BLUE"]["import"] == ["65000:1"]

    def test_to_napalm_excludes_the_schema_version(self):
        assert "schema_version" not in CollectedFacts().to_napalm()


class TestBuildingFactsByHand:
    """
    A collector builds these models by mutating them, and Pydantic validates
    assignment to a *field*, not mutation of a container a field already holds.
    These are the two ways that trap is closed.
    """

    def test_add_address_works_out_the_family(self):
        facts = CollectedFacts()
        facts.add_address("ether1", "192.0.2.1", 24)
        facts.add_address("ether1", "2001:db8::1/64")

        entry = facts.interfaces_ip["ether1"]
        assert entry.ipv4["192.0.2.1"].prefix_length == 24
        assert entry.ipv6["2001:db8::1"].prefix_length == 64

    def test_add_address_coerces_a_string_length(self):
        facts = CollectedFacts()
        facts.add_address("ether1", "192.0.2.1", "24")
        assert facts.interfaces_ip["ether1"].ipv4["192.0.2.1"].prefix_length == 24

    def test_normalized_validates_dicts_mutated_into_containers(self):
        # The natural-but-wrong way to build this. It must not crash the
        # pipeline, so process_facts() re-validates rather than trusting it.
        facts = CollectedFacts()
        facts.interfaces["ether1"] = {"is_up": "up", "speed": "1000"}
        facts.interfaces_ip["ether1"] = InterfaceAddressFacts()
        facts.interfaces_ip["ether1"].ipv4["192.0.2.1"] = {"prefix_length": "24"}

        clean = facts.normalized()
        assert clean.interfaces["ether1"].is_up is True
        assert clean.interfaces["ether1"].speed == 1000
        assert clean.interfaces_ip["ether1"].ipv4["192.0.2.1"].prefix_length == 24

    def test_normalized_keeps_vendor_extensions(self):
        facts = CollectedFacts()
        facts.interfaces["ether1"] = {"is_up": True, "poe_class": 4}
        assert facts.normalized().interfaces["ether1"].poe_class == 4

    def test_a_collector_may_hand_back_plain_dicts_throughout(self):
        facts = CollectedFacts.model_validate(
            {"facts": {"hostname": "rb1"}, "interfaces": {"ether1": {"is_up": "up"}}}
        )
        assert facts.normalized().interfaces["ether1"].is_up is True


class TestDiscoveredIP:
    def test_family_and_prefix_are_derived_not_trusted(self):
        # A collector that mislabels the family is a bug that used to surface as
        # a confusing rejection from the source of truth.
        ip = DiscoveredIP(
            address="2001:db8::1/64", interface="Lo0", ip_version="ipv4", prefix_length=32
        )
        assert ip.ip_version == "ipv6"
        assert ip.prefix_length == 64

    def test_host_address_inside_a_subnet_is_valid(self):
        assert DiscoveredIP(address="10.0.0.1/24", interface="Gi0/0").ip == "10.0.0.1"

    @pytest.mark.parametrize("bad", ["10.0.0.1/Vlan10", "not-an-ip", "10.0.0.1/99"])
    def test_unparseable_addresses_are_rejected(self, bad):
        with pytest.raises(ValidationError):
            DiscoveredIP(address=bad, interface="Gi0/0")

    def test_interface_is_required(self):
        with pytest.raises(ValidationError):
            DiscoveredIP(address="10.0.0.1/24", interface="")


class TestDiscoveredInterface:
    def test_original_name_defaults_to_the_canonical_one(self):
        assert DiscoveredInterface(name="GigabitEthernet0/0").original_name == "GigabitEthernet0/0"

    def test_original_name_is_kept_when_given(self):
        intf = DiscoveredInterface(name="GigabitEthernet0/0", original_name="Gi0/0")
        assert intf.original_name == "Gi0/0"

    def test_a_nameless_interface_is_rejected(self):
        with pytest.raises(ValidationError):
            DiscoveredInterface(name="")


class TestDiscoveredLLDPNeighbor:
    def test_both_ends_are_required(self):
        # A neighbour named at only one end says nothing placeable.
        with pytest.raises(ValidationError):
            DiscoveredLLDPNeighbor(local_interface="Gi0/0", remote_hostname="R2", remote_interface="")


class TestDiscoveryResult:
    def test_round_trips_through_json(self):
        # This is what lets a run collect on a jump host and sync elsewhere.
        result = DiscoveryResult(
            device=DiscoveredDevice(hostname="R1", vendor="Cisco"),
            interfaces=[DiscoveredInterface(name="Gi0/0", vrf="BLUE")],
            ip_addresses=[DiscoveredIP(address="10.0.0.1/24", interface="Gi0/0")],
            device_name="R1",
            platform="ios",
        )
        restored = DiscoveryResult.model_validate_json(result.model_dump_json())
        assert restored == result
        assert restored.interfaces[0].vrf == "BLUE"

    def test_is_usable_requires_a_name_and_interfaces(self):
        assert not DiscoveryResult().is_usable()
        assert not DiscoveryResult(device=DiscoveredDevice(hostname="R1")).is_usable()
        assert DiscoveryResult(
            device=DiscoveredDevice(hostname="R1"),
            interfaces=[DiscoveredInterface(name="Gi0/0")],
        ).is_usable()

    def test_netbox_device_name_still_reads_and_writes(self):
        # The field was renamed when sinks became pluggable.
        result = DiscoveryResult()
        result.netbox_device_name = "R1"
        assert result.device_name == "R1"
        assert result.netbox_device_name == "R1"
        assert "netbox_device_name" not in result.model_dump()

    def test_former_names_still_resolve(self):
        assert ProcessedData is DiscoveryResult
        assert SyncStats is SyncReport


class TestSyncReport:
    def test_partly_landed_is_not_a_success(self):
        assert SyncReport().succeeded
        assert not SyncReport(ip_addresses_failed=1).succeeded
        assert not SyncReport(errors=["boom"]).succeeded

    def test_a_duplicate_address_does_not_fail_the_run(self):
        # It is a data condition on the device, not a sync failure.
        assert SyncReport(ip_addresses_duplicate=2).succeeded
