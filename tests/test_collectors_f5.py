"""
Tests for the F5 BIG-IP collector plugin.

The fixtures are real tmsh output. ntc-templates has no F5 templates, so the
brace-block parser in the plugin is the only thing standing between tmsh and the
contract -- these cover it directly.
"""


from net2sot.collectors.f5 import (
    F5Collector,
    _facts,
    _interface_type,
    _interfaces,
    _interfaces_ip,
    _strip_partition,
    _tmsh_blocks,
)


class TestPluginRegistration:
    def test_claims_the_bigip_platforms(self):
        assert F5Collector.name == "f5"
        assert F5Collector.supports("f5")
        assert F5Collector.supports("BIGIP")
        assert not F5Collector.supports("paloalto")


F5_VERSION = """
Sys::Version
Main Package
  Product     BIG-IP
  Version     17.5.0
  Build       0.0.15
  Edition     Final
  Date        Wed Feb 19 02:13:04 PST 2025
"""

F5_HARDWARE = """sys hardware platform {
    base-mac 00:50:00:00:15:00
    bios-rev
    marketing-name BIG-IP Virtual Edition
}
sys hardware system-info {
    bigip-chassis-serial-num 794b20d3-09e7-aa4e-f29cba6e2c5f
    host-board-serial-num
    platform Z100
}"""

F5_HOSTNAME = """sys global-settings {
    hostname bigip1
}"""

F5_INTERFACE_LIST = """net interface 1.1 {
    if-index 48
    mac-address 00:50:00:00:15:01
    media-fixed 10000T-FD
    media-max auto
    mtu 9198
}
net interface 1.4 {
    disabled
    if-index 96
    mac-address 00:50:00:00:15:04
    media-fixed 1000SX-FD
}
net interface mgmt {
    if-index 32
    mac-address 00:50:00:00:15:00
    media-active 100TX-FD
}"""

F5_INTERFACE_STATUS = """net interface 1.1 {
    media-active none
    name 1.1
    status uninit
}
net interface mgmt {
    media-active 100TX-FD
    name mgmt
    status up
}"""

F5_VLANS = """net vlan /Common/internal {
    if-index 112
    interfaces {
        1.2 {
            tagged
        }
    }
    tag 4093
}"""

F5_SELFS = """net self /Common/internal_self {
    address 10.1.1.5%1/24
    allow-service {
        default
    }
    traffic-group /Common/traffic-group-local-only
    vlan /Common/internal
}"""

F5_MGMT_IP = """sys management-ip 10.10.10.111/24 {
    description static-fallback
}"""


class TestF5Facts:
    def test_version_hardware_and_hostname(self):
        facts = _facts(F5_VERSION, F5_HARDWARE, F5_HOSTNAME)
        assert facts["hostname"] == "bigip1"
        # The marketing name, not the Z100 platform code, is the model.
        assert facts["model"] == "BIG-IP Virtual Edition"
        assert facts["serial_number"] == "794b20d3-09e7-aa4e-f29cba6e2c5f"
        assert facts["os_version"] == "17.5.0"
        assert facts["vendor"] == "F5 Networks"

    def test_blank_hardware_falls_back_to_product(self):
        facts = _facts(F5_VERSION, "", "")
        assert facts["model"] == "BIG-IP"
        assert facts["serial_number"] == ""
        assert facts["hostname"] == ""


class TestTmshBlocks:
    def test_nested_blocks_and_flags(self):
        blocks = _tmsh_blocks(F5_VLANS)
        body = blocks["net vlan /Common/internal"]
        assert body["tag"] == "4093"
        # A nested block becomes a dict, and a bare word (tmsh's way of writing
        # a flag) an entry with an empty value.
        assert body["interfaces"] == {"1.2": {"tagged": ""}}

    def test_space_padded_empty_value(self):
        # tmsh pads an unset field with spaces: 'bios-rev  '.
        blocks = _tmsh_blocks("sys hardware platform {\n    bios-rev  \n}")
        assert blocks["sys hardware platform"] == {"bios-rev": ""}

    def test_value_may_contain_spaces(self):
        blocks = _tmsh_blocks("sys hardware platform {\n    marketing-name BIG-IP Virtual Edition\n}")
        assert blocks["sys hardware platform"]["marketing-name"] == "BIG-IP Virtual Edition"


class TestF5StripPartition:
    def test_partition_is_removed(self):
        assert _strip_partition("/Common/internal") == "internal"

    def test_management_address_keeps_its_prefix_length(self):
        # The address is the block's name; stripping on '/' would leave "24".
        assert _strip_partition("10.10.10.111/24") == "10.10.10.111/24"

    def test_plain_name_untouched(self):
        assert _strip_partition("1.1") == "1.1"


class TestF5Interfaces:
    def test_ports_merge_config_and_status(self):
        interfaces = _interfaces(F5_INTERFACE_LIST, F5_INTERFACE_STATUS, "")
        port = interfaces["1.1"]
        assert port["mac_address"] == "00:50:00:00:15:01"
        assert port["mtu"] == 9198
        # Speed comes from media-fixed: a dark port's media-active is "none".
        assert port["speed"] == 10000
        assert port["netbox_type"] == "10gbase-t"
        # 'uninit' is dark, not disabled -- inventoried, reported down.
        assert port["is_enabled"] is True
        assert port["is_up"] is False

    def test_disabled_flag_is_honoured(self):
        interfaces = _interfaces(F5_INTERFACE_LIST, F5_INTERFACE_STATUS, "")
        assert interfaces["1.4"]["is_enabled"] is False
        # 1000SX is fibre, so it must not be typed as copper.
        assert interfaces["1.4"]["netbox_type"] == "1000base-x-sfp"

    def test_management_port(self):
        interfaces = _interfaces(F5_INTERFACE_LIST, F5_INTERFACE_STATUS, "")
        assert interfaces["mgmt"]["is_up"] is True
        assert interfaces["mgmt"]["netbox_type"] == "100base-tx"

    def test_vlans_become_virtual_interfaces(self):
        # Self-IPs sit on VLANs, so the VLANs have to exist as interfaces.
        interfaces = _interfaces(F5_INTERFACE_LIST, F5_INTERFACE_STATUS, F5_VLANS)
        assert interfaces["internal"]["netbox_type"] == "virtual"
        assert interfaces["internal"]["is_virtual"] is True

    def test_interface_type_by_media(self):
        assert _interface_type(10000, "T") == "10gbase-t"
        assert _interface_type(10000, "SR") == "10gbase-x-sfpp"
        assert _interface_type(1000, "T") == "1000base-t"
        assert _interface_type(40000, "SR4") == "40gbase-x-qsfpp"
        assert _interface_type(0, "") == "other"


class TestF5InterfacesIp:
    def test_self_ip_lands_on_its_vlan(self):
        ips = _interfaces_ip(F5_SELFS, "")
        # Route domain ('%1') is stripped; the VLAN is the interface.
        assert ips == {"internal": {"ipv4": {"10.1.1.5": {"prefix_length": 24}}}}

    def test_management_ip_lands_on_mgmt(self):
        ips = _interfaces_ip("", F5_MGMT_IP)
        assert ips == {"mgmt": {"ipv4": {"10.10.10.111": {"prefix_length": 24}}}}

    def test_ipv6_self_ip(self):
        selfs = """net self /Common/v6 {
    address 2001:db8::5/64
    vlan /Common/external
}"""
        assert _interfaces_ip(selfs, "") == {
            "external": {"ipv6": {"2001:db8::5": {"prefix_length": 64}}}
        }

    def test_nothing_configured_yields_nothing(self):
        assert _interfaces_ip("", "") == {}
