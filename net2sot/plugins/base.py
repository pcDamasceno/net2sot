"""
plugins/base.py - What a plugin implements.

Two plugin kinds, and the line between them is where the vendor-specific part
of this project ends:

    Collector   talks to one device and returns CollectedFacts. Everything a
                new vendor needs lives here -- transport, commands, parsing.
    Sink        takes a normalized DiscoveryResult and writes it to a source of
                truth. NetBox is one; Infrahub, a CMDB or a file are others.

Everything between them -- canonical interface names, filtering, device-type
mapping, VRF resolution, primary-IP selection, reporting -- is platform- and
target-agnostic and stays in this project. A plugin author does not re-implement
it, and does not get to break it.

Both kinds take their configuration as the run's settings mapping and receive a
per-call context object rather than a widening argument list, so that a future
field on the context does not break plugins compiled against today's signature.
"""

from __future__ import annotations

import abc
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

from net2sot.schemas import CollectedFacts, DiscoveryResult, SyncReport

# ── Contexts ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CollectContext:
    """
    Everything a collector is given for one device.

    Plain dataclass rather than a model: it carries live objects (the Nornir
    task and its open connections) that validation has no business touching.
    """

    # The Nornir task for this host. Run commands with it the way any Nornir
    # task does -- task.run(task=napalm_get, ...), task.host.get_connection(...).
    task: Any
    # The host's platform as the inventory spells it: "ios", "srlinux", "f5".
    platform: str
    # The whole run's settings (settings.yaml, overlaid by env and CLI).
    settings: Mapping[str, Any] = field(default_factory=dict)

    @property
    def host(self) -> Any:
        """The Nornir host object."""
        return self.task.host

    @property
    def name(self) -> str:
        """The inventory name of the device, which may differ from its hostname."""
        return self.task.host.name

    @property
    def address(self) -> str | None:
        """The address or DNS name Nornir is connecting to."""
        return self.task.host.hostname

    def option(self, key: str, default: Any = None) -> Any:
        """
        One configuration value, resolved the way the rest of the project
        resolves them: the host's own data (which it inherits from its groups)
        wins over the run-wide settings.

        That ordering is what lets a group pin behaviour for the platforms that
        need it while a CLI flag still sets the default for everything else --
        the same reason `collector` is read off the host before settings.
        """
        value = self.task.host.get(key)
        if value is not None:
            return value
        return self.settings.get(key, default)


@dataclass(frozen=True)
class SyncContext:
    """Everything a sink is given for one device."""

    # The normalized discovery data to write.
    result: DiscoveryResult
    # The whole run's settings.
    settings: Mapping[str, Any] = field(default_factory=dict)
    # ISO-8601 UTC timestamp of when this device's discovery started, for
    # "last synced at"-style fields.
    start_time: str = ""

    @property
    def device_name(self) -> str:
        """The name this device should carry in the source of truth."""
        return self.result.device_name or self.result.device.hostname

    @property
    def platform(self) -> str:
        """
        The device's platform, taken off the result rather than out of settings:
        every worker thread shares one settings dict, so reading it there would
        race across a mixed-platform inventory.
        """
        return self.result.platform


# ── Collector ────────────────────────────────────────────────────────


class Collector(abc.ABC):
    """
    Base class for a collector plugin.

    Minimal implementation::

        from net2sot.plugins import Collector, CollectContext
        from net2sot.schemas import CollectedFacts, DeviceFacts

        class MikroTikCollector(Collector):
            name = "mikrotik"
            platforms = ("routeros",)

            def collect(self, ctx: CollectContext) -> CollectedFacts:
                facts = CollectedFacts()
                facts.facts = DeviceFacts(vendor="MikroTik", ...)
                ...
                return facts

    Register it from your own package's entry points (see docs/plugins.md); no
    change to this repository is needed.
    """

    #: How the collector is selected: --collector <name>, or `collector: <name>`
    #: on a group in the inventory. Must be unique across installed plugins.
    name: ClassVar[str] = ""

    #: Platforms this collector can talk to, matched against the host's
    #: `platform`. ("*",) means "any" -- claim that only for a genuinely
    #: platform-agnostic backend, because it is also what makes a collector
    #: eligible to be chosen automatically for a platform nothing else claims.
    platforms: ClassVar[tuple[str, ...]] = ("*",)

    #: One line, shown by `discovery.py --list-plugins`.
    description: ClassVar[str] = ""

    def __init__(self, settings: Mapping[str, Any] | None = None) -> None:
        # Constructed once per run, then called once per device, so anything
        # expensive and device-independent (loading templates, reading config)
        # belongs here rather than in collect().
        self.settings: Mapping[str, Any] = settings or {}

    @abc.abstractmethod
    def collect(self, ctx: CollectContext) -> CollectedFacts:
        """
        Gather everything this backend can from one device.

        Raise on a failure that makes the device undiscoverable -- an
        unreachable host, a refused login, a platform this collector does not
        support. The run marks that device failed and carries on with the rest
        of the inventory, so there is no need to catch and return empty.

        Do *not* raise for a section that simply is not available: a platform
        with no LLDP, or a command that this software version does not have,
        should leave that part of CollectedFacts empty. The pipeline degrades to
        what it was given.
        """

    @classmethod
    def supports(cls, platform: str) -> bool:
        """
        Whether this collector claims `platform`. A classmethod so the question
        can be asked of the class, before a run has instantiated anything.
        """
        if "*" in cls.platforms:
            return True
        return (platform or "").lower() in {p.lower() for p in cls.platforms}


# ── Sink ─────────────────────────────────────────────────────────────


class Sink(abc.ABC):
    """
    Base class for a sink plugin: a source of truth discovery writes into.

    `open()` and `close()` bracket the whole run, `sync()` is called once per
    device from a worker thread -- so anything `sync()` touches on `self` has to
    be safe to touch concurrently.
    """

    #: How the sink is selected: --sink <name>, or `sinks: [<name>]` in settings.
    name: ClassVar[str] = ""

    #: One line, shown by `discovery.py --list-plugins`.
    description: ClassVar[str] = ""

    def __init__(self, settings: Mapping[str, Any] | None = None) -> None:
        self.settings: Mapping[str, Any] = settings or {}

    def open(self) -> None:  # noqa: B027 - optional hook; the default is to do nothing
        """
        Prepare for a run: connect, authenticate, ensure schema (custom fields,
        object types) exists. Called once, before any device is synced. Raising
        here aborts the run, which is the right thing for an unreachable or
        misconfigured target -- discovering a whole inventory into a target that
        cannot accept it is wasted work.
        """

    @abc.abstractmethod
    def sync(self, ctx: SyncContext) -> SyncReport:
        """
        Write one device's discovery data, and report what happened.

        Report failures in the returned SyncReport (`errors`, the `*_failed`
        counters) rather than by raising: a device that partly landed is a
        distinct outcome from one that could not be collected at all, and the
        run needs to tell them apart. Raise only when the target has become
        unusable for every device, not just this one.
        """

    def close(self) -> None:  # noqa: B027 - optional hook; the default is to do nothing
        """Release whatever `open()` acquired. Called once, after the last device."""

    # ── Optional reachability hooks ──────────────────────────────────
    #
    # Discovery knows something a source of truth usually does not: whether the
    # device answered. A sink that can record that implements these; the default
    # is to do nothing, so a sink that has no such concept ignores them.

    def mark_unreachable(self, device_name: str) -> None:  # noqa: B027 - optional hook
        """Record that this device could not be collected from."""

    def mark_reachable(self, device_name: str) -> None:  # noqa: B027 - optional hook
        """Record that this device answered and was collected from."""
