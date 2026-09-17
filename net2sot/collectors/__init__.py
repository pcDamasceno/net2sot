"""
The collectors this project ships.

They are ordinary plugins: each subclasses `Collector`, is advertised from
pyproject's "net2sot.collectors" entry points, and is found by the same
registry lookup that finds a third party's. Nothing here is privileged, which is
deliberate -- if the plugin API is awkward, it is awkward for us first.

Read them as worked examples, in roughly increasing order of what they show:

    napalm.py     the thinnest -- NAPALM's output already is the contract
    scrapli.py    the same, over a different transport
    netmiko.py    a backend that dispatches per platform internally
    paloalto.py   a whole vendor in one file: its own commands, its own parsing
    f5.py         the same, for a platform nothing else on PyPI can reach

paloalto.py and f5.py are the ones to copy when adding a vendor. Both were part
of tasks/collect_netmiko.py before the plugin system existed; each is now
self-contained, declares the platforms it claims, and reaches the transport
through the public helpers in netmiko_support.py -- nothing a plugin in another
repository could not do.
"""

from net2sot.collectors.f5 import F5Collector
from net2sot.collectors.napalm import NapalmCollector
from net2sot.collectors.netmiko import NetmikoCollector
from net2sot.collectors.paloalto import PaloAltoCollector
from net2sot.collectors.scrapli import ScrapliCollector

__all__ = [
    "NapalmCollector",
    "ScrapliCollector",
    "NetmikoCollector",
    "PaloAltoCollector",
    "F5Collector",
]
