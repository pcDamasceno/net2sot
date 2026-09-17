"""
schemas/sync.py - What a sink reports back.

A sink writes a `DiscoveryResult` into a source of truth and returns a
`SyncReport` saying what it did. The counters are named after objects that any
DCIM/IPAM has -- sites, devices, interfaces, addresses, VRFs, cables -- rather
than after NetBox's API, so an Infrahub or a NetXMS sink fills the same shape.
A sink that tracks something outside this vocabulary puts it in `counters`,
which the run report prints without needing to know what it means.
"""

from __future__ import annotations

from pydantic import Field

from net2sot.schemas.base import DiscoverySchema


class SyncReport(DiscoverySchema):
    """The outcome of syncing one device to one source of truth."""

    # Which sink produced this, so a run that writes to two of them stays legible.
    sink: str = ""

    site_created: bool = False
    device_created: bool = False

    interfaces_created: int = 0
    interfaces_existing: int = 0

    vrfs_created: int = 0

    ip_addresses_created: int = 0
    ip_addresses_failed: int = 0
    # The same address already present in that VRF. A data condition on the
    # device (configured twice, or owned elsewhere), not a sync failure -- it is
    # counted separately so it can be surfaced without failing the run.
    ip_addresses_duplicate: int = 0
    ip_addresses_deleted: int = 0

    cables_created: int = 0
    cables_skipped: int = 0
    cables_failed: int = 0

    # One line per object that could not be written. Non-empty means the device
    # is only partly in the source of truth, which is not a success: a run that
    # reported green here is how an overloaded API produces silently incomplete
    # data behind a zero exit code.
    errors: list[str] = Field(default_factory=list)

    # Counters a particular sink tracks that the vocabulary above has no name
    # for, e.g. {"tags_applied": 3}.
    counters: dict[str, int] = Field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        """Whether everything this sink was asked to write actually landed."""
        return not self.errors and self.ip_addresses_failed == 0

    def failure_summary(self) -> str:
        """One line naming what did not land, for the run log."""
        parts = [f"{self.ip_addresses_failed} IP(s) not written"]
        if self.errors:
            parts.append(f"{len(self.errors)} object(s) failed: " + "; ".join(self.errors))
        return ", ".join(parts)


# The name this model carried when NetBox was the only sink.
SyncStats = SyncReport
