"""
The plugin system: registration, resolution, and a whole discovery run driven
by a collector and a sink that this project does not ship.

The last class is the one that matters. If it passes, somebody can add a vendor
from their own repository without touching this one.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from net2sot.discovery import discover_device, resolve_collector_name
from net2sot.plugins import (
    CollectContext,
    Collector,
    PluginNotFound,
    PluginRegistry,
    Sink,
    SyncContext,
    collector_for_platform,
    collectors,
    sinks,
)
from net2sot.schemas import (
    CollectedFacts,
    DeviceFacts,
    InterfaceAddressFacts,
    InterfaceFacts,
    SyncReport,
)

# ── A plugin pair from "another repository" ──────────────────────────


class MikroTikCollector(Collector):
    """Stands in for a plugin nobody in this repository wrote."""

    name = "mikrotik"
    platforms = ("routeros",)
    description = "RouterOS over SSH"

    def collect(self, ctx: CollectContext) -> CollectedFacts:
        facts = CollectedFacts(
            facts=DeviceFacts(
                hostname="rb5009",
                vendor="MikroTik",
                model="RB5009UG",
                os_version="7.14",
                serial_number="HFG0123",
            ),
            interfaces={
                "ether1": InterfaceFacts(is_up="up", is_enabled="enabled", speed="1000"),
                "ether2": InterfaceFacts(is_up="down", is_enabled="disabled"),
            },
            interfaces_ip={
                "ether1": InterfaceAddressFacts.model_validate(
                    {"ipv4": {"192.0.2.1": {"prefix_length": "24"}}}
                )
            },
        )
        facts.require_hostname(fallback=ctx.name)
        return facts


class RecordingSink(Sink):
    """Stands in for a sink for some other source of truth."""

    name = "recording"

    def __init__(self, settings=None):
        super().__init__(settings)
        self.opened = False
        self.closed = False
        self.synced = []
        self.reachable = []
        self.unreachable = []

    def open(self):
        self.opened = True

    def sync(self, ctx: SyncContext) -> SyncReport:
        self.synced.append(ctx.result)
        return SyncReport(
            sink=self.name,
            device_created=True,
            interfaces_created=len(ctx.result.interfaces),
            ip_addresses_created=len(ctx.result.ip_addresses),
        )

    def close(self):
        self.closed = True

    def mark_reachable(self, device_name):
        self.reachable.append(device_name)

    def mark_unreachable(self, device_name):
        self.unreachable.append(device_name)


@pytest.fixture
def registry():
    """A registry of its own, so a test never mutates the real one."""
    return PluginRegistry("collector", Collector, "net2sot.test_collectors")


# ── Registration ─────────────────────────────────────────────────────


class TestRegistration:
    def test_register_and_get(self, registry):
        registry.register(MikroTikCollector)
        assert registry.get("mikrotik") is MikroTikCollector
        assert "mikrotik" in registry

    def test_lookup_is_case_insensitive(self, registry):
        registry.register(MikroTikCollector)
        assert registry.get("MikroTik") is MikroTikCollector

    def test_unknown_name_lists_what_is_installed(self, registry):
        registry.register(MikroTikCollector)
        with pytest.raises(PluginNotFound, match="mikrotik"):
            registry.get("juniper-tng")

    def test_a_second_plugin_claiming_a_name_is_an_error(self, registry):
        # Two plugins silently claiming "netbox" is a configuration problem the
        # operator has to see, not one to settle by import order.
        registry.register(MikroTikCollector)

        class Impostor(Collector):
            name = "mikrotik"

            def collect(self, ctx):
                ...

        with pytest.raises(ValueError, match="already registered"):
            registry.register(Impostor)
        registry.register(Impostor, replace=True)
        assert registry.get("mikrotik") is Impostor

    def test_re_registering_the_same_class_is_fine(self, registry):
        registry.register(MikroTikCollector)
        registry.register(MikroTikCollector)

    def test_a_nameless_plugin_is_rejected(self, registry):
        class Nameless(Collector):
            def collect(self, ctx):
                ...

        with pytest.raises(ValueError, match="no name"):
            registry.register(Nameless)

    def test_something_that_is_not_a_plugin_is_rejected(self, registry):
        with pytest.raises(TypeError):
            registry.register(dict, name="nope")

    def test_create_passes_the_run_settings_through(self, registry):
        registry.register(MikroTikCollector)
        assert registry.create("mikrotik", {"max_retries": 9}).settings["max_retries"] == 9


class TestEntryPoints:
    def test_the_built_ins_are_found_the_same_way_a_plugin_would_be(self):
        # They are declared in pyproject's entry points, not hard-coded here.
        assert {"napalm", "scrapli", "netmiko"} <= set(collectors.names())
        assert "netbox" in sinks.names()

    def test_a_broken_plugin_is_skipped_not_fatal(self, registry, monkeypatch, caplog):
        # One unrelated package failing to import must not take down a run that
        # does not use it.
        broken = SimpleNamespace(
            name="broken",
            value="nonexistent.module:Thing",
            load=MagicMock(side_effect=ImportError("no such module")),
        )
        monkeypatch.setattr(registry, "_entry_points", lambda: [broken])
        registry.load_entry_points()
        assert registry.names() == []
        assert "failed to import" in caplog.text

    def test_concurrent_lookups_never_see_a_half_built_registry(self, registry, monkeypatch):
        """
        Lookups happen on Nornir's worker threads, so two hosts can ask for
        their collectors at once while the entry points are still importing.

        Regression: the "already loading" flag was set before the imports (it
        has to be, so a plugin importing the registry does not recurse), which
        let a second thread straight past into a registry that was still empty
        and told it its collector did not exist. Caught by a live parallel run
        against two devices, where one host collected and the other failed with
        "no collector named 'paloalto'. Installed: none".
        """
        import threading
        import time

        def slow_load():
            # Stands in for importing a vendor SDK: slow enough that a second
            # thread reaches the registry mid-load.
            time.sleep(0.05)
            return MikroTikCollector

        monkeypatch.setattr(
            registry,
            "_entry_points",
            lambda: [SimpleNamespace(name="mikrotik", value="x:Y", load=slow_load)],
        )

        results, errors = [], []

        def lookup():
            try:
                results.append(registry.get("mikrotik"))
            except Exception as exc:  # noqa: BLE001 - the failure is the assertion
                errors.append(exc)

        threads = [threading.Thread(target=lookup) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not errors, f"concurrent lookup failed: {errors[0]}"
        assert results == [MikroTikCollector] * 8

    def test_a_plugin_importing_the_registry_mid_load_does_not_deadlock(self, registry, monkeypatch):
        # The lock is re-entrant for this: the import of a plugin module may
        # itself touch the registry, on the very thread that holds the lock.
        def reentrant_load():
            registry.load_entry_points()  # must return, not block on itself
            return MikroTikCollector

        monkeypatch.setattr(
            registry,
            "_entry_points",
            lambda: [SimpleNamespace(name="mikrotik", value="x:Y", load=reentrant_load)],
        )
        registry.load_entry_points()
        assert registry.get("mikrotik") is MikroTikCollector

    def test_an_entry_point_pointing_at_a_non_plugin_is_skipped(self, registry, monkeypatch, caplog):
        bogus = SimpleNamespace(name="bogus", value="builtins:dict", load=lambda: dict)
        monkeypatch.setattr(registry, "_entry_points", lambda: [bogus])
        registry.load_entry_points()
        assert registry.names() == []
        assert "bogus" in caplog.text


# ── Platform resolution ──────────────────────────────────────────────


def _host(name="rb1", platform="routeros", data=None):
    """A stand-in for a Nornir host: the three things resolution reads."""
    data = data or {}
    return SimpleNamespace(
        name=name, platform=platform, hostname="198.51.100.9", get=data.get
    )


class TestPlatformResolution:
    def test_a_platform_resolves_to_the_collector_that_claims_it(self):
        # "*" marks a default, not a platform claim, so it never wins the
        # automatic match ahead of a collector that named the platform. f5 and
        # paloalto are each claimed by exactly one vendor plugin; srlinux only
        # by the netmiko backend.
        assert collector_for_platform("f5").name == "f5"
        assert collector_for_platform("paloalto").name == "paloalto"
        assert collector_for_platform("srlinux").name == "netmiko"

    def test_no_claim_no_match(self):
        # A made-up platform, not a plausible one: whatever plugins happen to be
        # installed in the environment running these tests must not change the
        # answer. (An earlier version of this test asked about "routeros" and
        # started failing the moment the example plugin was installed.)
        assert collector_for_platform("not-a-real-platform") is None
        assert collector_for_platform("") is None

    def test_the_inventory_pin_always_wins(self):
        host = _host(platform="ios", data={"collector": "netmiko"})
        assert resolve_collector_name(host, {"collector": "napalm"}) == "netmiko"

    def test_the_run_default_is_used_when_it_supports_the_platform(self):
        assert resolve_collector_name(_host(platform="ios"), {"collector": "napalm"}) == "napalm"

    def test_a_platform_the_default_cannot_reach_falls_to_one_that_claims_it(self):
        # NAPALM ships no F5 driver, so an unpinned F5 host still gets collected
        # -- by the f5 plugin, which is why its group no longer needs a
        # `collector:` pin in inventory/groups.yaml.
        assert resolve_collector_name(_host(platform="f5"), {"collector": "napalm"}) == "f5"
        assert resolve_collector_name(
            _host(platform="paloalto"), {"collector": "napalm"}
        ) == "paloalto"

    def test_an_installed_plugin_wins_a_platform_nothing_built_in_claims(self):
        # What installing a third-party collector buys you: a host on its
        # platform is collected by it without any inventory or settings change.
        class Exotic(Collector):
            name = "exotic-test-only"
            platforms = ("exotic-nos",)

            def collect(self, ctx):
                ...

        collectors.register(Exotic)
        try:
            assert resolve_collector_name(
                _host(platform="exotic-nos"), {"collector": "napalm"}
            ) == "exotic-test-only"
        finally:
            collectors.unregister("exotic-test-only")

    def test_an_unknown_configured_name_is_passed_through_to_fail_loudly(self):
        # Reported once, when the run builds its collectors, with the full list.
        assert resolve_collector_name(_host(platform="ios"), {"collector": "typo"}) == "typo"


# ── A whole run on plugins this project does not ship ────────────────


class TestThirdPartyPipeline:
    """
    discover_device end to end with an outside collector and an outside sink.
    Nothing in net2sot knows either of them.
    """

    @pytest.fixture
    def task(self):
        host = _host()
        return SimpleNamespace(host=host, name="discover_device")

    @pytest.fixture
    def settings(self):
        return {
            "collector": "mikrotik",
            "interface_filters": {"exclude_disabled": True},
            "device_type_mapping": {"mikrotik": {"default": "routerboard"}},
        }

    def test_collect_process_and_sync(self, task, settings):
        sink = RecordingSink(settings)
        result = discover_device(
            task, settings, sink, {"mikrotik": MikroTikCollector(settings)}
        )

        assert not result.failed
        data = sink.synced[0]
        assert data.device.hostname == "RB5009"
        assert data.device.vendor == "MikroTik"
        # An identifiable model becomes the device type as-is; the mapping's
        # `default` only catches a device that reported nothing usable.
        assert data.device.device_type == "RB5009UG"
        # ether2 is administratively down and exclude_disabled is on.
        assert [i.name for i in data.interfaces] == ["ether1"]
        assert data.interfaces[0].speed == 1000
        assert [ip.address for ip in data.ip_addresses] == ["192.0.2.1/24"]
        # Provenance rides on the result, not on the shared settings dict.
        assert (data.platform, data.collector) == ("routeros", "mikrotik")
        assert sink.reachable == ["RB5009"]

    def test_a_collector_that_raises_marks_the_device_unreachable(self, task, settings):
        class Unreachable(Collector):
            name = "mikrotik"
            platforms = ("routeros",)

            def collect(self, ctx):
                raise ConnectionError("timed out")

        sink = RecordingSink(settings)
        result = discover_device(task, settings, sink, {"mikrotik": Unreachable(settings)})

        assert result.failed
        assert "timed out" in result.result["error"]
        assert sink.unreachable == ["rb1"]  # by inventory name: nothing was collected
        assert sink.synced == []

    def test_a_device_with_no_usable_data_is_not_synced(self, task, settings):
        class Empty(Collector):
            name = "mikrotik"
            platforms = ("routeros",)

            def collect(self, ctx):
                return CollectedFacts(facts=DeviceFacts(hostname="x"))

        result = discover_device(task, settings, (sink := RecordingSink(settings)),
                                 {"mikrotik": Empty(settings)})
        assert result.failed
        assert sink.synced == []

    def test_a_partial_sync_fails_the_host(self, task, settings):
        # A device that only half landed is not a success: reporting it as one
        # is how an overloaded API produces silently incomplete data behind a
        # green run and a zero exit code.
        class Partial(RecordingSink):
            def sync(self, ctx):
                super().sync(ctx)
                return SyncReport(sink=self.name, ip_addresses_failed=1, errors=["503"])

        result = discover_device(task, settings, Partial(settings),
                                 {"mikrotik": MikroTikCollector(settings)})
        assert result.failed
        assert result.result["stats"].ip_addresses_failed == 1

    def test_a_sink_that_cannot_record_reachability_does_not_break_the_run(self, task, settings):
        class Minimal(Sink):
            name = "minimal"

            def sync(self, ctx):
                return SyncReport(sink=self.name)

        result = discover_device(task, settings, Minimal(settings),
                                 {"mikrotik": MikroTikCollector(settings)})
        assert not result.failed


class TestCollectContext:
    def test_option_prefers_the_host_over_the_run_settings(self):
        # The precedence the rest of the project uses: a group may pin
        # behaviour while a CLI flag still sets the default for everything else.
        ctx = CollectContext(
            task=SimpleNamespace(host=_host(data={"max_retries": 5})),
            platform="routeros",
            settings={"max_retries": 2, "retry_delay": 7},
        )
        assert ctx.option("max_retries") == 5
        assert ctx.option("retry_delay") == 7
        assert ctx.option("absent", "fallback") == "fallback"

    def test_exposes_the_host_identity(self):
        ctx = CollectContext(task=SimpleNamespace(host=_host()), platform="routeros")
        assert (ctx.name, ctx.address) == ("rb1", "198.51.100.9")
