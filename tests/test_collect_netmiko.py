"""
Tests for the Netmiko collection path, focused on the parts that differ per
platform: the shared TextFSM converters must absorb the field-name differences
between ios/eos/nxos ntc-templates, and interface IPs are read from the
'show interfaces' output rather than a separate command.

Field names used here mirror the real ntc-templates shipped with netmiko
(verified against the installed templates), so a converter that only knew the
cisco_ios spelling would visibly fail these.
"""

import pytest

from net2sot.tasks.collect import (
    _convert_cdp,
    _convert_facts,
    _convert_interfaces,
    _convert_lldp,
    _first,
)
from net2sot.tasks.collect_netmiko import (
    _COLLECTORS,
    _MOVED_TO_PLUGINS,
    _interfaces_ip_from_rows,
    collect_netmiko,
)


class TestFirst:
    def test_returns_first_non_empty(self):
        assert _first({"A": "", "B": "x", "C": "y"}, "A", "B", "C") == "x"

    def test_flattens_single_element_lists(self):
        # List-type template values (HARDWARE, SERIAL) arrive as one-item lists.
        assert _first({"HARDWARE": ["ISR4331/K9"]}, "HARDWARE") == "ISR4331/K9"

    def test_empty_when_nothing_present(self):
        assert _first({"A": "", "B": []}, "A", "B", "MISSING") == ""


class TestConvertFactsTextfsm:
    def test_cisco_ios(self):
        rows = [{"HOSTNAME": "CORE-RR-01", "VERSION": "15.9(3)M",
                 "HARDWARE": ["IOL"], "SERIAL": ["ABC123"]}]
        facts = _convert_facts(rows, "textfsm", "ios")
        assert facts["hostname"] == "CORE-RR-01"
        assert facts["os_version"] == "15.9(3)M"
        assert facts["model"] == "IOL"
        assert facts["serial_number"] == "ABC123"
        assert facts["vendor"] == "Cisco"

    def test_arista_eos_uses_model_serial_image_and_has_no_hostname(self):
        # arista_eos show version: MODEL / SERIAL_NUMBER / IMAGE, no HOSTNAME.
        rows = [{"MODEL": "cEOSLab", "SERIAL_NUMBER": "SN-EOS-1",
                 "IMAGE": "4.31.2F", "HW_VERSION": "1.0"}]
        facts = _convert_facts(rows, "textfsm", "eos")
        assert facts["hostname"] == ""          # recovered from prompt by caller
        assert facts["model"] == "cEOSLab"
        assert facts["serial_number"] == "SN-EOS-1"
        assert facts["os_version"] == "4.31.2F"
        assert facts["vendor"] == "Arista"

    def test_cisco_nxos_uses_platform_os_serial(self):
        rows = [{"HOSTNAME": "NX1", "PLATFORM": "Nexus9000",
                 "OS": "9.3(10)", "SERIAL": "SN-NX-1"}]
        facts = _convert_facts(rows, "textfsm", "nxos")
        assert facts["hostname"] == "NX1"
        assert facts["model"] == "Nexus9000"
        assert facts["os_version"] == "9.3(10)"
        assert facts["serial_number"] == "SN-NX-1"
        assert facts["vendor"] == "Cisco"

    def test_missing_model_falls_back_to_unknown(self):
        facts = _convert_facts([{"HOSTNAME": "X"}], "textfsm", "ios")
        assert facts["model"] == "Unknown"


class TestInterfacesIpFromRows:
    def test_ios_nxos_ip_plus_prefix_length(self):
        rows = [{"INTERFACE": "GigabitEthernet0/0",
                 "IP_ADDRESS": "10.0.0.1", "PREFIX_LENGTH": "24"}]
        result = _interfaces_ip_from_rows(rows)
        assert result == {"GigabitEthernet0/0": {"ipv4": {"10.0.0.1": {"prefix_length": 24}}}}

    def test_eos_embedded_prefix(self):
        # Arista's IP_ADDRESS already carries the mask, e.g. "10.0.0.1/24".
        rows = [{"INTERFACE": "Ethernet1", "IP_ADDRESS": "10.0.0.1/24"}]
        result = _interfaces_ip_from_rows(rows)
        assert result == {"Ethernet1": {"ipv4": {"10.0.0.1": {"prefix_length": 24}}}}

    def test_unassigned_and_empty_are_skipped(self):
        rows = [
            {"INTERFACE": "Ethernet2", "IP_ADDRESS": "unassigned"},
            {"INTERFACE": "Ethernet3", "IP_ADDRESS": ""},
            {"INTERFACE": "", "IP_ADDRESS": "10.0.0.9/24"},
        ]
        assert _interfaces_ip_from_rows(rows) == {}

    def test_defaults_to_32_when_no_prefix_anywhere(self):
        rows = [{"INTERFACE": "Loopback0", "IP_ADDRESS": "1.1.1.1"}]
        result = _interfaces_ip_from_rows(rows)
        assert result["Loopback0"]["ipv4"]["1.1.1.1"]["prefix_length"] == 32


class TestConvertInterfacesMac:
    def test_prefers_mac_address_field(self):
        rows = [{"INTERFACE": "Ethernet1", "LINK_STATUS": "up",
                 "PROTOCOL_STATUS": "up", "MAC_ADDRESS": "aa:bb:cc:dd:ee:ff",
                 "BIA": "11:22:33:44:55:66", "MTU": "1500", "BANDWIDTH": "1000000"}]
        interfaces = _convert_interfaces(rows, "textfsm")
        assert interfaces["Ethernet1"]["mac_address"] == "aa:bb:cc:dd:ee:ff"


class TestConvertLldpTextfsm:
    def test_neighbor_name_and_description_fields(self):
        rows = [{"LOCAL_INTERFACE": "Ethernet1", "NEIGHBOR_NAME": "PE-EMEA-02",
                 "NEIGHBOR_INTERFACE": "Ethernet1",
                 "NEIGHBOR_DESCRIPTION": "Arista cEOS", "CHASSIS_ID": "de:ad:be:ef"}]
        neighbors, details = _convert_lldp(rows, "textfsm")
        assert neighbors["Ethernet1"][0]["hostname"] == "PE-EMEA-02"
        assert neighbors["Ethernet1"][0]["port"] == "Ethernet1"
        assert details["Ethernet1"][0]["remote_system_description"] == "Arista cEOS"


class TestConvertCdpTextfsm:
    def test_neighbor_name_and_mgmt_address_fields(self):
        rows = [{"LOCAL_INTERFACE": "GigabitEthernet0/1",
                 "NEIGHBOR_NAME": "CORE-RR-01.lab", "NEIGHBOR_INTERFACE": "Gi0/2",
                 "MGMT_ADDRESS": "172.20.20.13", "NEIGHBOR_DESCRIPTION": "IOS"}]
        neighbors, details = _convert_cdp(rows, "textfsm")
        # Domain is stripped from the CDP neighbour name.
        assert neighbors["GigabitEthernet0/1"][0]["hostname"] == "CORE-RR-01"
        assert details["GigabitEthernet0/1"][0]["remote_chassis_id"] == "172.20.20.13"


# Rows exactly as ntc-templates parses a PAN-OS 'show interface all': the
# hardware section first (speed/duplex/state + MAC, no addresses), then the
# logical section (vsys/zone/forwarding + addresses, no MAC), with a configured
# port appearing in both. '[n/a]/[n/a]/up' is reproduced as the template really
# slices it -- the placeholders torn in half, the state pushed into FEC.
class TestCollectorRegistry:
    def test_ios_eos_nxos_are_registered(self):
        assert {"ios", "eos", "nxos", "iosxr", "srlinux", "linux"} <= set(_COLLECTORS)

    def test_unknown_platform_lists_supported(self):
        with pytest.raises(ValueError) as exc:
            collect_netmiko(task=None, platform="junos")
        message = str(exc.value)
        assert "junos" in message
        for platform in ("ios", "eos", "nxos", "iosxr", "srlinux"):
            assert platform in message

    @pytest.mark.parametrize("platform", sorted(_MOVED_TO_PLUGINS))
    def test_a_platform_that_became_a_plugin_says_where_it_went(self, platform):
        # An inventory pinning `collector: netmiko` for one of these predates
        # the move. "No Netmiko collector for platform 'f5'" would be true but
        # unhelpful -- the platform is still supported, just not from here.
        assert platform not in _COLLECTORS
        with pytest.raises(ValueError) as exc:
            collect_netmiko(task=None, platform=platform)
        message = str(exc.value)
        assert _MOVED_TO_PLUGINS[platform] in message
        assert "groups.yaml" in message
