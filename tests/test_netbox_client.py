"""
Tests for NetboxClient write behaviour against a stubbed pynetbox.

Focused on what a re-run does to data already in NetBox, which is the part that
is expensive to get wrong and impossible to notice from a green run.
"""

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

from net2sot.netbox_client import NetboxClient


@pytest.fixture
def nb():
    with patch("pynetbox.api"):
        client = NetboxClient("http://netbox.test", "token")
    client.nb = MagicMock()
    return client


def fake_device(name, device_type_id, device_type_model):
    """A pynetbox-shaped device Record: .device_type is a nested Record."""
    device = MagicMock()
    device.name = name
    device.device_type = MagicMock(id=device_type_id, model=device_type_model)
    return device


def fake_ip(address, assigned_object_id, ip_id=1):
    """A pynetbox-shaped IP address Record."""
    ip = MagicMock()
    ip.address = address
    ip.assigned_object_id = assigned_object_id
    ip.id = ip_id
    return ip


def fake_iface(name, description, vrf=None):
    """A pynetbox-shaped interface Record."""
    iface = MagicMock()
    iface.name = name
    iface.description = description
    # Explicit: an interface in the global table carries vrf=None, and a bare
    # MagicMock attribute would be truthy -- which reads as "this interface is
    # in some VRF" and provokes a VRF patch no real interface would get.
    iface.vrf = vrf
    return iface


class TestEnsureInterfaceSpeed:
    def test_converts_mbps_to_kbps(self, nb):
        nb.nb.dcim.interfaces.get.return_value = None
        nb.ensure_interface(1, {"name": "Gi0/0", "speed": 1000.0})
        assert nb.nb.dcim.interfaces.create.call_args.kwargs["speed"] == 1_000_000

    def test_omits_speed_when_unknown(self, nb):
        nb.nb.dcim.interfaces.get.return_value = None
        nb.ensure_interface(1, {"name": "Gi0/0", "speed": 0})
        assert "speed" not in nb.nb.dcim.interfaces.create.call_args.kwargs

    def test_omits_negative_speed(self, nb):
        # NX-OS reports -1 on nve1 (the VXLAN tunnel), where speed is
        # meaningless. NetBox rejects a negative speed and used to fail the whole
        # interface over it.
        nb.nb.dcim.interfaces.get.return_value = None
        nb.ensure_interface(1, {"name": "nve1", "speed": -1.0})
        assert "speed" not in nb.nb.dcim.interfaces.create.call_args.kwargs


class TestEnsureDeviceRetype:
    def test_retypes_existing_device_when_type_differs(self, nb):
        existing = fake_device("R1", device_type_id=7, device_type_model="cisco-generic")
        nb.nb.dcim.devices.filter.return_value = [existing]

        nb.ensure_device("R1", 1, device_type_id=42, device_role_id=1,
                         platform_id=1, update_existing=True)

        existing.update.assert_called_once_with({"device_type": 42})

    def test_only_device_type_and_comments_are_written(self, nb):
        # device_type and comments are discovery-owned and get refreshed. Serial,
        # platform, role and site may have been curated by hand; a re-run must
        # not clobber them, so they never appear in the update payload.
        existing = fake_device("R1", device_type_id=7, device_type_model="cisco-generic")
        existing.comments = "stale"
        nb.nb.dcim.devices.filter.return_value = [existing]

        nb.ensure_device("R1", site_id=1, device_type_id=42, device_role_id=9,
                         platform_id=3, serial="NEW-SERIAL",
                         comments="regenerated", update_existing=True)

        assert existing.update.call_args.args[0] == {
            "device_type": 42, "comments": "regenerated"
        }
        nb.nb.dcim.devices.create.assert_not_called()

    def test_no_write_when_type_already_correct(self, nb):
        existing = fake_device("R1", device_type_id=42, device_type_model="ISR4331/K9")
        nb.nb.dcim.devices.filter.return_value = [existing]

        nb.ensure_device("R1", 1, device_type_id=42, device_role_id=1,
                         platform_id=1, update_existing=True)

        existing.update.assert_not_called()

    def test_does_not_retype_when_disabled(self, nb):
        existing = fake_device("R1", device_type_id=7, device_type_model="cisco-generic")
        nb.nb.dcim.devices.filter.return_value = [existing]

        nb.ensure_device("R1", 1, device_type_id=42, device_role_id=1,
                         platform_id=1, update_existing=False)

        existing.update.assert_not_called()

    def test_defaults_to_not_retyping(self, nb):
        # The LLDP placeholder path calls ensure_device with the *local* device's
        # type. If that ever lands on a real device, defaulting to off is what
        # stops it being re-typed to something actively wrong.
        existing = fake_device("PEER-01", device_type_id=7, device_type_model="ceos")
        nb.nb.dcim.devices.filter.return_value = [existing]

        nb.ensure_device("PEER-01", 1, device_type_id=42, device_role_id=1, platform_id=1)

        existing.update.assert_not_called()

    def test_retype_failure_does_not_raise(self, nb):
        existing = fake_device("R1", device_type_id=7, device_type_model="cisco-generic")
        existing.update.side_effect = Exception("NetBox said no")
        nb.nb.dcim.devices.filter.return_value = [existing]

        # A device that cannot be re-typed must not sink the whole host.
        device = nb.ensure_device("R1", 1, device_type_id=42, device_role_id=1,
                                  platform_id=1, update_existing=True)
        assert device is existing

    def test_newly_created_device_is_not_retyped(self, nb):
        nb.nb.dcim.devices.filter.return_value = []
        created = fake_device("R1", device_type_id=42, device_type_model="ISR4331/K9")
        nb.nb.dcim.devices.create.return_value = created

        nb.ensure_device("R1", 1, device_type_id=42, device_role_id=1,
                         platform_id=1, update_existing=True)

        created.update.assert_not_called()


class TestEnsureDeviceComments:
    """The auto-discovered comments block goes stale (uptime, OS, timestamp),
    so a re-run rewrites it — unless the type already matches and nothing else
    changed, or --no-update-existing is in force."""

    def test_comments_refreshed_when_changed(self, nb):
        existing = fake_device("R1", device_type_id=42, device_type_model="ISR4331/K9")
        existing.comments = "old block"
        nb.nb.dcim.devices.filter.return_value = [existing]

        nb.ensure_device("R1", 1, device_type_id=42, device_role_id=1,
                         platform_id=1, comments="new block", update_existing=True)

        existing.update.assert_called_once_with({"comments": "new block"})

    def test_comments_not_written_when_identical(self, nb):
        existing = fake_device("R1", device_type_id=42, device_type_model="ISR4331/K9")
        existing.comments = "same block"
        nb.nb.dcim.devices.filter.return_value = [existing]

        nb.ensure_device("R1", 1, device_type_id=42, device_role_id=1,
                         platform_id=1, comments="same block", update_existing=True)

        existing.update.assert_not_called()

    def test_comments_not_written_when_disabled(self, nb):
        existing = fake_device("R1", device_type_id=42, device_type_model="ISR4331/K9")
        existing.comments = "old block"
        nb.nb.dcim.devices.filter.return_value = [existing]

        nb.ensure_device("R1", 1, device_type_id=42, device_role_id=1,
                         platform_id=1, comments="new block", update_existing=False)

        existing.update.assert_not_called()


class TestGetRenderedConfig:
    def test_returns_rendered_content(self, nb):
        nb.nb.base_url = "https://netbox.test/api"
        response = nb.nb.http_session.post.return_value
        response.json.return_value = {"content": "hostname R1\n"}

        content = nb.get_rendered_config(42)

        assert content == "hostname R1\n"
        nb.nb.http_session.post.assert_called_once_with(
            "https://netbox.test/api/dcim/devices/42/render-config/",
            headers={"Authorization": "Token token", "Accept": "application/json"},
        )
        response.raise_for_status.assert_called_once_with()

    def test_rejects_empty_rendered_content(self, nb):
        nb.nb.base_url = "https://netbox.test/api"
        nb.nb.http_session.post.return_value.json.return_value = {"content": "  "}

        with pytest.raises(RuntimeError, match="no rendered configuration"):
            nb.get_rendered_config(42)


class TestEnsureInterfaceDescription:
    """Port descriptions change over time and the device is authoritative, so a
    re-run syncs an existing interface's description (including clearing one the
    device no longer reports) — but only when update_existing is on."""

    def test_updates_description_when_changed(self, nb):
        iface = fake_iface("Gi0/0", "old uplink")
        nb.nb.dcim.interfaces.get.return_value = iface

        nb.ensure_interface(1, {"name": "Gi0/0", "description": "new uplink"},
                            update_existing=True)

        iface.update.assert_called_once_with({"description": "new uplink"})
        nb.nb.dcim.interfaces.create.assert_not_called()

    def test_clears_description_when_device_reports_none(self, nb):
        iface = fake_iface("Gi0/0", "stale uplink")
        nb.nb.dcim.interfaces.get.return_value = iface

        nb.ensure_interface(1, {"name": "Gi0/0"}, update_existing=True)

        iface.update.assert_called_once_with({"description": ""})

    def test_no_write_when_description_matches(self, nb):
        iface = fake_iface("Gi0/0", "uplink")
        nb.nb.dcim.interfaces.get.return_value = iface

        nb.ensure_interface(1, {"name": "Gi0/0", "description": "uplink"},
                            update_existing=True)

        iface.update.assert_not_called()

    def test_existing_description_untouched_when_disabled(self, nb):
        iface = fake_iface("Gi0/0", "uplink")
        nb.nb.dcim.interfaces.get.return_value = iface

        # update_existing defaults off (the LLDP remote-interface path relies on
        # this), so an existing interface is returned as-is.
        nb.ensure_interface(1, {"name": "Gi0/0", "description": "changed"})

        iface.update.assert_not_called()


class TestPruneDeviceIps:
    """A changed IP must not leave its predecessor behind: every IP still on a
    synced interface in NetBox that this run did not rediscover is deleted. Done
    with one device-wide query rather than one per interface."""

    def test_deletes_ip_not_discovered(self, nb):
        stale = fake_ip("10.0.0.9/24", assigned_object_id=5, ip_id=99)
        nb.nb.ipam.ip_addresses.filter.return_value = [stale]

        removed = nb.prune_device_ips(1, {5: {"10.0.0.1/24"}})

        assert removed == 1
        stale.delete.assert_called_once()

    def test_queries_once_for_the_whole_device(self, nb):
        nb.nb.ipam.ip_addresses.filter.return_value = []

        nb.prune_device_ips(1, {5: set(), 6: set(), 7: set()})

        nb.nb.ipam.ip_addresses.filter.assert_called_once_with(device_id=1)

    def test_keeps_discovered_ip(self, nb):
        keep_ip = fake_ip("10.0.0.1/24", assigned_object_id=5)
        nb.nb.ipam.ip_addresses.filter.return_value = [keep_ip]

        removed = nb.prune_device_ips(1, {5: {"10.0.0.1/24"}})

        assert removed == 0
        keep_ip.delete.assert_not_called()

    def test_ignores_ip_on_an_interface_not_synced_this_run(self, nb):
        # Interface 6 was not discovered (disabled, filtered out, or belongs to
        # another device the fuzzy filter dragged in) — leave it alone entirely.
        other = fake_ip("10.0.0.9/24", assigned_object_id=6)
        nb.nb.ipam.ip_addresses.filter.return_value = [other]

        removed = nb.prune_device_ips(1, {5: set()})

        assert removed == 0
        other.delete.assert_not_called()

    def test_empty_keep_set_removes_every_ip_on_that_interface(self, nb):
        # The device no longer reports any IP here, which is itself the truth.
        stale = fake_ip("10.0.0.9/24", assigned_object_id=5, ip_id=99)
        nb.nb.ipam.ip_addresses.filter.return_value = [stale]

        removed = nb.prune_device_ips(1, {5: set()})

        assert removed == 1
        stale.delete.assert_called_once()

    def test_ipv6_compression_difference_is_kept(self, nb):
        # NetBox stores compressed; the device reported it expanded. Same address,
        # so it must not be deleted and recreated on every run.
        keep_ip = fake_ip("2001:db8::1/64", assigned_object_id=5)
        nb.nb.ipam.ip_addresses.filter.return_value = [keep_ip]

        removed = nb.prune_device_ips(1, {5: {"2001:db8:0:0::1/64"}})

        assert removed == 0
        keep_ip.delete.assert_not_called()

    def test_clears_primary_then_deletes_when_protected(self, nb):
        stale = fake_ip("10.0.0.9/24", assigned_object_id=5, ip_id=99)
        # NetBox refuses the first delete (still the device's primary), then
        # accepts it once the pointer is cleared.
        stale.delete.side_effect = [Exception("primary IP"), None]
        nb.nb.ipam.ip_addresses.filter.return_value = [stale]

        device = MagicMock()
        device.name = "R1"
        device.primary_ip4 = MagicMock(id=99)
        device.primary_ip6 = MagicMock(id=None)
        nb.nb.dcim.devices.get.return_value = device

        removed = nb.prune_device_ips(1, {5: set()})

        assert removed == 1
        device.update.assert_called_once_with({"primary_ip4": None})
        assert stale.delete.call_count == 2


class TestEnsureInterfacesBulk:
    """The hot path: a device's whole interface list in a couple of requests
    instead of a GET and a POST per port, which is what saturated NetBox into
    answering 503."""

    def test_creates_all_missing_in_one_request(self, nb):
        nb.nb.dcim.interfaces.filter.return_value = []
        nb.nb.dcim.interfaces.create.return_value = [
            fake_iface("Gi0/0", ""), fake_iface("Gi0/1", ""),
        ]

        synced = nb.ensure_interfaces(
            1, [{"name": "Gi0/0"}, {"name": "Gi0/1"}]
        )

        assert set(synced) == {"Gi0/0", "Gi0/1"}
        nb.nb.dcim.interfaces.create.assert_called_once()
        payloads = nb.nb.dcim.interfaces.create.call_args.args[0]
        assert [p["name"] for p in payloads] == ["Gi0/0", "Gi0/1"]
        # One list read for the device, never a get() per interface.
        nb.nb.dcim.interfaces.filter.assert_called_once_with(device_id=1)
        nb.nb.dcim.interfaces.get.assert_not_called()

    def test_existing_interfaces_are_not_recreated(self, nb):
        nb.nb.dcim.interfaces.filter.return_value = [fake_iface("Gi0/0", "")]

        synced = nb.ensure_interfaces(1, [{"name": "Gi0/0"}])

        assert set(synced) == {"Gi0/0"}
        nb.nb.dcim.interfaces.create.assert_not_called()

    def test_speed_is_converted_to_kbps(self, nb):
        nb.nb.dcim.interfaces.filter.return_value = []
        nb.nb.dcim.interfaces.create.return_value = [fake_iface("Gi0/0", "")]

        nb.ensure_interfaces(1, [{"name": "Gi0/0", "speed": 1000.0}])

        assert nb.nb.dcim.interfaces.create.call_args.args[0][0]["speed"] == 1_000_000

    def test_rejected_batch_falls_back_to_one_request_each(self, nb):
        # NetBox applies a bulk create atomically, so one bad payload must not
        # take the rest of the device's interfaces down with it.
        nb.nb.dcim.interfaces.filter.return_value = []
        nb.nb.dcim.interfaces.create.side_effect = [
            Exception("400 bad mac_address"),
            fake_iface("Gi0/0", ""),
            Exception("400 bad mac_address"),
        ]

        synced = nb.ensure_interfaces(
            1, [{"name": "Gi0/0"}, {"name": "Gi0/1"}]
        )

        assert set(synced) == {"Gi0/0"}  # Gi0/1 reported missing, not silently ok

    def test_failed_interface_is_absent_from_the_result(self, nb):
        nb.nb.dcim.interfaces.filter.return_value = []
        nb.nb.dcim.interfaces.create.side_effect = Exception("503 Service Unavailable")

        synced = nb.ensure_interfaces(1, [{"name": "Gi0/0"}])

        assert synced == {}

    def test_batches_are_chunked(self, nb):
        from net2sot.netbox_client import _BULK_CHUNK

        wanted = [{"name": f"Gi0/{n}"} for n in range(_BULK_CHUNK + 5)]
        nb.nb.dcim.interfaces.filter.return_value = []
        nb.nb.dcim.interfaces.create.side_effect = lambda payloads: [
            fake_iface(p["name"], "") for p in payloads
        ]

        synced = nb.ensure_interfaces(1, wanted)

        assert len(synced) == _BULK_CHUNK + 5
        assert nb.nb.dcim.interfaces.create.call_count == 2

    def test_descriptions_synced_in_one_patch(self, nb):
        stale = fake_iface("Gi0/0", "old uplink")
        stale.id = 7
        unchanged = fake_iface("Gi0/1", "core")
        unchanged.id = 8
        nb.nb.dcim.interfaces.filter.return_value = [stale, unchanged]

        nb.ensure_interfaces(
            1,
            [
                {"name": "Gi0/0", "description": "New Uplink"},
                {"name": "Gi0/1", "description": "core"},
            ],
            update_existing=True,
        )

        # Only the one that actually changed, lower-cased, in a single request.
        nb.nb.dcim.interfaces.update.assert_called_once_with(
            [{"id": 7, "description": "new uplink"}]
        )

    def test_descriptions_untouched_when_disabled(self, nb):
        iface = fake_iface("Gi0/0", "uplink")
        iface.id = 7
        nb.nb.dcim.interfaces.filter.return_value = [iface]

        nb.ensure_interfaces(1, [{"name": "Gi0/0", "description": "changed"}])

        nb.nb.dcim.interfaces.update.assert_not_called()


class TestSharedObjectResolution:
    """Every host in a run wants the same site, role, platform and VRF at the
    same moment. NetBox does not enforce uniqueness on all of them, so workers
    that read 'missing' together used to each create one — leaving duplicates
    that every later read then failed on."""

    def test_created_once_then_served_from_cache(self, nb):
        nb.nb.ipam.vrfs.filter.return_value = []
        nb.nb.ipam.vrfs.create.return_value = MagicMock(id=42)

        first = nb.ensure_vrf("default_lab")
        second = nb.ensure_vrf("default_lab")

        assert first is second
        nb.nb.ipam.vrfs.create.assert_called_once()
        # The second caller does not even re-read it.
        nb.nb.ipam.vrfs.filter.assert_called_once()

    def test_duplicates_resolve_to_the_lowest_id(self, nb):
        # The state an earlier race left in NetBox. get() raises outright on
        # this; picking the oldest keeps every worker and every later run on the
        # same object instead of making the duplicate permanently fatal.
        nb.nb.ipam.vrfs.filter.return_value = [MagicMock(id=44), MagicMock(id=42)]

        vrf = nb.ensure_vrf("default_lab")

        assert vrf.id == 42
        nb.nb.ipam.vrfs.create.assert_not_called()

    def test_lookup_never_uses_get(self, nb):
        nb.nb.dcim.platforms.filter.return_value = [MagicMock(id=1)]

        nb.ensure_platform("NXOS")

        nb.nb.dcim.platforms.get.assert_not_called()

    def test_concurrent_callers_create_only_one(self, nb):
        # The real failure mode: ten Nornir workers arriving together. The stub
        # only reports the VRF as existing once something has created it.
        created = []

        def fake_filter(**_):
            return list(created)

        def fake_create(**_):
            obj = MagicMock(id=42 + len(created))
            created.append(obj)
            return obj

        nb.nb.ipam.vrfs.filter.side_effect = fake_filter
        nb.nb.ipam.vrfs.create.side_effect = fake_create

        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(lambda _: nb.ensure_vrf("default_lab"), range(10)))

        assert len(created) == 1
        assert all(r is results[0] for r in results)

    def test_existing_site_skips_the_tenant_lookup(self, nb):
        nb.nb.dcim.sites.filter.return_value = [MagicMock(id=157)]

        site = nb.ensure_site("CISCO_ND", tenant="CISCO_SANDBOX")

        assert site.id == 157
        nb.nb.tenancy.tenants.create.assert_not_called()

    def test_no_tenant_configured_creates_no_tenant(self, nb):
        """
        NetBox tenancy is optional, and an unset `tenant` used to fall back to a
        hard-coded name. A deployment that doesn't use tenancy should not find a
        tenant it never asked for -- least of all one called "".
        """
        nb.nb.dcim.sites.filter.return_value = []
        nb.nb.tenancy.tenants.filter.return_value = []

        for tenant in (None, ""):
            nb.ensure_site("lab", tenant=tenant)

        nb.nb.tenancy.tenants.create.assert_not_called()
        for call in nb.nb.dcim.sites.create.call_args_list:
            assert call.kwargs["tenant"] is None

    def test_configured_tenant_is_still_created_and_attached(self, nb):
        nb.nb.dcim.sites.filter.return_value = []
        nb.nb.tenancy.tenants.filter.return_value = []
        nb.nb.tenancy.tenants.create.return_value = MagicMock(id=9)

        nb.ensure_site("lab", tenant="NET_OPS")

        assert nb.nb.tenancy.tenants.create.call_args.kwargs["name"] == "NET_OPS"
        assert nb.nb.dcim.sites.create.call_args.kwargs["tenant"] == 9


class TestRetrySession:
    """A 503 from an overloaded NetBox is transient, but the callers here turn an
    exception into a skipped object — i.e. silently missing data. The session has
    to absorb it."""

    def test_retries_are_mounted_for_both_schemes(self):
        with patch("pynetbox.api"):
            client = NetboxClient("https://netbox.test", "token")

        for scheme in ("https://", "http://"):
            retry = client.nb.http_session.get_adapter(scheme).max_retries
            assert retry.total == 5
            assert set(retry.status_forcelist) == {429, 502, 503, 504}
            # None = every method, so POST/PATCH/DELETE are retried too; the
            # default would silently skip exactly the writes that matter.
            assert retry.allowed_methods is None
            assert retry.respect_retry_after_header is True

    def test_pool_is_sized_to_the_worker_count(self):
        with patch("pynetbox.api"):
            client = NetboxClient("https://netbox.test", "token", pool_size=10)

        adapter = client.nb.http_session.get_adapter("https://")
        assert adapter._pool_maxsize == 10

    def test_cert_validation_still_honoured(self):
        # Separate patch contexts: within one, pynetbox.api() hands back the same
        # mock every call, so both clients would share a single http_session.
        with patch("pynetbox.api"):
            verified = NetboxClient("https://netbox.test", "t", validate_certs=True)
        with patch("pynetbox.api"):
            skipped = NetboxClient("https://netbox.test", "t", validate_certs=False)

        assert verified.nb.http_session.verify is True
        assert skipped.nb.http_session.verify is False


class TestGetDevices:
    """Server-side device listing for --from-netbox re-discovery."""

    def test_passes_only_non_empty_filters(self, nb):
        nb.get_devices(site="lab", platform=None, name__ie=["R1"], location="")

        nb.nb.dcim.devices.filter.assert_called_once_with(site="lab", name__ie=["R1"])
        nb.nb.dcim.devices.all.assert_not_called()

    def test_no_filters_returns_all(self, nb):
        nb.get_devices(site=None, platform=None)

        nb.nb.dcim.devices.all.assert_called_once_with()
        nb.nb.dcim.devices.filter.assert_not_called()
