"""
collectors/napalm.py - The NAPALM backend as a plugin.

The thinnest of the three, because NAPALM's getter output *is* the collector
contract's shape (see schemas/facts.py): the work is reading this run's
settings and handing the result to `CollectedFacts.from_napalm`.
"""

from __future__ import annotations

from net2sot.plugins import CollectContext, Collector
from net2sot.schemas import CollectedFacts
from net2sot.tasks.collect import collect_napalm


class NapalmCollector(Collector):
    """Collect with NAPALM drivers, over whatever transport the driver uses."""

    name = "napalm"
    description = "NAPALM getters (facts, interfaces, interfaces_ip, network_instances)"

    # The drivers NAPALM ships. Not a claim on every platform NAPALM can be
    # made to speak: napalm-panos and other community drivers are separate
    # packages, so a firewall reaches NAPALM by being pinned to it in the
    # inventory rather than by being auto-matched here.
    platforms = ("ios", "iosxr", "nxos", "nxos_ssh", "eos", "junos")

    def collect(self, ctx: CollectContext) -> CollectedFacts:
        facts = CollectedFacts.from_napalm(
            collect_napalm(
                ctx.task,
                getters=ctx.option("napalm_getters", ["facts", "interfaces"]),
                max_retries=ctx.option("max_retries", 2),
                retry_delay=ctx.option("retry_delay", 5),
                collect_vrfs=ctx.option("sync_vrfs", True),
            )
        )
        facts.require_hostname(fallback=ctx.name)
        return facts
