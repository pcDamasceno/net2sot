"""
Tests for load_inventory_from_netbox: sourcing the Nornir inventory from NetBox
for a re-discovery run.

The important behaviours are that each NetBox device lands in the right platform
group (so it inherits connection options + collector), and that devices we cannot
act on -- no primary IP, or a platform with no matching inventory group -- are
skipped rather than producing a broken host.
"""

from unittest.mock import MagicMock

from nornir.core.inventory import Defaults, Group, Groups, Hosts

from net2sot.discovery import load_inventory_from_netbox


def make_nr(group_names=("ios", "eos", "srlinux")):
    """A minimal stand-in for a Nornir object: only .inventory is touched."""
    groups = Groups()
    for name in group_names:
        groups[name] = Group(name=name, platform=name)

    inventory = MagicMock()
    inventory.groups = groups
    inventory.defaults = Defaults(username="admin", password="admin")
    inventory.hosts = Hosts()

    nr = MagicMock()
    nr.inventory = inventory
    return nr


def fake_nb_device(name, platform_slug, primary_ip):
    device = MagicMock()
    device.name = name
    device.id = 999
    device.platform = MagicMock(slug=platform_slug) if platform_slug else None
    device.primary_ip = MagicMock(address=primary_ip) if primary_ip else None
    return device


def make_nb(devices):
    nb = MagicMock()
    nb.get_devices.return_value = devices
    return nb


def test_maps_devices_to_matching_platform_groups():
    nr = make_nr()
    nb = make_nb([
        fake_nb_device("PE-EMEA-01", "eos", "172.20.20.11/24"),
        fake_nb_device("CORE-RR-01", "ios", "172.20.20.13/24"),
    ])

    load_inventory_from_netbox(nr, nb, {"site": "lab"}, {})

    hosts = nr.inventory.hosts
    assert set(hosts) == {"PE-EMEA-01", "CORE-RR-01"}
    # Reached at the NetBox primary IP, with the /prefix stripped.
    assert hosts["PE-EMEA-01"].hostname == "172.20.20.11"
    # Slotted into the matching group so it inherits its connection options.
    assert [g.name for g in hosts["PE-EMEA-01"].groups] == ["eos"]
    assert [g.name for g in hosts["CORE-RR-01"].groups] == ["ios"]


def test_platform_inherited_from_group():
    nr = make_nr()
    nb = make_nb([fake_nb_device("PE-EMEA-01", "eos", "172.20.20.11/24")])

    load_inventory_from_netbox(nr, nb, {}, {})

    # No platform= is set on the Host; it must resolve through the group.
    assert nr.inventory.hosts["PE-EMEA-01"].platform == "eos"


def test_skips_device_without_primary_ip():
    nr = make_nr()
    nb = make_nb([
        fake_nb_device("NO-IP", "eos", None),
        fake_nb_device("PE-EMEA-01", "eos", "172.20.20.11/24"),
    ])

    load_inventory_from_netbox(nr, nb, {}, {})

    assert set(nr.inventory.hosts) == {"PE-EMEA-01"}


def test_skips_platform_with_no_inventory_group():
    nr = make_nr()
    nb = make_nb([fake_nb_device("JUNI-01", "junos", "10.0.0.1/24")])

    load_inventory_from_netbox(nr, nb, {}, {})

    assert len(nr.inventory.hosts) == 0


def test_platform_map_override_bridges_slug_to_group():
    nr = make_nr()
    nb = make_nb([fake_nb_device("X", "cisco-ios-xe", "10.0.0.1/24")])

    load_inventory_from_netbox(
        nr, nb, {}, {"netbox_platform_map": {"cisco-ios-xe": "ios"}}
    )

    assert "X" in nr.inventory.hosts
    assert [g.name for g in nr.inventory.hosts["X"].groups] == ["ios"]


def test_static_hosts_are_replaced():
    nr = make_nr()
    # Pretend the SimpleInventory already loaded a static host.
    nr.inventory.hosts["stale-static"] = MagicMock()
    nb = make_nb([fake_nb_device("PE-EMEA-01", "eos", "172.20.20.11/24")])

    load_inventory_from_netbox(nr, nb, {}, {})

    assert "stale-static" not in nr.inventory.hosts
    assert set(nr.inventory.hosts) == {"PE-EMEA-01"}


def test_filters_forwarded_to_netbox():
    nr = make_nr()
    nb = make_nb([])

    load_inventory_from_netbox(
        nr, nb, {"site": "lab", "platform": ["eos", "ios"], "location": None}, {}
    )

    nb.get_devices.assert_called_once_with(
        site="lab", platform=["eos", "ios"], location=None
    )
