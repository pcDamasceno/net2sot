"""
collectors/scrapli.py - The Scrapli backend as a plugin.

Sends 'show' commands and parses them with Genie, falling back to TextFSM. The
converters in tasks/collect.py already shape the result the way the contract
expects, so this wrapper only validates it.
"""

from __future__ import annotations

from net2sot.plugins import CollectContext, Collector
from net2sot.schemas import CollectedFacts
from net2sot.tasks.collect import collect_scrapli


class ScrapliCollector(Collector):
    """Collect over Scrapli, parsing CLI output with Genie or TextFSM."""

    name = "scrapli"
    description = "Scrapli 'show' commands parsed with Genie, falling back to TextFSM"
    platforms = ("ios", "iosxr", "nxos", "eos")

    def collect(self, ctx: CollectContext) -> CollectedFacts:
        facts = CollectedFacts.from_napalm(collect_scrapli(ctx.task, ctx.platform))
        # IOS-XR's 'show version' carries no hostname and the CLI prompt is the
        # only other source; if that failed too, the inventory name stands in
        # rather than the device being rejected for having no name.
        facts.require_hostname(fallback=ctx.name)
        return facts
