"""
collectors/netmiko_support.py - The shared parts of collecting over Netmiko.

A vendor plugin that talks SSH needs a handful of things that are not
vendor-specific: send a command, send one and have ntc-templates parse it,
recover a hostname from the prompt, turn parsed rows into the contract's facts
and neighbour structures. Every collector in this project needed them, and the
PAN-OS and F5 plugins would otherwise have had to reach into
``tasks.collect_netmiko`` for underscore-prefixed functions -- which is exactly
the coupling the plugin system exists to remove, and exactly what a third-party
author would copy.

So they are public here. This module is a thin, stable surface over the
implementations in tasks/collect.py and tasks/collect_netmiko.py; it does not
fork them, so a fix to the parsing reaches every caller.

    from net2sot.collectors.netmiko_support import send, send_textfsm
"""

from __future__ import annotations

from typing import Any

from net2sot.tasks.collect import (
    _convert_cdp,
    _convert_facts,
    _convert_interfaces,
    _convert_interfaces_ip,
    _convert_lldp,
)
from net2sot.tasks.collect_netmiko import (
    _ensure_net_textfsm,
    _prompt_hostname,
    _send,
    _send_textfsm,
)

__all__ = [
    "send",
    "send_textfsm",
    "prompt_hostname",
    "ensure_net_textfsm",
    "convert_facts",
    "convert_interfaces",
    "convert_interfaces_ip",
    "convert_lldp",
    "convert_cdp",
]


def send(task: Any, command: str, expect_string: str | None = None) -> str:
    """
    Run a command and return its raw output.

    `expect_string` overrides netmiko's prompt auto-detection, which is
    unreliable against a shell whose prompt changes (a dynamic PS1, a device
    that decorates its prompt with mode or licence state).
    """
    return _send(task, command, expect_string)


def send_textfsm(
    task: Any, command: str, expect_string: str | None = None
) -> list[dict[str, Any]]:
    """
    Run a command and return the rows ntc-templates parsed out of it, keyed by
    the template's uppercase value names.

    Netmiko hands back the raw string when no template matches or nothing parses
    -- including protocol-disabled banners like '% LLDP is not enabled'. That is
    collapsed to `[]`, so a caller can treat "no rows" as "nothing to report"
    without having to type-check the result.
    """
    return _send_textfsm(task, command, expect_string)


def prompt_hostname(task: Any) -> str:
    """
    The hostname the CLI prompt carries, with its terminator stripped:
    'RP/0/RP0/CPU0:core-rtr01#' → 'core-rtr01', 'pe-emea-01#' → 'pe-emea-01'.

    For the platforms whose 'show version' does not report one. Returns "" if
    the prompt cannot be read.
    """
    return _prompt_hostname(task)


def ensure_net_textfsm() -> None:
    """
    Point netmiko at the installed ntc-templates through NET_TEXTFSM.

    Without it netmiko locates the template directory through an
    importlib.resources call that several Python versions reject, which surfaces
    as "path() got an unexpected keyword argument 'package'" the moment a
    command is parsed. A user-provided NET_TEXTFSM always wins. Idempotent; the
    built-in collectors call it before their first parse.
    """
    _ensure_net_textfsm()


# ── Row converters ───────────────────────────────────────────────────
#
# Rows as ntc-templates (or Genie) produced them, in the shape the contract
# wants. `parser` is "textfsm" or "genie"; `platform` selects the per-platform
# field spellings, which differ between templates for the same fact.


def convert_facts(rows: Any, parser: str, platform: str) -> dict[str, Any]:
    """Parsed 'show version'-style rows → the fields of `DeviceFacts`."""
    return _convert_facts(rows, parser, platform)


def convert_interfaces(rows: Any, parser: str) -> dict[str, Any]:
    """Parsed 'show interfaces' rows → {name: fields of `InterfaceFacts`}."""
    return _convert_interfaces(rows, parser)


def convert_interfaces_ip(rows: Any, parser: str) -> dict[str, Any]:
    """Parsed 'show ip interface' rows → {name: fields of `InterfaceAddressFacts`}."""
    return _convert_interfaces_ip(rows, parser)


def convert_lldp(rows: Any, parser: str) -> tuple[dict[str, list], dict[str, list]]:
    """Parsed LLDP rows → (lldp_neighbors, lldp_neighbors_detail)."""
    return _convert_lldp(rows, parser)


def convert_cdp(rows: Any, parser: str) -> tuple[dict[str, list], dict[str, list]]:
    """Parsed CDP rows → the same pair of structures as `convert_lldp`."""
    return _convert_cdp(rows, parser)
