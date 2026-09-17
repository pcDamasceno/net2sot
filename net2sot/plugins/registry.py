"""
plugins/registry.py - Finding plugins, wherever they were installed from.

Two ways in, the same as Nornir's and NAPALM's:

  entry points   The one that makes a plugin somebody else's repository. A
                 package declares

                     [project.entry-points."net2sot.collectors"]
                     mikrotik = "nornir_mikrotik:MikroTikCollector"

                 and once it is pip-installed, `--collector mikrotik` works
                 with no change to this project. The built-in collectors are
                 declared the same way, so the path a plugin author takes is
                 the path this project takes -- if it breaks, it breaks for us
                 first.

  register()     For a plugin that is not packaged: a module imported by your
                 own driver script, or a test. Same registry, same lookups.

A third-party plugin that fails to import is logged and skipped, never fatal.
One broken package on the system should not take down a discovery run that does
not use it -- and the alternative, an ImportError from a package the operator
has never heard of, is the worst kind of Monday.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable
from importlib.metadata import EntryPoint, entry_points
from typing import Generic, TypeVar

from net2sot.plugins.base import Collector, Sink

logger = logging.getLogger("discovery.plugins")

COLLECTOR_ENTRY_POINT_GROUP = "net2sot.collectors"
SINK_ENTRY_POINT_GROUP = "net2sot.sinks"

PluginT = TypeVar("PluginT", bound=Collector | Sink)


class PluginNotFound(LookupError):
    """Raised when a plugin was asked for by a name nothing is registered under."""


class PluginRegistry(Generic[PluginT]):
    """A named set of plugins of one kind, backed by an entry-point group."""

    def __init__(self, kind: str, base: type[PluginT], entry_point_group: str) -> None:
        self.kind = kind
        self.base = base
        self.entry_point_group = entry_point_group
        self._plugins: dict[str, type[PluginT]] = {}
        self._loaded_entry_points = False
        # Re-entrant, because a plugin module may import this registry while it
        # is being loaded (see load_entry_points).
        self._lock = threading.RLock()

    # ── Registration ─────────────────────────────────────────────────

    def register(
        self, plugin: type[PluginT], name: str | None = None, replace: bool = False
    ) -> type[PluginT]:
        """
        Add a plugin class under `plugin.name` (or an explicit `name`).

        Returns the class, so this also works as a decorator. Registering over
        an existing name raises unless `replace=True`: two plugins silently
        claiming "netbox" is a configuration problem the operator needs to see,
        not one to resolve by import order.
        """
        if not isinstance(plugin, type) or not issubclass(plugin, self.base):
            raise TypeError(
                f"a {self.kind} plugin must be a subclass of {self.base.__name__}, "
                f"got {plugin!r}"
            )
        key = (name or getattr(plugin, "name", "") or "").strip().lower()
        if not key:
            raise ValueError(
                f"{plugin.__name__} has no name: set a class-level `name` attribute, "
                f"or pass name= to register()"
            )
        with self._lock:
            existing = self._plugins.get(key)
            if existing is not None and existing is not plugin and not replace:
                raise ValueError(
                    f"a {self.kind} named {key!r} is already registered "
                    f"({existing.__module__}.{existing.__name__}); pass replace=True to override"
                )
            self._plugins[key] = plugin
        return plugin

    def unregister(self, name: str) -> None:
        with self._lock:
            self._plugins.pop(name.strip().lower(), None)

    # ── Lookup ───────────────────────────────────────────────────────

    def get(self, name: str) -> type[PluginT]:
        """The plugin class registered under `name`."""
        self.load_entry_points()
        key = (name or "").strip().lower()
        try:
            return self._plugins[key]
        except KeyError:
            raise PluginNotFound(
                f"no {self.kind} named {key!r}. Installed: {', '.join(self.names()) or 'none'}. "
                f"A plugin from another package must declare a "
                f"'{self.entry_point_group}' entry point and be installed in this environment."
            ) from None

    def create(self, name: str, settings=None, **kwargs) -> PluginT:
        """
        Instantiate the plugin registered under `name` with the run's settings.

        Extra keyword arguments reach the plugin's __init__. Use them only for
        something a specific plugin is known to accept -- anything every plugin
        should be able to read belongs in `settings`, which they all get.
        """
        return self.get(name)(settings, **kwargs)

    def names(self) -> list[str]:
        self.load_entry_points()
        return sorted(self._plugins)

    def available(self) -> dict[str, type[PluginT]]:
        self.load_entry_points()
        return dict(self._plugins)

    def __contains__(self, name: str) -> bool:
        self.load_entry_points()
        return (name or "").strip().lower() in self._plugins

    # ── Entry points ─────────────────────────────────────────────────

    def load_entry_points(self, force: bool = False) -> None:
        """
        Import every plugin advertised for this registry's entry-point group.

        Done once, lazily, on first lookup rather than at import time: a
        discovery run that names one collector should not pay to import every
        vendor SDK installed on the box, and an unused plugin's missing
        dependency should not be able to break it.

        Held under a lock because lookups happen on Nornir's worker threads. The
        flag alone is not enough: set before the imports (which it must be, so a
        plugin importing this registry does not recurse), it would let a second
        thread straight past into a registry that is still half-built, and that
        thread would be told its collector does not exist. The lock makes the
        second thread wait for the first to finish instead; being re-entrant, it
        still lets the *same* thread back through to the flag.
        """
        with self._lock:
            if self._loaded_entry_points and not force:
                return
            # Set inside the lock, before loading: a plugin whose module imports
            # this registry must not re-enter the loading loop.
            self._loaded_entry_points = True
            for entry_point in self._entry_points():
                try:
                    plugin = entry_point.load()
                except Exception as exc:
                    logger.warning(
                        f"Skipping {self.kind} plugin '{entry_point.name}' "
                        f"({entry_point.value}): it failed to import: {exc}"
                    )
                    continue
                try:
                    self.register(plugin, name=entry_point.name, replace=True)
                except (TypeError, ValueError) as exc:
                    logger.warning(
                        f"Skipping {self.kind} plugin '{entry_point.name}' "
                        f"({entry_point.value}): {exc}"
                    )

    def _entry_points(self) -> Iterable[EntryPoint]:
        try:
            return entry_points(group=self.entry_point_group)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"Could not read '{self.entry_point_group}' entry points: {exc}")
            return ()


# ── The registries ───────────────────────────────────────────────────

collectors: PluginRegistry[Collector] = PluginRegistry(
    "collector", Collector, COLLECTOR_ENTRY_POINT_GROUP
)
sinks: PluginRegistry[Sink] = PluginRegistry("sink", Sink, SINK_ENTRY_POINT_GROUP)


def register_collector(plugin: type[Collector] | None = None, **kwargs):
    """Decorator form of ``collectors.register``."""
    if plugin is None:
        return lambda cls: collectors.register(cls, **kwargs)
    return collectors.register(plugin, **kwargs)


def register_sink(plugin: type[Sink] | None = None, **kwargs):
    """Decorator form of ``sinks.register``."""
    if plugin is None:
        return lambda cls: sinks.register(cls, **kwargs)
    return sinks.register(plugin, **kwargs)


def collector_for_platform(platform: str) -> type[Collector] | None:
    """
    A collector that claims `platform` outright, or None.

    Used when nothing pinned a collector for a host: installing a plugin that
    declares `platforms = ("routeros",)` is then enough to make a routeros host
    discoverable, without also editing the inventory to name the collector.
    Collectors that claim everything ("*") are never matched here -- they are
    defaults, not claims -- and a platform claimed by more than one plugin
    resolves by name order so the choice is at least stable and inspectable.
    """
    key = (platform or "").strip().lower()
    if not key:
        return None
    matches = [
        plugin
        for name, plugin in sorted(collectors.available().items())
        if "*" not in plugin.platforms and key in {p.lower() for p in plugin.platforms}
    ]
    return matches[0] if matches else None


def describe_plugins() -> str:
    """
    The installed plugins, as a table. Backs `discovery.py --list-plugins`,
    which is how an operator answers "is my plugin actually installed" without
    starting a run.
    """
    lines: list[str] = []
    for registry in (collectors, sinks):
        lines.append(f"{registry.kind.capitalize()}s ({registry.entry_point_group}):")
        available = registry.available()
        if not available:
            lines.append("  (none installed)")
        for name, plugin in sorted(available.items()):
            platforms = ", ".join(getattr(plugin, "platforms", ())) or "-"
            detail = f"platforms: {platforms}" if registry is collectors else ""
            lines.append(
                f"  {name:<12} {plugin.__module__}.{plugin.__name__}"
                + (f"\n               {detail}" if detail else "")
                + (f"\n               {plugin.description}" if plugin.description else "")
            )
        lines.append("")
    return "\n".join(lines).rstrip()
