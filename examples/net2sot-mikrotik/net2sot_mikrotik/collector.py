"""
A worked example of a collector plugin, in the shape a real one takes: it lives
in its own repository, depends on net2sot for the contract, brings its own
transport, and is wired in by an entry point alone.

The RouterOS parsing here is deliberately small -- enough to be a real starting
point, not enough to be a finished driver. What is worth copying is the shape:
every command is best-effort, the sections this box cannot answer are left empty,
and the only hard failure is a device that will not talk at all.
"""

from __future__ import annotations

import logging
import re

from net2sot.plugins import CollectContext, Collector
from net2sot.schemas import CollectedFacts, DeviceFacts, InterfaceFacts

logger = logging.getLogger("discovery.collect.mikrotik")

# RouterOS prints "key: value" pairs, one per line, indented under a record.
_KEY_VALUE = re.compile(r"^\s*([\w-]+):\s*(.*?)\s*$")

# "1w2d03:04:05" -- RouterOS' uptime format.
_UPTIME = re.compile(r"(?:(\d+)w)?(?:(\d+)d)?(?:(\d+):)?(?:(\d+):)?(\d+)$")


def _key_values(output: str) -> dict[str, str]:
    """The "key: value" lines of a single-record `print` into a dict."""
    values: dict[str, str] = {}
    for line in output.splitlines():
        match = _KEY_VALUE.match(line)
        if match:
            values[match.group(1)] = match.group(2)
    return values


def _uptime_seconds(value: str) -> int:
    """'1w2d03:04:05' -> seconds. 0 when it does not parse."""
    match = _UPTIME.match(value.strip())
    if not match:
        return 0
    weeks, days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return ((weeks * 7 + days) * 24 + hours) * 3600 + minutes * 60 + seconds


class MikroTikCollector(Collector):
    """Collect a RouterOS device over a Netmiko SSH session."""

    # What the operator types after --collector, and what the entry point in
    # pyproject.toml is keyed by.
    name = "mikrotik"

    # The platforms this collector claims. Naming them is what lets a host with
    # `platform: routeros` be matched automatically -- no groups.yaml pin and no
    # settings.yaml entry needed, just the package installed.
    platforms = ("routeros", "mikrotik")

    description = "MikroTik RouterOS over SSH (Netmiko)"

    def collect(self, ctx: CollectContext) -> CollectedFacts:
        facts = CollectedFacts()

        # ── Device facts ─────────────────────────────────────────────
        # Let this one propagate: a box that cannot answer its own resource
        # print is not going to answer anything else, and failing here marks the
        # device unreachable and moves the run on to the next host.
        resource = _key_values(self._send(ctx, "/system resource print"))
        identity = _key_values(self._send(ctx, "/system identity print"))

        facts.facts = DeviceFacts(
            hostname=identity.get("name", ""),
            vendor="MikroTik",
            # "" and "N/A" are absorbed by the contract, so no cleanup here.
            model=resource.get("board-name", ""),
            os_version=resource.get("version", ""),
            uptime=_uptime_seconds(resource.get("uptime", "")),
            serial_number=resource.get("serial-number", ""),
        )

        # ── Interfaces ───────────────────────────────────────────────
        # Best effort from here down: a section this box cannot answer is left
        # empty and the pipeline degrades to what it was given, rather than the
        # whole device being thrown away over one missing command.
        try:
            self._collect_interfaces(ctx, facts)
        except Exception as exc:
            logger.warning(f"[{ctx.name}] interface print failed: {exc}")

        try:
            self._collect_addresses(ctx, facts)
        except Exception as exc:
            logger.warning(f"[{ctx.name}] address print failed: {exc}")

        # A device with no name cannot be created in a source of truth. The
        # inventory name stands in rather than the device being rejected.
        facts.require_hostname(fallback=ctx.name)
        logger.info(f"[{ctx.name}] RouterOS collection done: {facts.summary()}")
        return facts

    # ── Helpers ──────────────────────────────────────────────────────

    def _send(self, ctx: CollectContext, command: str) -> str:
        from nornir_netmiko.tasks import netmiko_send_command

        # ctx.option() reads the host's own data first, then the run settings --
        # the precedence the rest of the project uses, so a slow group can raise
        # its own timeout without changing anyone else's.
        return ctx.task.run(
            task=netmiko_send_command,
            command_string=command,
            read_timeout=ctx.option("netmiko_read_timeout", 60),
        )[0].result

    def _collect_interfaces(self, ctx: CollectContext, facts: CollectedFacts) -> None:
        output = self._send(ctx, "/interface print detail without-paging")
        for record in self._records(output):
            name = record.get("name", "")
            if not name:
                continue
            facts.interfaces[name] = InterfaceFacts(
                # "true"/"false" and "yes"/"no" both resolve to booleans.
                is_up=record.get("running", "false"),
                is_enabled=not _is_true(record.get("disabled", "false")),
                description=record.get("comment", ""),
                mac_address=record.get("mac-address", ""),
                # "" is absorbed; no need to guard.
                mtu=record.get("mtu", ""),
                # RouterOS knows what kind of link this is, so say so rather
                # than leaving the pipeline to guess from the name. Anything
                # unset falls back to the name heuristic.
                netbox_type=_NETBOX_TYPES.get(record.get("type", ""), ""),
                is_virtual=record.get("type", "") in _VIRTUAL_TYPES or None,
            )

    def _collect_addresses(self, ctx: CollectContext, facts: CollectedFacts) -> None:
        output = self._send(ctx, "/ip address print detail without-paging")
        for record in self._records(output):
            interface = record.get("interface", "")
            address = record.get("address", "")
            if not interface or "/" not in address:
                continue
            facts.add_address(interface, address)

    @staticmethod
    def _records(output: str) -> list[dict[str, str]]:
        """
        Split a `print detail` into one dict per numbered record.

        RouterOS starts each record with its index, then flows "key=value" pairs
        across as many lines as it likes.
        """
        records: list[dict[str, str]] = []
        for chunk in re.split(r"\n(?=\s*\d+\s)", output):
            pairs = {
                key: value.strip('"')
                for key, value in re.findall(r'([\w-]+)=("[^"]*"|\S+)', chunk)
            }
            if pairs:
                records.append(pairs)
        return records


def _is_true(value: str) -> bool:
    return value.strip().strip('"').lower() in {"true", "yes"}


# RouterOS interface types → source-of-truth port types.
_NETBOX_TYPES = {
    "ether": "1000base-t",
    "wlan": "ieee802.11ac",
    "bridge": "bridge",
    "bond": "lag",
    "vlan": "virtual",
}

_VIRTUAL_TYPES = {"bridge", "vlan", "vrrp", "pppoe-out", "ovpn-out", "wg"}
