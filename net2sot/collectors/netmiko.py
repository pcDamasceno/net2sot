"""
collectors/netmiko.py - The Netmiko backend as a plugin.

The backend that covers the platforms the others cannot reach: Nokia SR Linux
has no NAPALM driver published at all, and Linux has no driver in either. It
dispatches per platform internally (tasks/collect_netmiko.py).

PAN-OS and BIG-IP used to be two of those platforms and are now plugins of their
own (paloalto.py, f5.py), so this collector no longer claims them. An inventory
that still pins `collector: netmiko` for one of them gets an error naming the
plugin that took over.
"""

from __future__ import annotations

from net2sot.plugins import CollectContext, Collector
from net2sot.schemas import CollectedFacts
from net2sot.tasks.collect_netmiko import collect_netmiko


class NetmikoCollector(Collector):
    """Collect over a plain Netmiko SSH session, parsing with ntc-templates."""

    name = "netmiko"
    description = "Netmiko SSH; the only backend for SR Linux and Linux"

    # Mirrors the dispatch table in tasks/collect_netmiko.py. Adding a platform
    # there means adding it here, or a host on it will not be matched
    # automatically -- the run still works if the inventory pins the collector,
    # which is why the two lists are allowed to be written by hand.
    platforms = ("srlinux", "linux", "ios", "eos", "nxos", "iosxr")

    def collect(self, ctx: CollectContext) -> CollectedFacts:
        facts = CollectedFacts.from_napalm(collect_netmiko(ctx.task, ctx.platform))
        facts.require_hostname(fallback=ctx.name)
        return facts
