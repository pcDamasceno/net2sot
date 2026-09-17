"""
sinks/netbox.py - NetBox as a sink plugin.

The writing itself lives in tasks/netbox_sync.py and the API client in
netbox_client.py; this is the lifecycle around them -- connect once, ensure the
custom fields exist, sync each device, record reachability.

What to copy when writing a sink for another source of truth:

  open()              is where a target that cannot accept the data should fail,
                      before an inventory's worth of collection is spent on it.
                      Schema/custom-field creation belongs here too: once per
                      run, not once per device.
  sync()              runs on a worker thread, one device at a time, in
                      parallel. Everything it touches on `self` has to tolerate
                      that -- here, one NetboxClient whose connection pool is
                      sized to the worker count.
  failures            go in the returned SyncReport, not into an exception. A
                      device that landed by halves is a different outcome from
                      one that was never collected, and the run has to be able
                      to say which happened.
  mark_*()            are optional. Discovery knows whether the device answered,
                      which a source of truth usually does not; a target with
                      nowhere to put that simply does not implement them.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from net2sot.netbox_client import NetboxClient
from net2sot.plugins import Sink, SyncContext
from net2sot.schemas import SyncReport
from net2sot.tasks.netbox_sync import sync_to_netbox

logger = logging.getLogger("discovery.sinks.netbox")


class NetBoxSink(Sink):
    """Write discovered devices, interfaces, addresses, VRFs and cables to NetBox."""

    name = "netbox"
    description = "NetBox DCIM/IPAM over its REST API (pynetbox)"

    def __init__(
        self,
        settings: Mapping[str, Any] | None = None,
        client: NetboxClient | None = None,
    ) -> None:
        super().__init__(settings)
        # A caller that already holds a client passes it in rather than opening
        # a second one: discovery.py needs the same client (and the same branch)
        # to source a --from-netbox inventory before any sync happens.
        self._client = client

    @property
    def client(self) -> NetboxClient:
        """The underlying API client. `open()` must have run."""
        if self._client is None:
            raise RuntimeError("NetBoxSink.open() has not been called")
        return self._client

    # ── Lifecycle ────────────────────────────────────────────────────

    def open(self) -> None:
        if self._client is None:
            # A requested branch is resolved and activated by the constructor,
            # so a bad branch name fails here -- before anything is collected.
            self._client = NetboxClient(
                url=self.settings["netbox_url"],
                token=self.settings["netbox_token"],
                validate_certs=self.settings.get("netbox_validate_certs", False),
                branch=self.settings.get("netbox_branch"),
                # One client is shared by every worker, so its connection pool
                # has to hold a connection per worker; a smaller pool just makes
                # urllib3 discard and rebuild connections under load.
                pool_size=self.settings.get("num_workers", 10),
            )
        self.ensure_custom_fields()

    def ensure_custom_fields(self) -> None:
        """
        Create the custom-field definitions declared in settings.yaml that opt
        into enforce_creation, so the fields exist before the per-device sync
        writes their values. Fields without enforce_creation are assumed to
        already exist. Once per run, not per device.
        """
        for cf in self.settings.get("custom_fields") or []:
            name = cf.get("name")
            if not name or not cf.get("enforce_creation"):
                continue
            self.client.ensure_custom_field(
                name=name,
                label=cf.get("label", name),
                cf_type=cf.get("type", "text"),
                object_types=cf.get("object_types") or ["dcim.device"],
                description=cf.get("description", ""),
            )

    # ── Per device ───────────────────────────────────────────────────

    def sync(self, ctx: SyncContext) -> SyncReport:
        report = sync_to_netbox(
            nb=self.client,
            data=ctx.result,
            settings=ctx.settings,
            start_time=ctx.start_time,
            platform=ctx.platform,
        )
        report.sink = self.name
        return report

    # ── Reachability ─────────────────────────────────────────────────

    def mark_unreachable(self, device_name: str) -> None:
        """
        Flag the device failed in NetBox, so one we could not gather from is
        visible there and not only in the run log. Looked up by the name given
        (which matches the NetBox name on a --from-netbox re-discovery); a no-op
        if the device is not in NetBox yet. Only a currently-active device is
        touched, so a deliberately staged/planned/decommissioning status set by
        hand is left alone.
        """
        self.client.set_device_status(device_name, "failed", only_if_current_in={"active"})

    def mark_reachable(self, device_name: str) -> None:
        """
        Recover the device to "active" -- but only from the down states
        discovery itself sets, mirroring the guard in mark_unreachable().
        """
        self.client.set_device_status(
            device_name, "active", only_if_current_in={"failed", "offline"}
        )
