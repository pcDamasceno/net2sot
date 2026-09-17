"""
The plugin system.

    from net2sot.plugins import Collector, CollectContext
    from net2sot.schemas import CollectedFacts

Subclass `Collector` to add a vendor, `Sink` to add a source of truth, then
advertise the class from your own package's entry points -- "net2sot.collectors"
or "net2sot.sinks". Nothing here needs to change for either.

See docs/plugins.md.
"""

from net2sot.plugins.base import (
    CollectContext,
    Collector,
    Sink,
    SyncContext,
)
from net2sot.plugins.registry import (
    COLLECTOR_ENTRY_POINT_GROUP,
    SINK_ENTRY_POINT_GROUP,
    PluginNotFound,
    PluginRegistry,
    collector_for_platform,
    collectors,
    describe_plugins,
    register_collector,
    register_sink,
    sinks,
)

__all__ = [
    "Collector",
    "CollectContext",
    "Sink",
    "SyncContext",
    "PluginRegistry",
    "PluginNotFound",
    "collectors",
    "sinks",
    "register_collector",
    "register_sink",
    "collector_for_platform",
    "describe_plugins",
    "COLLECTOR_ENTRY_POINT_GROUP",
    "SINK_ENTRY_POINT_GROUP",
]
