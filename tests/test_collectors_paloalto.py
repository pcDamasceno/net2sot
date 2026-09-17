"""
Tests for the Palo Alto PAN-OS collector plugin.

The fixtures are real 'show interface all' / 'show system info' rows as
ntc-templates parses them, so a change to the template spellings shows up here
rather than against a firewall.
"""


from net2sot.collectors.paloalto import (
    PaloAltoCollector,
    _convert_interfaces,
    _facts,
    _interface_type,
    _link_state,
    _management,
    _uptime_seconds,
)


class TestPluginRegistration:
    def test_claims_the_panos_platforms(self):
        assert PaloAltoCollector.name == "paloalto"
        assert PaloAltoCollector.supports("paloalto")
        assert PaloAltoCollector.supports("PANOS")
        assert not PaloAltoCollector.supports("f5")


PAN_INTERFACE_ROWS = [
    {"INTERFACE": "ethernet1/1", "ID": "16", "SPEED": "1000", "DUPLEX": "full",
     "STATE": "up", "FEC": "", "MAC_ADDRESS": "00:50:56:aa:bb:01",
     "VSYS": "", "ZONE": "", "FORWARDING": "", "VLAN_ID": "", "IP_ADDRESS": []},
    {"INTERFACE": "ethernet1/3", "ID": "18", "SPEED": "[n/a]", "DUPLEX": "[n",
     "STATE": "a]", "FEC": "down", "MAC_ADDRESS": "00:50:56:aa:bb:03",
     "VSYS": "", "ZONE": "", "FORWARDING": "", "VLAN_ID": "", "IP_ADDRESS": []},
    {"INTERFACE": "ae1", "ID": "19", "SPEED": "[n/a]", "DUPLEX": "[n",
     "STATE": "a]", "FEC": "up", "MAC_ADDRESS": "00:50:56:aa:bb:04",
     "VSYS": "", "ZONE": "", "FORWARDING": "", "VLAN_ID": "", "IP_ADDRESS": []},
    {"INTERFACE": "ethernet1/1", "ID": "16", "SPEED": "", "DUPLEX": "",
     "STATE": "", "FEC": "", "MAC_ADDRESS": "", "VSYS": "1", "ZONE": "trust",
     "FORWARDING": "vr:default", "VLAN_ID": "0", "IP_ADDRESS": ["10.10.20.1/24"]},
    {"INTERFACE": "ethernet1/2.100", "ID": "18", "SPEED": "", "DUPLEX": "",
     "STATE": "", "FEC": "", "MAC_ADDRESS": "", "VSYS": "1", "ZONE": "dmz",
     "FORWARDING": "vr:default", "VLAN_ID": "100",
     "IP_ADDRESS": ["198.51.100.1/24"]},
    {"INTERFACE": "tunnel.1", "ID": "21", "SPEED": "", "DUPLEX": "", "STATE": "",
     "FEC": "", "MAC_ADDRESS": "", "VSYS": "1", "ZONE": "vpn",
     "FORWARDING": "vr:default", "VLAN_ID": "0", "IP_ADDRESS": ["N/A"]},
]

PAN_SYSTEM_INFO_ROWS = [{
    "HOSTNAME": "FW-DC1", "IP_ADDRESS": "192.0.2.1",
    "NETMASK": "255.255.255.0", "GATEWAY": "192.0.2.254",
    "MAC_ADDRESS": "00:50:56:aa:bb:00", "UPTIME": "38 days, 2:11:15",
    "FAMILY": "vm", "MODEL": "PA-VM", "SERIAL": "007951000123456",
    "OS": "11.1.2-h3",
}]


class TestPaloAltoFacts:
    def test_system_info_maps_to_napalm_facts(self):
        facts = _facts(PAN_SYSTEM_INFO_ROWS)
        assert facts["hostname"] == "FW-DC1"
        assert facts["model"] == "PA-VM"
        assert facts["serial_number"] == "007951000123456"
        assert facts["os_version"] == "11.1.2-h3"
        assert facts["vendor"] == "Palo Alto Networks"

    def test_uptime_is_converted_to_seconds(self):
        assert _uptime_seconds("38 days, 2:11:15") == 38 * 86400 + 2 * 3600 + 11 * 60 + 15
        assert _uptime_seconds("2:11:15") == 2 * 3600 + 11 * 60 + 15
        assert _uptime_seconds("") == 0
        assert _uptime_seconds("unknown") == 0


class TestPaloAltoLinkState:
    def test_plain_three_field_column(self):
        assert _link_state({"STATE": "up", "FEC": ""}) == "up"

    def test_fec_capable_port_keeps_state_in_state(self):
        # '10000/full/up/rs-fec' slices cleanly, so FEC is not a state.
        assert _link_state({"STATE": "up", "FEC": "rs-fec"}) == "up"

    def test_placeholder_column_leaves_the_state_in_fec(self):
        # '[n/a]/[n/a]/up' -> ('[n/a]', '[n', 'a]', 'up'): an aggregate that is
        # up must not be read as down just because the template mis-sliced it.
        assert _link_state({"STATE": "a]", "FEC": "up"}) == "up"
        assert _link_state({"STATE": "a]", "FEC": "down"}) == "down"

    def test_logical_interface_has_no_link_state(self):
        assert _link_state({"STATE": "", "FEC": ""}) == ""


class TestPaloAltoInterfaces:
    def test_hardware_and_logical_rows_are_merged(self):
        interfaces, interfaces_ip = _convert_interfaces(PAN_INTERFACE_ROWS)
        # ethernet1/1 appears in both sections: the MAC and speed from the
        # hardware row survive the logical row that carries its address.
        assert interfaces["ethernet1/1"]["mac_address"] == "00:50:56:aa:bb:01"
        assert interfaces["ethernet1/1"]["speed"] == 1000
        assert interfaces_ip["ethernet1/1"] == {
            "ipv4": {"10.10.20.1": {"prefix_length": 24}}
        }

    def test_dark_port_is_down_but_still_inventoried(self):
        interfaces, _ = _convert_interfaces(PAN_INTERFACE_ROWS)
        # PAN-OS reports no admin state, so a dark port stays enabled (and is
        # therefore not dropped by interface_filters.exclude_disabled).
        assert interfaces["ethernet1/3"]["is_enabled"] is True
        assert interfaces["ethernet1/3"]["is_up"] is False

    def test_aggregate_up_despite_placeholder_speed(self):
        interfaces, _ = _convert_interfaces(PAN_INTERFACE_ROWS)
        assert interfaces["ae1"]["is_up"] is True
        assert interfaces["ae1"]["speed"] == 0      # '[n/a]' is not a speed
        assert interfaces["ae1"]["netbox_type"] == "lag"

    def test_unnumbered_interface_has_no_address(self):
        _, interfaces_ip = _convert_interfaces(PAN_INTERFACE_ROWS)
        assert "tunnel.1" not in interfaces_ip          # 'N/A' is not an address
        assert interfaces_ip["ethernet1/2.100"] == {
            "ipv4": {"198.51.100.1": {"prefix_length": 24}}
        }

    def test_interface_types(self):
        interfaces, _ = _convert_interfaces(PAN_INTERFACE_ROWS)
        # Typed from the speed the hardware row reported, and from the name for
        # everything PAN-OS names by function.
        assert interfaces["ethernet1/1"]["netbox_type"] == "1000base-t"
        assert interfaces["ethernet1/1"]["is_virtual"] is False
        assert interfaces["ethernet1/2.100"]["netbox_type"] == "virtual"
        assert interfaces["ethernet1/2.100"]["is_virtual"] is True
        assert interfaces["tunnel.1"]["netbox_type"] == "virtual"
        # A port whose speed PAN-OS never negotiated gets no media guess.
        assert interfaces["ethernet1/3"]["netbox_type"] == "other"

    def test_interface_type_by_name(self):
        assert _interface_type("loopback.1", 0) == "virtual"
        assert _interface_type("vlan.5", 0) == "virtual"
        assert _interface_type("ae2", 0) == "lag"
        assert _interface_type("management", 0) == "1000base-t"
        assert _interface_type("ethernet1/1", 10000) == "10gbase-x-sfpp"

    def test_ipv6_address_lands_in_its_own_family(self):
        rows = [{"INTERFACE": "ethernet1/4", "IP_ADDRESS": ["2001:db8::1/64"]}]
        _, interfaces_ip = _convert_interfaces(rows)
        assert interfaces_ip["ethernet1/4"] == {
            "ipv6": {"2001:db8::1": {"prefix_length": 64}}
        }


class TestPaloAltoManagementInterface:
    def test_rebuilt_from_system_info(self):
        # 'show interface all' never lists the management port, so without this
        # the firewall would reach NetBox with no primary IP.
        interface, addresses = _management(PAN_SYSTEM_INFO_ROWS)
        assert interface["mac_address"] == "00:50:56:aa:bb:00"
        assert interface["netbox_type"] == "1000base-t"
        # Dotted netmask, as PAN-OS prints it, becomes a prefix length.
        assert addresses == {"ipv4": {"192.0.2.1": {"prefix_length": 24}}}

    def test_absent_when_system_info_carried_nothing(self):
        assert _management([]) == ({}, {})


# tmsh output as a BIG-IP 17.5.0 VE really prints it (captured from the lab
# appliance), except for the VLAN/self-IP blocks: that box has none configured,
# so those follow the documented tmsh format.
