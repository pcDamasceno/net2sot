"""
The discovery contract.

Three layers, each a Pydantic model, each the boundary between a part somebody
else may replace and a part this project owns:

    CollectedFacts    what a collector plugin returns  (schemas/facts.py)
    DiscoveryResult   what a sink plugin receives      (schemas/discovery.py)
    SyncReport        what a sink plugin returns       (schemas/sync.py)

Import them from here rather than from the submodules -- this is the surface
that is versioned (SCHEMA_VERSION) and kept stable for plugins.

    from net2sot.schemas import CollectedFacts, DiscoveryResult

See docs/plugins.md for how to write a plugin against them.
"""

from net2sot.schemas.base import SCHEMA_VERSION, DiscoverySchema
from net2sot.schemas.discovery import (
    DiscoveredDevice,
    DiscoveredInterface,
    DiscoveredIP,
    DiscoveredLLDPNeighbor,
    DiscoveredVRF,
    DiscoveryResult,
    ProcessedData,
)
from net2sot.schemas.facts import (
    CollectedFacts,
    DeviceFacts,
    InterfaceAddressFacts,
    InterfaceFacts,
    IPAddressFacts,
    LLDPNeighborDetailFacts,
    LLDPNeighborFacts,
    NetworkInstanceFacts,
    NetworkInstanceInterfaces,
    NetworkInstanceState,
    RouteTargets,
)
from net2sot.schemas.sync import SyncReport, SyncStats

__all__ = [
    "SCHEMA_VERSION",
    "DiscoverySchema",
    # Collector contract
    "CollectedFacts",
    "DeviceFacts",
    "InterfaceFacts",
    "InterfaceAddressFacts",
    "IPAddressFacts",
    "LLDPNeighborFacts",
    "LLDPNeighborDetailFacts",
    "NetworkInstanceFacts",
    "NetworkInstanceInterfaces",
    "NetworkInstanceState",
    "RouteTargets",
    # Sink contract
    "DiscoveryResult",
    "DiscoveredDevice",
    "DiscoveredInterface",
    "DiscoveredIP",
    "DiscoveredVRF",
    "DiscoveredLLDPNeighbor",
    "SyncReport",
    # Former names, still resolvable
    "ProcessedData",
    "SyncStats",
]
