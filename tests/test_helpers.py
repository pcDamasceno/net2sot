"""
Tests for the shared normalization helpers.

These functions decide what every device looks like in NetBox, and they are
pure — no device, no NetBox, no network. Every case below is a bug that
reached production data at least once.
"""

import re

import pytest

from net2sot.helpers import (
    determine_device_type,
    extract_interface_type,
    is_management_interface,
    is_virtual_interface,
    normalize_interface_name,
    normalize_mac_address,
    should_include_interface,
)
from net2sot.netbox_client import slugify


class TestNormalizeInterfaceName:
    @pytest.mark.parametrize(
        "abbreviated,canonical",
        [
            ("Gi0/0", "GigabitEthernet0/0"),
            ("Te0/1", "TenGigabitEthernet0/1"),
            ("Fa0/1", "FastEthernet0/1"),
            ("Po10", "Port-channel10"),
            ("Lo0", "Loopback0"),
            ("Vl100", "Vlan100"),
            ("Tu1", "Tunnel1"),
            ("mgmt0", "Management0"),
            ("Eth1/1", "Ethernet1/1"),
        ],
    )
    def test_expands_abbreviations(self, abbreviated, canonical):
        assert normalize_interface_name(abbreviated) == canonical

    @pytest.mark.parametrize(
        "name",
        [
            "GigabitEthernet0/0",
            "TenGigabitEthernet0/1",
            "HundredGigabitEthernet1/1",
            "Port-channel10",
            "Loopback0",
        ],
    )
    def test_preserves_canonical_casing(self, name):
        # Regression: a trailing .capitalize() used to lowercase everything past
        # the first character, so "TenGigabitEthernet0/1" reached NetBox as
        # "Tengigabitethernet0/1".
        assert normalize_interface_name(name) == name

    def test_preserves_iosxr_management_spelling(self):
        assert normalize_interface_name("MgmtEth0/RP0/CPU0/0") == "MgmtEth0/RP0/CPU0/0"

    def test_preserves_srlinux_lowercase_names(self):
        # SR Linux ports really are named "ethernet-1/1" on the box.
        assert normalize_interface_name("ethernet-1/1") == "ethernet-1/1"

    def test_is_idempotent(self):
        once = normalize_interface_name("Gi0/0")
        assert normalize_interface_name(once) == once

    @pytest.mark.parametrize("empty", ["", None])
    def test_passes_through_empty(self, empty):
        assert normalize_interface_name(empty) == empty


class TestExtractInterfaceType:
    @pytest.mark.parametrize(
        "name,expected",
        [
            # Regression: an unanchored substring test matched "gigabit" inside
            # "TenGigabitEthernet" and typed every 10G+ port as 1000base-t.
            ("TenGigabitEthernet0/1", "10gbase-t"),
            ("FortyGigabitEthernet1/1", "40gbase-x-qsfpp"),
            ("HundredGigabitEthernet1/1", "100gbase-x-qsfp28"),
            ("TwentyFiveGigE1/1", "25gbase-x-sfp28"),
            ("GigabitEthernet0/0", "1000base-t"),
            ("FastEthernet0/1", "100base-tx"),
            ("Ethernet1/1", "1000base-t"),
            # Regression: port-channels fell through to "other".
            ("Port-channel10", "lag"),
            ("Loopback0", "virtual"),
            ("Vlan100", "virtual"),
            ("Tunnel1", "virtual"),
            ("Null0", "virtual"),
            ("Management1", "1000base-t"),
            ("MgmtEth0/RP0/CPU0/0", "1000base-t"),
            ("GigabitEthernet0/0.100", "virtual"),
            ("Serial0/0", "other"),
        ],
    )
    def test_maps_canonical_name_to_netbox_type(self, name, expected):
        assert extract_interface_type(name) == expected

    def test_empty_name_is_other(self):
        assert extract_interface_type("") == "other"


class TestIsManagementInterface:
    @pytest.mark.parametrize(
        "name",
        [
            "Management1",
            "management1",
            "mgmt0",
            # Regression: IOS-XR's spelling missed a case-sensitive "^mgmt",
            # costing the device its primary IP and inventing OOB cables.
            "MgmtEth0/RP0/CPU0/0",
            "Mgmt0",
            "ma0",
            "fxp0",
            "em0",
        ],
    )
    def test_detects_management_interfaces(self, name):
        assert is_management_interface(name) is True

    @pytest.mark.parametrize(
        "name", ["GigabitEthernet0/0", "Loopback0", "Ethernet1/1", "Port-channel10", ""]
    )
    def test_rejects_non_management_interfaces(self, name):
        assert is_management_interface(name) is False


class TestIsVirtualInterface:
    @pytest.mark.parametrize(
        "name", ["Loopback0", "Vlan100", "Tunnel1", "GigabitEthernet0/0.100", "Null0"]
    )
    def test_detects_virtual(self, name):
        assert is_virtual_interface(name) is True

    @pytest.mark.parametrize("name", ["GigabitEthernet0/0", "Ethernet1/1", ""])
    def test_detects_physical(self, name):
        assert is_virtual_interface(name) is False


class TestNormalizeMacAddress:
    @pytest.mark.parametrize(
        "raw",
        ["00:1A:2B:3C:4D:5E", "001A.2B3C.4D5E", "00-1A-2B-3C-4D-5E", "001a2b3c4d5e"],
    )
    def test_accepts_common_vendor_formats(self, raw):
        assert normalize_mac_address(raw) == "00:1a:2b:3c:4d:5e"

    @pytest.mark.parametrize("junk", ["", None, "not-a-mac", "00:1A:2B:3C:4D", "None"])
    def test_rejects_junk(self, junk):
        assert normalize_mac_address(junk) is None


class TestShouldIncludeInterface:
    FILTERS = {"exclude_patterns": ["^Null.*", "^Async.*"], "include_patterns": []}

    def test_excludes_matching_pattern(self):
        assert should_include_interface("Null0", self.FILTERS) is False

    def test_includes_everything_else_when_no_include_list(self):
        assert should_include_interface("GigabitEthernet0/0", self.FILTERS) is True

    def test_include_list_restricts_to_matches(self):
        filters = {"exclude_patterns": [], "include_patterns": ["^GigabitEthernet"]}
        assert should_include_interface("GigabitEthernet0/0", filters) is True
        assert should_include_interface("Loopback0", filters) is False

    def test_exclude_wins_over_include(self):
        filters = {"exclude_patterns": ["^Null.*"], "include_patterns": ["^Null.*"]}
        assert should_include_interface("Null0", filters) is False


class TestDetermineDeviceType:
    MAPPING = {
        "cisco": {
            "default": "cisco-generic",
            "patterns": [
                {"regex": ".*iol.*", "type": "cisco_iol"},
                {"regex": ".*Nexus.*", "type": "nx-os"},
            ],
        }
    }

    def test_curated_pattern_wins(self):
        assert determine_device_type("Cisco IOL", "cisco", self.MAPPING) == "cisco_iol"

    def test_is_case_insensitive(self):
        assert determine_device_type("NEXUS 9000", "Cisco", self.MAPPING) == "nx-os"

    @pytest.mark.parametrize("model", ["CSR1000V", "ISR4331/K9", "MX960", "7220 IXR-D3L"])
    def test_identified_model_is_used_as_its_own_type(self, model):
        # A model the device actually reported is worth creating in NetBox as
        # itself. It used to be collapsed into the vendor default, which threw
        # away the one fact we went and asked the device for.
        assert determine_device_type(model, "cisco", self.MAPPING) == model

    def test_identified_model_beats_vendor_default_for_unmapped_vendor(self):
        assert determine_device_type("MX960", "juniper", self.MAPPING) == "MX960"

    @pytest.mark.parametrize("model", ["", "Unknown", "unknown", "N/A", "  ", None])
    def test_unidentified_model_falls_back_to_vendor_default(self, model):
        assert determine_device_type(model, "cisco", self.MAPPING) == "cisco-generic"

    @pytest.mark.parametrize("model", ["", "Unknown", None])
    def test_unidentified_model_and_unmapped_vendor_is_unknown(self, model):
        assert determine_device_type(model, "juniper", self.MAPPING) == "unknown"

    def test_strips_surrounding_whitespace(self):
        assert determine_device_type("  CSR1000V  ", "cisco", self.MAPPING) == "CSR1000V"


class TestSlugify:
    @pytest.mark.parametrize(
        "value,expected",
        [
            # NetBox rejects a slash; real Cisco models have them.
            ("ISR4331/K9", "isr4331-k9"),
            ("7220 IXR-D3L", "7220-ixr-d3l"),
            ("Nexus 9000 (C9300v)", "nexus-9000-c9300v"),
            ("cEOS", "ceos"),
            # Underscores are legal in a NetBox slug, so a mapping that pins
            # "cisco_iol" keeps resolving to the slug it already has.
            ("cisco_iol", "cisco_iol"),
            ("cisco-generic", "cisco-generic"),
            ("  spaced  ", "spaced"),
            ("--leading-and-trailing--", "leading-and-trailing"),
        ],
    )
    def test_produces_valid_netbox_slugs(self, value, expected):
        assert slugify(value) == expected

    @pytest.mark.parametrize("value", ["", None, "///", "!!!"])
    def test_degenerate_input_never_yields_an_empty_slug(self, value):
        # An empty slug is a 400 from NetBox; "unknown" at least round-trips.
        assert slugify(value) == "unknown"

    @pytest.mark.parametrize(
        "value", ["ISR4331/K9", "7220 IXR-D3L", "Nexus 9000 (C9300v)", "cisco_iol", ""]
    )
    def test_output_always_matches_netbox_slug_charset(self, value):
        assert re.fullmatch(r"[-a-zA-Z0-9_]+", slugify(value))
