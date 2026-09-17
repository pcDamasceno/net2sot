"""
Tests for VRF import/export route-target discovery.

Route targets are the one part of a VRF that NAPALM's get_network_instances does
not carry, so they are fetched with a per-platform command and parsed here. The
device output in these tests is real output captured from the lab (Arista cEOS
4.33, Cisco IOL 15.9), not hand-written approximations, because the whole point
of these parsers is to survive the exact shape the boxes emit.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from net2sot.netbox_client import NetboxClient
from net2sot.tasks.collect import (
    _parse_route_targets_cisco_text,
    _parse_route_targets_eos,
    _parse_vrf_membership_cisco_text,
)
from net2sot.tasks.process import _build_vrf_map

# 'show bgp instance vrf all | json' from PE-EMEA-01 (cEOS), trimmed to the keys
# the parser reads. Targets are nested one level deeper than you would expect:
# afiSafiConfig -> address family -> the family that carries the target.
EOS_BGP_JSON = json.dumps({
    "vrfs": {
        "default": {
            "localAs": 65010,
            "afiSafiConfig": {"v4u": {"routeDistinguisher": 0}},
        },
        "CUSTC-PROD": {
            "localAs": 65010,
            "afiSafiConfig": {
                "v4u": {
                    "routeDistinguisher": 279215823914163,
                    "routeTargetImports": {"mplsVpnV4u": ["65010:1203"]},
                    "routeTargetExports": {"mplsVpnV4u": ["65010:1203"]},
                },
                "v4m": {},
                "evpn": {"routeDistinguisher": 279215823914163},
            },
        },
    }
})

# 'show vrf detail' from a Cisco IOL. clab-mgmt is the real lab VRF (no targets
# configured); CUSTC carries targets across two address families.
IOS_SHOW_VRF_DETAIL = """VRF CUSTC (VRF Id = 2); default RD 65000:1203; default VPNID <not set>
  Description: customer c
  New CLI format, supports multiple address-families
  Flags: 0x180C
  Interfaces:
    Et0/1
Address family ipv4 unicast (Table ID = 0x2):
  Flags: 0x0
  Export VPN route-target communities
    RT:65000:1203                RT:65000:9999
  Import VPN route-target communities
    RT:65000:1203
  No import route-map
  No global export route-map
  No export route-map
  VRF label distribution protocol: not configured
  VRF label allocation mode: per-prefix
Address family ipv6 unicast (Table ID = 0x1E000002):
  Flags: 0x0
  No Export VPN route-target communities
  No Import VPN route-target communities
  No import route-map
Address family ipv4 multicast not active
VRF clab-mgmt (VRF Id = 1); default RD <not set>; default VPNID <not set>
  Description: clab-mgmt
  Interfaces:
    Et0/0
Address family ipv4 unicast (Table ID = 0x1):
  Flags: 0x0
  No Export VPN route-target communities
  No Import VPN route-target communities
  No import route-map
"""

# IOS-XR spells the header and the body differently ('VRF x; RD y' rather than
# 'VRF x (VRF Id = n)'), and puts each target on its own line.
IOSXR_SHOW_VRF_ALL_DETAIL = """VRF CUSTC; RD 65000:100; VPN ID not set
VRF mode: Regular
Description not set
Interfaces:
  GigabitEthernet0/0/0/1
Address family IPV4 Unicast
  Import VPN route-target communities
    RT:65000:100
    RT:65000:200
  Export VPN route-target communities
    RT:65000:100
  No import route policy
  No export route policy
"""


class TestParseRouteTargetsEos:
    def test_targets_lifted_out_of_nested_afi_config(self):
        assert _parse_route_targets_eos(EOS_BGP_JSON) == {
            "CUSTC-PROD": {"import": ["65010:1203"], "export": ["65010:1203"]}
        }

    def test_vrf_without_targets_is_omitted(self):
        # 'default' has an afiSafiConfig but no targets; it must not appear at
        # all, so a VRF is never stamped with an empty set it did not report.
        assert "default" not in _parse_route_targets_eos(EOS_BGP_JSON)

    def test_targets_unioned_across_address_families(self):
        # A NetBox VRF carries one import/export set, not one per family, so
        # mpls-vpn and evpn targets collapse together.
        payload = json.dumps({"vrfs": {"V": {"afiSafiConfig": {
            "v4u": {"routeTargetImports": {"mplsVpnV4u": ["1:1"]}},
            "evpn": {"routeTargetImports": {"evpn": ["2:2", "1:1"]}},
        }}}})
        assert _parse_route_targets_eos(payload) == {
            "V": {"import": ["1:1", "2:2"], "export": []}
        }

    def test_no_bgp_configured_yields_nothing(self):
        assert _parse_route_targets_eos(json.dumps({"vrfs": {}})) == {}

    def test_non_json_output_raises_for_the_caller_to_swallow(self):
        with pytest.raises(ValueError):
            _parse_route_targets_eos("% Invalid input")


class TestParseRouteTargetsCiscoText:
    def test_ios_targets_per_direction(self):
        parsed = _parse_route_targets_cisco_text(IOS_SHOW_VRF_DETAIL)
        assert parsed["CUSTC"] == {
            "import": ["65000:1203"],
            "export": ["65000:1203", "65000:9999"],
        }

    def test_negated_sections_claim_no_targets(self):
        # 'No Export VPN route-target communities' must not leave the previous
        # direction open, or the next block's targets land on the wrong one.
        parsed = _parse_route_targets_cisco_text(IOS_SHOW_VRF_DETAIL)
        assert "clab-mgmt" not in parsed

    def test_iosxr_header_and_one_target_per_line(self):
        assert _parse_route_targets_cisco_text(IOSXR_SHOW_VRF_ALL_DETAIL) == {
            "CUSTC": {"import": ["65000:100", "65000:200"], "export": ["65000:100"]}
        }

    def test_iosxr_body_line_is_not_read_as_a_vrf(self):
        # 'VRF mode: Regular' starts at column 0 like a real header does; a
        # looser match invents a VRF named "mode:" and hangs XR's targets on it.
        assert "mode:" not in _parse_route_targets_cisco_text(IOSXR_SHOW_VRF_ALL_DETAIL)

    def test_empty_output(self):
        assert _parse_route_targets_cisco_text("") == {}


class TestParseVrfMembershipCiscoText:
    """
    The 'Interfaces:' block of the same 'show vrf detail' output is the only VRF
    membership source on IOS-XR (its NAPALM driver reports no network_instances),
    so an interface reaches its real VRF instead of the default one.
    """

    def test_ios_membership_and_rd_per_vrf(self):
        intf_map, rd_map = _parse_vrf_membership_cisco_text(IOS_SHOW_VRF_DETAIL)
        assert intf_map == {"CUSTC": ["Et0/1"], "clab-mgmt": ["Et0/0"]}
        assert rd_map["CUSTC"] == "65000:1203"

    def test_ios_not_set_rd_is_dropped(self):
        # 'default RD <not set>' carries no ':' -- it must not stamp a fake RD.
        _, rd_map = _parse_vrf_membership_cisco_text(IOS_SHOW_VRF_DETAIL)
        assert "clab-mgmt" not in rd_map

    def test_iosxr_one_interface_per_line(self):
        intf_map, rd_map = _parse_vrf_membership_cisco_text(IOSXR_SHOW_VRF_ALL_DETAIL)
        assert intf_map == {"CUSTC": ["GigabitEthernet0/0/0/1"]}
        assert rd_map == {"CUSTC": "65000:100"}

    def test_address_family_line_ends_the_block(self):
        # 'Address family ...' must close the Interfaces: list, or its words get
        # mistaken for interface names.
        intf_map, _ = _parse_vrf_membership_cisco_text(IOSXR_SHOW_VRF_ALL_DETAIL)
        assert intf_map["CUSTC"] == ["GigabitEthernet0/0/0/1"]

    def test_empty_output(self):
        assert _parse_vrf_membership_cisco_text("") == ({}, {})


class TestBuildVrfMapMembershipFallback:
    """
    _build_vrf_map keeps NAPALM authoritative but falls back to the
    'show vrf detail' membership when network_instances is empty.
    """

    def test_membership_fallback_when_napalm_silent(self):
        # Empty network_instances (IOS-XR ASR-9906) -> the VRF and its interface
        # come entirely from vrf_interfaces/vrf_rds/route_targets.
        interface_vrf, vrfs = _build_vrf_map(
            network_instances={},
            route_targets={"M_20577": {"import": ["6000:4000"], "export": []}},
            vrf_interfaces={"M_20577": ["GigabitEthernet0/0/1/22.99"]},
            vrf_rds={"M_20577": "20577:6900"},
        )
        assert interface_vrf == {"GigabitEthernet0/0/1/22.99": "M_20577"}
        assert len(vrfs) == 1
        assert vrfs[0].name == "M_20577"
        assert vrfs[0].rd == "20577:6900"
        assert vrfs[0].import_targets == ["6000:4000"]

    def test_napalm_membership_wins_over_fallback(self):
        # When NAPALM already placed the interface, the fallback must not add a
        # duplicate VRF or re-point the interface.
        network_instances = {
            "M_20577": {
                "type": "L3VRF",
                "state": {"route_distinguisher": "20577:6900"},
                "interfaces": {"interface": {"GigabitEthernet0/0/1/22.99": {}}},
            }
        }
        interface_vrf, vrfs = _build_vrf_map(
            network_instances=network_instances,
            vrf_interfaces={"M_20577": ["GigabitEthernet0/0/1/22.99"]},
            vrf_rds={"M_20577": "20577:6900"},
        )
        assert interface_vrf == {"GigabitEthernet0/0/1/22.99": "M_20577"}
        assert [v.name for v in vrfs] == ["M_20577"]

    def test_default_vrf_name_from_fallback_is_ignored(self):
        interface_vrf, vrfs = _build_vrf_map(
            network_instances={},
            vrf_interfaces={"default": ["GigabitEthernet0/0/0/0"]},
        )
        assert interface_vrf == {}
        assert vrfs == []



@pytest.fixture
def nb():
    with patch("pynetbox.api"):
        client = NetboxClient("http://netbox.test", "token")
    client.nb = MagicMock()
    return client


def fake_vrf(import_targets, export_targets):
    vrf = MagicMock()
    vrf.name = "CUSTC-PROD"
    vrf.import_targets = import_targets
    vrf.export_targets = export_targets
    return vrf


class TestSyncVrfRouteTargets:
    """
    The shared object cache hands one VRF Record to every host in a run, and
    pynetbox rewrites that Record from the request body after an update() --
    so the second host reads back bare ids where the first read Records.
    """

    def test_reads_nested_records(self):
        vrf = fake_vrf([MagicMock(id=1)], [MagicMock(id=1)])
        assert NetboxClient._target_ids(vrf.import_targets) == {1}

    def test_reads_bare_ids_left_by_a_previous_update(self):
        assert NetboxClient._target_ids([1, 2]) == {1, 2}

    def test_reads_plain_dicts(self):
        assert NetboxClient._target_ids([{"id": 1, "name": "65010:1203"}]) == {1}

    def test_empty_and_none(self):
        assert NetboxClient._target_ids([]) == set()
        assert NetboxClient._target_ids(None) == set()

    def test_no_write_when_targets_already_match(self, nb):
        vrf = fake_vrf([MagicMock(id=7)], [MagicMock(id=7)])
        assert nb._sync_vrf_route_targets(vrf, [7], [7]) is False
        vrf.update.assert_not_called()

    def test_second_host_in_the_run_sees_a_match_not_a_crash(self, nb):
        # Exactly the state the first host's update() leaves behind. Before the
        # id-shape fix this raised AttributeError, which surfaced as
        # "VRF ... could not be synced: 'int' object has no attribute 'id'"
        # and dropped the VRF off that host's interfaces.
        vrf = fake_vrf([7], [7])
        assert nb._sync_vrf_route_targets(vrf, [7], [7]) is False
        vrf.update.assert_not_called()

    def test_writes_when_targets_differ(self, nb):
        vrf = fake_vrf([], [])
        assert nb._sync_vrf_route_targets(vrf, [7], [8]) is True
        vrf.update.assert_called_once_with({"import_targets": [7], "export_targets": [8]})

    def test_discovery_is_authoritative_and_drops_removed_targets(self, nb):
        vrf = fake_vrf([MagicMock(id=7), MagicMock(id=9)], [MagicMock(id=7)])
        assert nb._sync_vrf_route_targets(vrf, [7], [7]) is True
        vrf.update.assert_called_once_with({"import_targets": [7]})

    def test_netbox_failure_is_reported_not_raised(self, nb):
        vrf = fake_vrf([], [])
        vrf.update.side_effect = Exception("409")
        assert nb._sync_vrf_route_targets(vrf, [7], [7]) is False
