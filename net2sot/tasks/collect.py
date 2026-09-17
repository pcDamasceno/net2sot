"""
collect.py - Collect device facts via NAPALM or Scrapli.

Supports two collection backends:
  - napalm  (default) – uses nornir_napalm
  - scrapli            – uses nornir_scrapli + Genie (preferred) / TextFSM (fallback)
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from nornir.core.exceptions import NornirSubTaskError
from nornir.core.task import Task
from nornir_napalm.plugins.tasks import napalm_get

logger = logging.getLogger("discovery.collect")

def _unwrap(exc: Exception) -> Exception:
    """
    NornirSubTaskError stringifies to just "Subtask: napalm_get (failed)".
    Dig out the driver exception it wraps, so failures are diagnosable.
    """
    if isinstance(exc, NornirSubTaskError):
        for result in exc.result:
            if result.exception:
                return result.exception
    return exc


# ── NAPALM collection ────────────────────────────────────────────────


def collect_napalm(
    task: Task,
    getters: list[str],
    max_retries: int = 2,
    retry_delay: int = 5,
    collect_vrfs: bool = True,
) -> dict[str, Any]:
    """
    Collect facts via NAPALM with retry support.
    Retries on failure with a delay between attempts (see settings.yaml).
    """
    hostname = task.host.name
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"[{hostname}] NAPALM collection attempt {attempt}/{max_retries}")
            result = task.run(task=napalm_get, getters=getters)
            napalm_data = result[0].result

            if "facts" not in napalm_data or "interfaces" not in napalm_data:
                raise ValueError("Required NAPALM facts (facts, interfaces) were not collected")

            logger.info(
                f"[{hostname}] NAPALM OK: "
                f"hostname={napalm_data['facts'].get('hostname', '?')}, "
                f"model={napalm_data['facts'].get('model', '?')}, "
                f"{len(napalm_data.get('interfaces', {}))} interfaces, "
                f"{len(napalm_data.get('interfaces_ip', {}))} with IPs, "
                f"{len(napalm_data.get('lldp_neighbors', {}))} LLDP neighbors"
            )

            # Supplement with CDP neighbors via Scrapli (LLDP takes precedence)
            _enrich_with_cdp(task, napalm_data)

            # VRF membership, so IPs land in their real VRF instead of all
            # colliding in the default one.
            if collect_vrfs:
                _collect_network_instances(task, napalm_data)
                _collect_vrf_route_targets(task, napalm_data)

            return napalm_data

        except Exception as e:
            last_error = _unwrap(e)
            logger.warning(
                f"[{hostname}] NAPALM attempt {attempt} failed: "
                f"{type(last_error).__name__}: {last_error}"
            )
            if attempt < max_retries:
                logger.info(f"[{hostname}] Retrying in {retry_delay}s...")
                time.sleep(retry_delay)

    raise RuntimeError(
        f"NAPALM fact collection failed after {max_retries} attempts for {hostname}: {last_error}"
    )


def _collect_network_instances(task: Task, napalm_data: dict[str, Any]) -> None:
    """
    Best-effort VRF membership via NAPALM's get_network_instances, run as its own
    getter call rather than folded into the main one: a driver that doesn't
    implement it (or a box that errors on it) would otherwise fail the whole
    collection. On any failure the key is simply left absent, and downstream every
    IP stays in the default VRF -- the pre-VRF behaviour.
    """
    hostname = task.host.name
    try:
        result = task.run(task=napalm_get, getters=["network_instances"])
        instances = result[0].result.get("network_instances", {}) or {}
        napalm_data["network_instances"] = instances
        logger.info(f"[{hostname}] NAPALM network_instances: {len(instances)} instance(s)")
    except Exception as e:
        logger.debug(f"[{hostname}] network_instances collection skipped: {_unwrap(e)}")


# 'show vrf detail' is the Cisco spelling; IOS-XR needs 'all' to list every VRF
# rather than erroring. A platform absent here reports no targets at all.
_RT_COMMANDS = {
    "ios": "show vrf detail",
    "iosxe": "show vrf detail",
    "nxos": "show vrf detail",
    "iosxr": "show vrf all detail",
    "eos": "show bgp instance vrf all | json",
}

# Header of a per-VRF block: 'VRF CUSTC (VRF Id = 2); default RD 65000:100' on
# IOS/IOS-XE, 'VRF CUSTC; RD 65000:100; VPN ID not set' on IOS-XR. Anchored at
# column 0 so the indented 'VRF label ...' body lines never match, and the name
# must be followed by '(VRF Id' or ';' so IOS-XR's own 'VRF mode: Regular' body
# line is not mistaken for the start of a VRF called "mode:".
_VRF_HEADER_RE = re.compile(r"^VRF\s+(?P<name>[^\s;]+)\s*(?:;|\(VRF Id)")
# 'Import VPN route-target communities' / 'No Export VPN route-target communities'
_RT_SECTION_RE = re.compile(
    r"^\s*(?P<none>No\s+)?(?P<direction>Import|Export)\s+VPN\s+route-target\s+communities",
    re.IGNORECASE,
)
# Targets are listed as 'RT:65000:100', several to a line.
_RT_VALUE_RE = re.compile(r"\bRT:(\S+)")
# The RD on a VRF header: 'VRF X; RD 65000:100; ...' (IOS-XR) or
# 'VRF X (VRF Id = 2); default RD 65000:100; ...' (IOS/IOS-XE).
_VRF_RD_RE = re.compile(r"\bRD\s+(?P<rd>\S+)")
# An interface name inside a 'show vrf detail' Interfaces: block -- starts with a
# letter and carries at least one digit (GigabitEthernet0/0/1/22.99, Et0/1,
# Bundle-Ether1, Loopback0). Filters stray words so the block ends cleanly.
_VRF_MEMBER_RE = re.compile(r"^[A-Za-z][\w./:-]*\d[\w./:-]*$")


def _collect_vrf_route_targets(task: Task, napalm_data: dict[str, Any]) -> None:
    """
    Best-effort import/export route targets per VRF -- NAPALM's
    get_network_instances carries only the RD, not the targets, so they are
    fetched over Scrapli with a per-platform command.

    Cisco (IOS/IOS-XE/NX-OS/IOS-XR) reports them in the 'show vrf detail'
    address-family blocks; Arista EOS keeps them under 'router bgp', so they come
    from 'show bgp instance vrf all | json' instead. A platform with no known
    command, an unparseable reply, or a device with no targets configured simply
    yields nothing and the VRFs are synced without targets -- this never fails
    the collection.

    Result: napalm_data["vrf_route_targets"] =
    {vrf_name: {"import": [...], "export": [...]}}, and (Cisco only)
    napalm_data["vrf_interfaces"] = {vrf_name: [interface, ...]} plus
    napalm_data["vrf_rds"] = {vrf_name: "rd"}, both lifted from the same output.
    """
    from nornir_scrapli.tasks import send_command

    hostname = task.host.name
    platform = (task.host.platform or "").lower()
    command = _RT_COMMANDS.get(platform)
    if not command:
        logger.debug(f"[{hostname}] no route-target command for platform '{platform}'")
        return

    try:
        r = task.run(task=send_command, command=command, strip_prompt=True)
        response = r[0]
    except Exception as e:
        logger.debug(f"[{hostname}] route target collection skipped: {_unwrap(e)}")
        return

    try:
        if platform == "eos":
            rt_map = _parse_route_targets_eos(response.result)
        else:
            rt_map = _parse_route_targets_cisco(response, hostname)
    except Exception as e:
        logger.debug(f"[{hostname}] route targets unparseable from '{command}': {e}")
        return

    napalm_data["vrf_route_targets"] = rt_map
    total = sum(len(v["import"]) + len(v["export"]) for v in rt_map.values())
    logger.info(f"[{hostname}] Route targets: {total} across {len(rt_map)} VRF(s)")

    # VRF membership (which interfaces sit in which VRF) from the SAME output.
    # NAPALM's get_network_instances is the primary source, but its IOS-XR driver
    # returns nothing on the ASR-9906s, so this 'Interfaces:' list is the only way
    # those VRFs -- and their IPs -- reach NetBox instead of all collapsing into
    # the default VRF. EOS membership comes from NAPALM, so skip it there.
    if platform != "eos":
        try:
            vrf_intf_map, vrf_rd_map = _parse_vrf_membership_cisco(response, hostname)
        except Exception as e:
            logger.debug(f"[{hostname}] VRF membership unparseable from '{command}': {e}")
            vrf_intf_map, vrf_rd_map = {}, {}
        if vrf_intf_map:
            napalm_data["vrf_interfaces"] = vrf_intf_map
            napalm_data["vrf_rds"] = vrf_rd_map
            members = sum(len(v) for v in vrf_intf_map.values())
            logger.info(
                f"[{hostname}] VRF membership from '{command}': {members} "
                f"interface(s) across {len(vrf_intf_map)} VRF(s)"
            )


def _parse_route_targets_eos(output: str) -> dict[str, dict[str, list[str]]]:
    """
    Route targets from EOS 'show bgp instance vrf all | json'. They live per
    address family under afiSafiConfig, keyed again by the family that carries
    them (mplsVpnV4u, evpn, ...); NetBox route targets are just names, so every
    family is unioned into one import/export pair per VRF.
    """
    payload = json.loads(output)
    rt_map: dict[str, dict[str, list[str]]] = {}
    for vrf_name, vrf_data in (payload.get("vrfs") or {}).items():
        if not isinstance(vrf_data, dict):
            continue
        imports: set[str] = set()
        exports: set[str] = set()
        for af_data in (vrf_data.get("afiSafiConfig") or {}).values():
            if not isinstance(af_data, dict):
                continue
            for group in (af_data.get("routeTargetImports") or {}).values():
                imports.update(str(rt) for rt in group or [])
            for group in (af_data.get("routeTargetExports") or {}).values():
                exports.update(str(rt) for rt in group or [])
        if imports or exports:
            rt_map[vrf_name] = {"import": sorted(imports), "export": sorted(exports)}
    return rt_map


def _parse_route_targets_cisco(response: Any, hostname: str) -> dict[str, dict[str, list[str]]]:
    """
    Route targets from a Cisco 'show vrf detail'. Genie is used when it is
    installed (it is an optional, very heavy dependency), and the raw text is
    parsed otherwise -- so this works on a plain install rather than silently
    reporting no targets.
    """
    parsed, parser = _parse_response(response, "show vrf detail", hostname)
    if parser == "genie" and isinstance(parsed, dict):
        return _parse_route_targets_genie(parsed)
    return _parse_route_targets_cisco_text(parsed if isinstance(parsed, str) else response.result)


def _parse_vrf_membership_cisco(
    response: Any, hostname: str
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """
    Interface membership and RD per VRF from a Cisco 'show vrf detail', so a
    device whose NAPALM driver reports no network_instances (IOS-XR ASR-9906)
    still lands its interfaces in the right VRF. Genie's structured output is
    used when installed, the raw text otherwise -- mirroring the route-target
    path. Returns ({vrf: [interfaces]}, {vrf: rd}).
    """
    parsed, parser = _parse_response(response, "show vrf detail", hostname)
    if parser == "genie" and isinstance(parsed, dict):
        return _parse_vrf_membership_genie(parsed)
    return _parse_vrf_membership_cisco_text(
        parsed if isinstance(parsed, str) else response.result
    )


def _parse_vrf_membership_genie(
    parsed: dict,
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Interface membership + RD out of Genie's 'show vrf detail' structure."""
    intf_map: dict[str, list[str]] = {}
    rd_map: dict[str, str] = {}
    for vrf_name, vrf_data in parsed.items():
        if not isinstance(vrf_data, dict):
            continue
        members = [str(i) for i in (vrf_data.get("interfaces") or [])]
        if members:
            intf_map[vrf_name] = members
        rd = vrf_data.get("route_distinguisher") or ""
        if isinstance(rd, str) and ":" in rd:
            rd_map[vrf_name] = rd
    return intf_map, rd_map


def _parse_vrf_membership_cisco_text(
    output: str,
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """
    Interface membership + RD out of raw 'show vrf detail' text. Each VRF block
    carries an 'Interfaces:' section -- one name per line on IOS-XR, several
    space-separated on IOS/IOS-XE -- that runs until the next 'Address family'
    (or the next VRF); the RD is lifted from the header line.

        VRF CUSTC; RD 65000:100; VPN ID not set
        Interfaces:
          GigabitEthernet0/0/0/1
        Address family IPV4 Unicast
          ...

    A '<not set>' RD (or any value without a ':') is dropped, so a VRF without a
    real distinguisher is not stamped with a fake one.
    """
    intf_map: dict[str, list[str]] = {}
    rd_map: dict[str, str] = {}
    vrf_name: str | None = None
    in_interfaces = False

    for line in (output or "").splitlines():
        header = _VRF_HEADER_RE.match(line)
        if header:
            vrf_name = header.group("name")
            in_interfaces = False
            rd_match = _VRF_RD_RE.search(line)
            if rd_match:
                rd = rd_match.group("rd").rstrip(";")
                if ":" in rd:
                    rd_map[vrf_name] = rd
            continue

        if vrf_name is None:
            continue

        stripped = line.strip()
        if stripped.lower().startswith("interfaces:"):
            in_interfaces = True
            # IOS may list the first interface(s) on the 'Interfaces:' line itself.
            tokens = [t for t in stripped.split(":", 1)[1].split() if _VRF_MEMBER_RE.match(t)]
            if tokens:
                intf_map.setdefault(vrf_name, []).extend(tokens)
            continue

        if in_interfaces:
            tokens = stripped.split()
            if tokens and all(_VRF_MEMBER_RE.match(t) for t in tokens):
                intf_map.setdefault(vrf_name, []).extend(tokens)
                continue
            in_interfaces = False  # a non-interface line ends the block

    return {v: ints for v, ints in intf_map.items() if ints}, rd_map


def _parse_route_targets_genie(parsed: dict) -> dict[str, dict[str, list[str]]]:
    """Route targets out of Genie's 'show vrf detail' structure."""
    rt_map: dict[str, dict[str, list[str]]] = {}
    for vrf_name, vrf_data in parsed.items():
        if not isinstance(vrf_data, dict):
            continue
        imports: set[str] = set()
        exports: set[str] = set()
        for af_data in (vrf_data.get("address_family") or {}).values():
            targets = af_data.get("route_targets") if isinstance(af_data, dict) else None
            for rt, rt_data in (targets or {}).items():
                info = rt_data if isinstance(rt_data, dict) else {}
                value = info.get("route_target") or rt
                rt_type = (info.get("rt_type") or "both").lower()
                if rt_type in ("import", "both"):
                    imports.add(value)
                if rt_type in ("export", "both"):
                    exports.add(value)
        if imports or exports:
            rt_map[vrf_name] = {"import": sorted(imports), "export": sorted(exports)}
    return rt_map


def _parse_route_targets_cisco_text(output: str) -> dict[str, dict[str, list[str]]]:
    """
    Route targets out of raw 'show vrf detail' text, e.g.

        VRF CUSTC (VRF Id = 2); default RD 65000:100; default VPNID <not set>
        Address family ipv4 unicast (Table ID = 0x2):
          Export VPN route-target communities
            RT:65000:100                 RT:65000:200
          Import VPN route-target communities
            RT:65000:100

    Targets accumulate across address families, because a NetBox VRF carries one
    import/export set rather than one per family. A 'No Import/Export ...' line
    opens no section, so the RT lines of the neighbouring block are not credited
    to the wrong direction.
    """
    rt_map: dict[str, dict[str, set[str]]] = {}
    vrf_name: str | None = None
    direction: str | None = None

    for line in (output or "").splitlines():
        header = _VRF_HEADER_RE.match(line)
        if header:
            vrf_name, direction = header.group("name"), None
            continue

        section = _RT_SECTION_RE.match(line)
        if section:
            direction = None if section.group("none") else section.group("direction").lower()
            continue

        targets = _RT_VALUE_RE.findall(line)
        if targets and vrf_name and direction:
            entry = rt_map.setdefault(vrf_name, {"import": set(), "export": set()})
            entry[direction].update(targets)
        elif not targets and line.strip():
            # Any other body line ('No import route-map', the next address
            # family, ...) ends the list of targets.
            direction = None

    return {
        name: {"import": sorted(dirs["import"]), "export": sorted(dirs["export"])}
        for name, dirs in rt_map.items()
        if dirs["import"] or dirs["export"]
    }


# Platforms with no CDP implementation at all. Asking them costs a second
# (Scrapli) connection whose only possible outcome is the except branch below.
_NO_CDP_PLATFORMS = {"paloalto"}


def _enrich_with_cdp(task: Task, napalm_data: dict[str, Any]) -> None:
    """
    Supplement NAPALM-collected neighbor data with CDP via Scrapli.
    Runs 'show cdp neighbors detail', parses with Genie/TextFSM, and merges
    any interfaces not already discovered by LLDP into lldp_neighbors /
    lldp_neighbors_detail. LLDP always takes precedence on the same interface.
    Called inline after a successful NAPALM collection.
    """
    from nornir_scrapli.tasks import send_command

    hostname = task.host.name

    if (task.host.platform or "").lower() in _NO_CDP_PLATFORMS:
        logger.debug(f"[{hostname}] CDP enrichment skipped: platform does not run CDP")
        return

    # Not every run asks for the lldp getters; keep CDP working when it didn't.
    lldp = napalm_data.setdefault("lldp_neighbors", {})
    lldp_detail = napalm_data.setdefault("lldp_neighbors_detail", {})

    try:
        r = task.run(task=send_command, command="show cdp neighbors detail", strip_prompt=True)
        parsed, parser = _parse_response(r[0], "show cdp neighbors detail", hostname)
        cdp, cdp_details = _convert_cdp(parsed, parser)

        merged = 0
        for intf, nbrs in cdp.items():
            if intf not in lldp:
                lldp[intf] = nbrs
                lldp_detail[intf] = cdp_details.get(intf, [])
                merged += 1

        logger.info(
            f"[{hostname}] CDP via Scrapli ({parser}): "
            f"{len(cdp)} neighbors found, {merged} merged (not already in LLDP)"
        )
    except Exception as e:
        logger.debug(f"[{hostname}] CDP enrichment skipped: {e}")


# ── Scrapli collection ───────────────────────────────────────────────


def _hostname_from_prompt(task: Task) -> str:
    """
    Hostname from the CLI prompt: 'RP/0/RP0/CPU0:core-rtr01#' → 'core-rtr01'.
    The IOS-XR 'show version' output (and its parsers) carry no hostname.
    """
    try:
        conn = task.host.get_connection("scrapli", task.nornir.config)
        prompt = conn.get_prompt().strip().rstrip("#> ")
        return prompt.rsplit(":", 1)[-1]
    except Exception:
        return ""


def collect_scrapli(task: Task, platform: str) -> dict[str, Any]:
    """
    Collect facts via Scrapli, parsing with Genie (preferred) then TextFSM
    fallback. All output is converted to a NAPALM-compatible dict structure.
    """
    from nornir_scrapli.tasks import send_command

    hostname = task.host.name
    platform_lower = platform.lower()

    logger.info(f"[{hostname}] Collecting via Scrapli")

    napalm_data: dict[str, Any] = {
        "facts": {},
        "interfaces": {},
        "interfaces_ip": {},
        "lldp_neighbors": {},
        "lldp_neighbors_detail": {},
    }

    # ── show version → facts ─────────────────────────────────────────
    try:
        r = task.run(task=send_command, command="show version", strip_prompt=True)
        parsed, parser = _parse_response(r[0], "show version", hostname)
        napalm_data["facts"] = _convert_facts(parsed, parser, platform_lower)
        logger.info(f"[{hostname}] show version parsed via {parser}")
    except Exception as e:
        logger.warning(f"[{hostname}] show version failed: {e}")

    # Processing rejects a device without a hostname, and not every platform's
    # 'show version' reports one (IOS-XR) — recover it from the CLI prompt.
    if not napalm_data["facts"].get("hostname"):
        prompt_hostname = _hostname_from_prompt(task)
        if prompt_hostname:
            napalm_data["facts"]["hostname"] = prompt_hostname
            napalm_data["facts"]["fqdn"] = napalm_data["facts"].get("fqdn") or prompt_hostname
            logger.info(f"[{hostname}] hostname taken from CLI prompt: {prompt_hostname}")

    # ── show interfaces → interfaces ─────────────────────────────────
    show_intf_cmd = "show interface" if "nx" in platform_lower else "show interfaces"
    try:
        r = task.run(task=send_command, command=show_intf_cmd, strip_prompt=True)
        parsed, parser = _parse_response(r[0], show_intf_cmd, hostname)
        napalm_data["interfaces"] = _convert_interfaces(parsed, parser)
        logger.info(f"[{hostname}] {show_intf_cmd} parsed via {parser}: {len(napalm_data['interfaces'])} interfaces")
    except Exception as e:
        logger.warning(f"[{hostname}] {show_intf_cmd} failed: {e}")

    # ── show ip interface → interfaces_ip ────────────────────────────
    show_ip_cmd = "show ip interface" if "nx" not in platform_lower else "show ip interface"
    try:
        r = task.run(task=send_command, command=show_ip_cmd, strip_prompt=True)
        parsed, parser = _parse_response(r[0], show_ip_cmd, hostname)
        napalm_data["interfaces_ip"] = _convert_interfaces_ip(parsed, parser)
        logger.info(f"[{hostname}] {show_ip_cmd} parsed via {parser}: {len(napalm_data['interfaces_ip'])} IPs")
    except Exception as e:
        logger.warning(f"[{hostname}] {show_ip_cmd} failed: {e}")

    # ── show lldp neighbors detail → lldp ────────────────────────────
    try:
        r = task.run(task=send_command, command="show lldp neighbors detail", strip_prompt=True)
        parsed, parser = _parse_response(r[0], "show lldp neighbors detail", hostname)
        lldp, details = _convert_lldp(parsed, parser)
        napalm_data["lldp_neighbors"] = lldp
        napalm_data["lldp_neighbors_detail"] = details
        logger.info(f"[{hostname}] show lldp neighbors detail parsed via {parser}: {len(lldp)} neighbors")
    except Exception as e:
        logger.warning(f"[{hostname}] show lldp neighbors detail failed: {e}")

    # ── show cdp neighbors detail → merged into lldp_neighbors ───────
    try:
        r = task.run(task=send_command, command="show cdp neighbors detail", strip_prompt=True)
        parsed, parser = _parse_response(r[0], "show cdp neighbors detail", hostname)
        cdp, cdp_details = _convert_cdp(parsed, parser)
        # Merge CDP into LLDP dicts (LLDP takes precedence if same interface)
        for intf, nbrs in cdp.items():
            if intf not in napalm_data["lldp_neighbors"]:
                napalm_data["lldp_neighbors"][intf] = nbrs
        for intf, det in cdp_details.items():
            if intf not in napalm_data["lldp_neighbors_detail"]:
                napalm_data["lldp_neighbors_detail"][intf] = det
        logger.info(f"[{hostname}] show cdp neighbors detail parsed via {parser}: {len(cdp)} neighbors")
    except Exception as e:
        logger.warning(f"[{hostname}] show cdp neighbors detail failed: {e}")

    logger.info(
        f"[{hostname}] Scrapli collection done: "
        f"{len(napalm_data['interfaces'])} interfaces, "
        f"{len(napalm_data['interfaces_ip'])} with IPs, "
        f"{len(napalm_data['lldp_neighbors'])} LLDP neighbors"
    )
    return napalm_data


# ── Parser dispatch ──────────────────────────────────────────────────


def _upper_keys(rows: Any) -> Any:
    """
    TextFSM parsers (scrapli's to_dict and Netmiko's use_textfsm) key rows by
    the template value names lowercased; the converters below use the
    uppercase names as written in the ntc-templates files. Normalize up.
    """
    if isinstance(rows, list):
        return [
            {k.upper(): v for k, v in row.items()} if isinstance(row, dict) else row
            for row in rows
        ]
    return rows


def _first(row: dict, *keys: str) -> str:
    """
    First non-empty value among `keys` in a TextFSM row, flattening the
    single-element lists that List-type template values produce. "" if none set.

    ntc-templates name the same field differently per platform (e.g. the model
    is HARDWARE on ios, PLATFORM on nxos, MODEL on eos), so the converters below
    ask for every spelling in priority order rather than one fixed key.
    """
    for key in keys:
        value = row.get(key)
        if isinstance(value, list):
            value = value[0] if value else ""
        if value:
            return value
    return ""


def _parse_response(result: Any, command: str, hostname: str) -> tuple[Any, str]:
    """
    Try Genie first, fall back to TextFSM.
    Returns (parsed_data, parser_name).

    Scrapli resolves the Genie/TextFSM platform from the connection's platform
    (e.g. cisco_iosxr → cisco_xr templates), so no platform is passed here.
    """
    scrapli_resp = getattr(result, "scrapli_response", None)

    # 1. Try Genie
    if scrapli_resp is not None:
        try:
            parsed = scrapli_resp.genie_parse_output()
            if parsed:
                return parsed, "genie"
        except Exception as e:
            logger.debug(f"[{hostname}] Genie failed for '{command}': {e}")

    # 2. Try TextFSM
    if scrapli_resp is not None:
        try:
            parsed = scrapli_resp.textfsm_parse_output()
            if parsed:
                return _upper_keys(parsed), "textfsm"
        except Exception as e:
            logger.debug(f"[{hostname}] TextFSM failed for '{command}': {e}")

    # 3. Return raw string as last resort
    raw = result.result if hasattr(result, "result") else str(result)
    logger.debug(f"[{hostname}] Both parsers failed for '{command}', returning raw output")
    return raw, "raw"


# ── Converters: Genie/TextFSM → NAPALM format ───────────────────────


def _convert_facts(parsed: Any, parser: str, platform: str) -> dict[str, Any]:
    """Convert Genie dict or TextFSM list to NAPALM facts dict."""
    facts: dict[str, Any] = {
        "hostname": "",
        "model": "Unknown",
        "serial_number": "",
        "os_version": "",
        "vendor": "",
        "fqdn": "",
        "uptime": 0,
    }

    if parser == "genie" and isinstance(parsed, dict):
        # Genie: {"version": {...}} for IOS/NX-OS/EOS
        ver = parsed.get("version", parsed)  # EOS uses top-level keys
        facts["hostname"] = ver.get("hostname", "")
        facts["fqdn"] = ver.get("hostname", "")
        facts["os_version"] = (
            ver.get("version", "")
            or ver.get("software_version", "")
            or ver.get("system_version", "")
        )
        # `x or y or z if cond else w` parses as `(x or y or z) if cond else w`,
        # which dropped platform/chassis whenever hardware was a plain string.
        hardware = ver.get("hardware", "")
        if isinstance(hardware, list):
            hardware = hardware[0] if hardware else ""
        facts["model"] = (
            ver.get("platform", "") or ver.get("chassis", "") or hardware or "Unknown"
        )
        serials = ver.get("processor_board_id", "") or ver.get("serial_number", "")
        if isinstance(serials, list):
            serials = serials[0] if serials else ""
        facts["serial_number"] = serials or ""

        # Vendor from platform
        if "ios" in platform or "nx" in platform:
            facts["vendor"] = "Cisco"
        elif "eos" in platform:
            facts["vendor"] = "Arista"
        elif "junos" in platform:
            facts["vendor"] = "Juniper"

    elif parser == "textfsm" and isinstance(parsed, list) and parsed:
        row = parsed[0]
        # Field names vary by platform: hostname HOSTNAME (ios/nxos; Arista's
        # 'show version' has none, so the caller recovers it from the prompt);
        # version VERSION (ios) / OS (nxos) / IMAGE (eos); model HARDWARE (ios/xr)
        # / PLATFORM (nxos) / MODEL (eos); serial SERIAL (ios/nxos) / SERIAL_NUMBER
        # (eos).
        facts["hostname"] = _first(row, "HOSTNAME")
        facts["fqdn"] = facts["hostname"]
        facts["os_version"] = _first(row, "VERSION", "OS", "IMAGE")
        facts["model"] = _first(row, "HARDWARE", "PLATFORM", "MODEL") or "Unknown"
        facts["serial_number"] = _first(row, "SERIAL", "SERIAL_NUMBER")

        if "ios" in platform or "nx" in platform:
            facts["vendor"] = "Cisco"
        elif "eos" in platform:
            facts["vendor"] = "Arista"
        elif "junos" in platform:
            facts["vendor"] = "Juniper"

    return facts


def _convert_interfaces(parsed: Any, parser: str) -> dict[str, Any]:
    """Convert Genie dict or TextFSM list to NAPALM interfaces dict."""
    interfaces: dict[str, Any] = {}

    if parser == "genie" and isinstance(parsed, dict):
        for name, data in parsed.items():
            enabled = data.get("enabled", True)
            oper = data.get("oper_status", data.get("line_protocol", "")).lower() == "up"
            bw = data.get("bandwidth", 0)
            speed = int(bw) // 1000 if bw else 0  # bandwidth is in kbps → convert to Mbps
            interfaces[name] = {
                "is_enabled": enabled,
                "is_up": oper,
                "description": data.get("description", ""),
                "mac_address": data.get("mac_address", data.get("phys_address", "")),
                "mtu": data.get("mtu", 1500),
                "speed": speed,
            }

    elif parser == "textfsm" and isinstance(parsed, list):
        for row in parsed:
            name = row.get("INTERFACE", "")
            if not name:
                continue
            enabled = row.get("LINK_STATUS", "").lower() == "up"
            # cisco_ios calls line protocol PROTOCOL_STATUS; cisco_xr captures
            # it in a value misleadingly named ADMIN_STATE.
            oper = (row.get("PROTOCOL_STATUS") or row.get("ADMIN_STATE") or "").lower() == "up"
            try:
                speed = int(row.get("BANDWIDTH", "0").split()[0]) // 1000
            except (ValueError, IndexError, AttributeError):
                speed = 0
            interfaces[name] = {
                "is_enabled": enabled,
                "is_up": oper,
                "description": row.get("DESCRIPTION", ""),
                # ios/eos/nxos templates report the MAC as MAC_ADDRESS; older
                # ones used ADDRESS/BIA. BIA (burned-in) is the last resort.
                "mac_address": _first(row, "MAC_ADDRESS", "ADDRESS", "BIA"),
                "mtu": int(row.get("MTU", 1500) or 1500),
                "speed": speed,
            }

    return interfaces


def _convert_interfaces_ip(parsed: Any, parser: str) -> dict[str, Any]:
    """Convert Genie dict or TextFSM list to NAPALM interfaces_ip dict."""
    interfaces_ip: dict[str, Any] = {}

    if parser == "genie" and isinstance(parsed, dict):
        # Genie show ip interface: {"GigabitEthernet0/0": {"ipv4": {"10.0.0.1": {"prefix_length": 24}}}}
        for name, data in parsed.items():
            entry: dict[str, Any] = {}
            ipv4 = data.get("ipv4", {})
            if ipv4:
                entry["ipv4"] = {
                    addr: {"prefix_length": info.get("prefix_length", 32)}
                    for addr, info in ipv4.items()
                }
            ipv6 = data.get("ipv6", {})
            if ipv6:
                entry["ipv6"] = {
                    addr: {"prefix_length": info.get("prefix_length", 128)}
                    for addr, info in ipv6.items()
                }
            if entry:
                interfaces_ip[name] = entry

    elif parser == "textfsm" and isinstance(parsed, list):
        for row in parsed:
            name = row.get("INTERFACE", row.get("INTF", ""))
            ip = row.get("IP_ADDRESS", row.get("IPADDR", ""))
            if not name or not ip or ip == "unassigned":
                continue
            # TextFSM typically gives address without prefix - default /32 unless mask given
            mask = row.get("PREFIX_LENGTH", row.get("MASK", ""))
            try:
                prefix_len = int(mask) if mask else 32
            except ValueError:
                prefix_len = 32
            interfaces_ip.setdefault(name, {}).setdefault("ipv4", {})[ip] = {
                "prefix_length": prefix_len
            }

    return interfaces_ip


def _convert_lldp(parsed: Any, parser: str) -> tuple[dict[str, list], dict[str, list]]:
    """Convert Genie dict or TextFSM list to NAPALM lldp_neighbors + lldp_neighbors_detail."""
    neighbors: dict[str, list] = {}
    details: dict[str, list] = {}

    if parser == "genie" and isinstance(parsed, dict):
        # Genie: {"interfaces": {"Gi0/0": {"port_id": {"Gi0/1": {"neighbors": {"R2": {...}}}}}}}
        for local_intf, intf_data in parsed.get("interfaces", {}).items():
            for port_id, port_data in intf_data.get("port_id", {}).items():
                for remote_name, nbr_data in port_data.get("neighbors", {}).items():
                    neighbors.setdefault(local_intf, []).append({
                        "hostname": remote_name,
                        "port": port_id,
                    })
                    details.setdefault(local_intf, []).append({
                        "remote_system_name": remote_name,
                        "remote_port_id": port_id,
                        "remote_system_description": nbr_data.get("system_description", ""),
                        "remote_chassis_id": nbr_data.get("chassis_id", ""),
                        "remote_port_description": nbr_data.get("port_description", ""),
                    })

    elif parser == "textfsm" and isinstance(parsed, list):
        for row in parsed:
            local_intf = row.get("LOCAL_INTERFACE", "")
            # Current ntc-templates (ios/eos/nxos) use NEIGHBOR_NAME and
            # NEIGHBOR_DESCRIPTION; keep NEIGHBOR / SYSTEM_* as older fallbacks.
            remote_host = _first(row, "NEIGHBOR_NAME", "NEIGHBOR", "SYSTEM_NAME")
            remote_port = _first(row, "NEIGHBOR_INTERFACE", "PORT_ID")
            if not local_intf or not remote_host:
                continue
            neighbors.setdefault(local_intf, []).append({
                "hostname": remote_host,
                "port": remote_port,
            })
            details.setdefault(local_intf, []).append({
                "remote_system_name": remote_host,
                "remote_port_id": remote_port,
                "remote_system_description": _first(row, "NEIGHBOR_DESCRIPTION", "SYSTEM_DESCRIPTION"),
                "remote_chassis_id": row.get("CHASSIS_ID", ""),
                "remote_port_description": row.get("PORT_DESCRIPTION", ""),
            })

    return neighbors, details


def _convert_cdp(parsed: Any, parser: str) -> tuple[dict[str, list], dict[str, list]]:
    """Convert Genie dict or TextFSM list to NAPALM-style lldp_neighbors + detail from CDP."""
    neighbors: dict[str, list] = {}
    details: dict[str, list] = {}

    if parser == "genie" and isinstance(parsed, dict):
        # Genie: {"index": {1: {"local_interface": "Gi0/0", "device_id": "R2", "port_id": "Gi0/1", ...}}}
        for entry in parsed.get("index", {}).values():
            local_intf = entry.get("local_interface", "")
            remote_host = entry.get("device_id", "").split(".")[0]  # strip domain
            remote_port = entry.get("port_id", "")
            if not local_intf or not remote_host:
                continue
            neighbors.setdefault(local_intf, []).append({
                "hostname": remote_host,
                "port": remote_port,
            })
            details.setdefault(local_intf, []).append({
                "remote_system_name": remote_host,
                "remote_port_id": remote_port,
                "remote_system_description": entry.get("software_version", ""),
                "remote_chassis_id": entry.get("management_addresses", {}).get("ipv4", ""),
                "remote_port_description": entry.get("interface_addresses", {}).get("ipv4", ""),
            })

    elif parser == "textfsm" and isinstance(parsed, list):
        for row in parsed:
            local_intf = row.get("LOCAL_INTERFACE", "")
            remote_host = _first(
                row, "NEIGHBOR_NAME", "NEIGHBOR", "DESTINATION_HOST"
            ).split(".")[0]
            remote_port = _first(row, "NEIGHBOR_INTERFACE", "REMOTE_PORT")
            if not local_intf or not remote_host:
                continue
            neighbors.setdefault(local_intf, []).append({
                "hostname": remote_host,
                "port": remote_port,
            })
            details.setdefault(local_intf, []).append({
                "remote_system_name": remote_host,
                "remote_port_id": remote_port,
                "remote_system_description": _first(
                    row, "NEIGHBOR_DESCRIPTION", "SOFTWARE_VERSION", "CAPABILITIES"
                ),
                "remote_chassis_id": _first(row, "MGMT_ADDRESS", "MANAGEMENT_IP"),
                "remote_port_description": "",
            })

    return neighbors, details


# ── Save raw data ────────────────────────────────────────────────────


def save_raw_data(hostname: str, napalm_data: dict, output_dir: str = "/tmp/netbox_discovery"):
    """Optionally save collected raw data to JSON for debugging."""
    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)
    filepath = path / f"{hostname}_napalm_raw.json"
    filepath.write_text(json.dumps(napalm_data, indent=2, default=str))
    logger.info(f"[{hostname}] Raw data saved to {filepath}")
