#!/usr/bin/env python3
"""
Network Discovery to NetBox - Nornir orchestrator

Runs the pipeline over every host in the inventory:
  1. validate    → tasks/validate.py     (settings + NetBox reachability)
  2. collect     → tasks/collect.py      (NAPALM / Scrapli / Netmiko)
  3. process     → tasks/process.py      (normalize into NetBox shapes)
  4. netbox sync → tasks/netbox_sync.py  (site, device, interfaces, IPs, cables)
  5. report      → tasks/report.py       (per-host and aggregate summary)

Usage:
    python discovery.py
    python discovery.py --settings settings.yaml --config config.yaml
    python discovery.py --site-name prod --debug
    python discovery.py --hosts r1 r2 --collector scrapli

    # Import a single new device without editing the inventory files:
    python discovery.py --device-name r9 --device-ip 172.20.20.19 --device-platform ios

    # Place the device in a specific site and role:
    python discovery.py --device-name r9 --device-ip 172.20.20.19 \
        --device-platform ios --site-name lab --device-role core-router

    # Re-discover devices already in NetBox (inventory sourced from NetBox, each
    # reached at its primary IP). Filter by site / location / platform / device;
    # filters combine (AND across kinds, OR within a kind) and imply --from-netbox:
    python discovery.py --from-netbox
    python discovery.py --filter-site emea --filter-platform eos ios
    python discovery.py --filter-device pe-emea-01 pe-emea-02
    python discovery.py --filter-location rack-a1

    # Re-discover into a NetBox branch (netbox-branching plugin) instead of main,
    # so the changes land in the branch to review and merge in NetBox afterwards.
    # The branch must already exist in NetBox:
    python discovery.py --from-netbox --netbox-branch rediscovery
    python discovery.py --filter-site emea --netbox-branch rediscovery

    # Also create LLDP-derived cables (off by default):
    python discovery.py --create-cables

    # Never touch devices already in NetBox (default is to re-type them):
    python discovery.py --no-update-existing

Environment overrides (take precedence over settings.yaml, useful for CI):
    NETBOX_URL, NETBOX_TOKEN, NETBOX_BRANCH, NETBOX_VALIDATE_CERTS,
    SITE_NAME, TENANT, DEVICE_ROLE, RAW_DATA_PATH,
    CREATE_CABLES, UPDATE_EXISTING, LLDP_ENABLED, SAVE_RAW_DATA, DEBUG,
    DEVICE_USERNAME, DEVICE_PASSWORD  (login credentials for the devices)
"""

import argparse
import ipaddress
import logging
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

# Run as a script from inside net2sot/, this file's own directory is on
# sys.path but the repository root is not -- so the absolute imports below
# ("from net2sot.x import y") only resolve in a checkout that was
# pip-installed. Add the root, so a plain clone still runs. Absolute imports are
# what a plugin in another package uses, and the pipeline has to agree with it:
# importing the same module under two names gives two copies of every schema
# class, and an isinstance check across that boundary silently fails.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml
from nornir import InitNornir
from nornir.core.inventory import Host, ParentGroups
from nornir.core.task import Result, Task

from net2sot.netbox_client import NetboxClient
from net2sot.plugins import (
    CollectContext,
    Collector,
    PluginNotFound,
    Sink,
    SyncContext,
    collector_for_platform,
    collectors,
    describe_plugins,
    sinks,
)
from net2sot.schemas import DiscoveryResult, SyncReport
from net2sot.tasks.collect import save_raw_data
from net2sot.tasks.process import process_facts
from net2sot.tasks.report import (
    print_host_report,
    print_summary_report,
    save_report,
    save_summary_report,
)
from net2sot.tasks.validate import run_validation

logger = logging.getLogger("discovery")

# Auto-named log/report files land here (next to the script's cwd), keeping the
# working directory clean and the per-run artifacts together.
LOG_DIR = "logs"


# ── Logging & run artifacts ───────────────────────────────────

def _auto_path(kind: str, ext: str) -> str:
    """Timestamped path under LOG_DIR, e.g. logs/discovery_run_20260831_140502.log."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return os.path.join(LOG_DIR, f"discovery_{kind}_{ts}.{ext}")


def setup_logging(level: int, log_file: str | None = None) -> None:
    """
    Send logs to the console and, when log_file is given, also to that file so a
    production run leaves an on-disk record. The file is opened in append mode,
    so re-running never truncates an earlier run's log. This runs before
    InitNornir, which detects the handlers we install here and leaves them in
    place rather than configuring its own.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        handlers.append(logging.FileHandler(log_file, mode="a", encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )

    # The SSH/transport libraries log per-connection detail at INFO (paramiko
    # auth banners, scrapli channel I/O and driver setup), which buries
    # discovery's own messages in a production run. Keep them at WARNING unless
    # --debug asked for that detail.
    if level > logging.DEBUG:
        for noisy in ("paramiko", "netmiko", "ncclient"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        # scrapli logs its own parse fallbacks at WARNING: the "optional extra
        # 'genie' is not installed" banner fires on every parse (we try genie
        # before textfsm, and its message opens with a blank line, so only a
        # bare "scrapli:" shows), and "failed to parse with textfsm" fires on
        # output no template matches (e.g. IOS-XR VRFs). Neither is actionable
        # here -- discovery's own "CDP via Scrapli (…)" line and VRF counts
        # already report what landed -- so keep scrapli quiet unless it errors.
        logging.getLogger("scrapli").setLevel(logging.ERROR)


# ── Settings ─────────────────────────────────────────────────────────

# Environment variable → settings key (string values)
ENV_SETTINGS = {
    "NETBOX_URL": "netbox_url",
    "NETBOX_TOKEN": "netbox_token",
    "NETBOX_BRANCH": "netbox_branch",
    "SITE_NAME": "site_name",
    "TENANT": "tenant",
    "DEVICE_ROLE": "device_role",
    "RAW_DATA_PATH": "raw_data_path",
    # Which plugin each end of the pipeline uses. Env-settable like everything
    # else so a CI job can pick them without editing settings.yaml.
    "COLLECTOR": "collector",
    "SINK": "sink",
}

# Environment variable → settings key (boolean values)
ENV_BOOL_SETTINGS = {
    "NETBOX_VALIDATE_CERTS": "netbox_validate_certs",
    "CREATE_CABLES": "create_cables",
    "UPDATE_EXISTING": "update_existing",
    "LLDP_ENABLED": "lldp_enabled",
    "SAVE_RAW_DATA": "save_raw_data",
    "DEBUG": "debug",
}


def _env_bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


# .env sits next to this script; loaded before settings so its values feed the
# NETBOX_* overrides in load_settings. Git-ignored, so secrets stay out of VCS.
ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def load_dotenv_file(path: str = ENV_FILE) -> int:
    """
    Minimal .env loader (no third-party dependency): read KEY=VALUE lines from
    `path` into os.environ without clobbering variables already set in the real
    environment, so shell/CI values still win. Blank lines and #comments are
    ignored, a leading 'export ' is tolerated, and one layer of surrounding
    single/double quotes is stripped. Returns how many variables were set.
    """
    if not os.path.isfile(path):
        return 0
    loaded = 0
    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            key, sep, value = line.partition("=")
            if not sep:
                continue
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value
                loaded += 1
    return loaded


def load_settings(path: str = "settings.yaml") -> dict:
    """Load settings.yaml, then apply environment variable overrides."""
    with open(path) as f:
        settings = yaml.safe_load(f) or {}

    for env_var, key in ENV_SETTINGS.items():
        if os.environ.get(env_var):
            settings[key] = os.environ[env_var]

    for env_var, key in ENV_BOOL_SETTINGS.items():
        if os.environ.get(env_var):
            settings[key] = _env_bool(os.environ[env_var])

    # NETBOX_URL is typed by hand or pasted from a browser, so it often arrives
    # with a trailing slash (and sometimes surrounding whitespace). Normalize
    # once here, after both sources have been applied, so every consumer reads
    # the same clean base URL instead of building ".../" + "/api/".
    if settings.get("netbox_url"):
        settings["netbox_url"] = str(settings["netbox_url"]).strip().rstrip("/")

    return settings


# ── Ad-hoc device injection ──────────────────────────────────────────

def add_ad_hoc_device(nr, name: str, ip: str, platform_group: str) -> None:
    """
    Add a device to the Nornir inventory at runtime, without touching
    inventory/hosts.yaml. The device inherits connection options and
    credentials from the given platform group (ios/eos/iosxr/nxos/paloalto) and
    the inventory defaults.
    """
    inv = nr.inventory
    if platform_group not in inv.groups:
        available = ", ".join(sorted(inv.groups.keys()))
        raise ValueError(
            f"Unknown platform group '{platform_group}' (available: {available})"
        )

    inv.hosts[name] = Host(
        name=name,
        hostname=ip,
        groups=ParentGroups([inv.groups[platform_group]]),
        defaults=inv.defaults,
    )
    logger.info(f"Added ad-hoc device '{name}' ({ip}, group={platform_group})")


# ── Inventory sourced from NetBox ────────────────────────────────────

def load_inventory_from_netbox(
    nr, nb: NetboxClient, filters: dict, settings: dict
) -> None:
    """
    Replace the Nornir inventory's hosts with devices pulled from NetBox, so a
    re-discovery runs against what NetBox already knows instead of
    inventory/hosts.yaml.

    Each device is reached at its NetBox primary IP and slotted into the
    inventory platform group (ios/eos/iosxr/nxos/srlinux/paloalto) matching its NetBox
    platform, so it inherits that group's connection options and collector
    exactly like a statically-defined host -- groups.yaml / defaults.yaml, loaded
    by InitNornir, still supply those. This is why we don't use the stock
    NetBoxInventory2 plugin: it invents platform__<slug> groups that carry none
    of that.

    Skipped, with a warning: devices with no primary IP (unreachable) and devices
    whose platform maps to no inventory group (uncollectable). `filters` go
    straight to the NetBox device API (site/location/platform/name__ie); empty
    values are ignored.
    """
    inv = nr.inventory
    # Optional slug→group override for platforms whose NetBox slug is not already
    # an inventory group name; identity mapping otherwise. discovery.py itself
    # creates platforms as UPPER(group), so "eos"→"eos" needs no override.
    platform_map = settings.get("netbox_platform_map", {})

    devices = nb.get_devices(**filters)

    new_hosts: dict[str, Host] = {}
    skipped: list[str] = []
    for dev in devices:
        name = dev.name or str(dev.id)

        primary = getattr(dev, "primary_ip", None)
        ip = primary.address.split("/")[0] if primary else None
        if not ip:
            skipped.append(f"{name} (no primary IP)")
            continue

        platform = getattr(dev, "platform", None)
        platform_slug = getattr(platform, "slug", None) if platform else None
        group_name = platform_map.get(platform_slug, platform_slug)
        if not group_name or group_name not in inv.groups:
            skipped.append(f"{name} (platform '{platform_slug}' → no inventory group)")
            continue

        new_hosts[name] = Host(
            name=name,
            hostname=ip,
            # No platform= here: it is inherited from the group, same as a
            # statically-defined host and add_ad_hoc_device.
            groups=ParentGroups([inv.groups[group_name]]),
            defaults=inv.defaults,
        )

    # Swap in place so inv.hosts stays a Hosts instance (nr.filter etc. rely on
    # it); replacing static hosts entirely is the point of --from-netbox.
    inv.hosts.clear()
    inv.hosts.update(new_hosts)

    logger.info(
        f"Loaded {len(new_hosts)} device(s) from NetBox"
        + (f"; skipped {len(skipped)}: {', '.join(skipped)}" if skipped else "")
    )

# ── Per-host discovery task (runs inside Nornir) ─────────────────────

# Platforms whose own interface names are the real identifiers, stored verbatim
# rather than canonicalized into the Cisco spelling: Linux kernel names (eth0,
# ens1f0, bond0), PAN-OS names (ethernet1/1, ae1.100, loopback.1) and BIG-IP
# names (1.1, mgmt, and the VLANs) all name interfaces that no rewritten form
# would match back on the device -- "mgmt" in particular would be canonicalized
# into a "Management" port the appliance does not have.
VERBATIM_INTERFACE_PLATFORMS = {"linux", "paloalto", "f5"}


def resolve_collector_name(host, settings: dict) -> str:
    """
    Which collector backend should talk to this host.

    In order:

      1. The host's own ``collector`` (inherited from its groups). A group may
         pin the only backend that can reach its platform -- SR Linux has no
         NAPALM driver -- so the inventory always wins. That is what lets one
         pass discover a mixed-vendor inventory.
      2. The run's collector (--collector, or settings.yaml), if it claims this
         platform.
      3. Any installed collector that claims this platform. This is what makes
         a plugin work on installation alone: pip install the thing, put a host
         on its platform in the inventory, and neither settings.yaml nor
         groups.yaml has to learn its name.
      4. The run's collector anyway, so the failure comes from the backend --
         which can say what it does support -- rather than from here.
    """
    pinned = host.get("collector")
    if pinned:
        return str(pinned)

    platform = (host.platform or "").lower()
    configured = settings.get("collector", "napalm")
    try:
        if collectors.get(configured).supports(platform):
            return configured
    except PluginNotFound:
        # An unknown name in settings is reported when the run builds its
        # collectors, with the full list of what is installed.
        return configured

    claimed = collector_for_platform(platform)
    if claimed is not None:
        logger.info(
            f"[{host.name}] '{configured}' does not support platform '{platform}'; "
            f"using '{claimed.name}', which claims it"
        )
        return claimed.name

    return configured


def _resolve_login_ip(hostname: str | None) -> str | None:
    """
    The IPv4 address the device was actually reached at, for the primary-IP
    fallback in process.py. An inventory hostname that is already an IP is
    returned unchanged; a DNS name (as the Linux server inventory uses) is
    resolved so it can be matched against the interface addresses collected from
    the device. An unresolvable name yields None, disabling that fallback.
    """
    if not hostname:
        return None
    try:
        ipaddress.ip_address(hostname)
        return hostname
    except ValueError:
        pass
    try:
        return socket.gethostbyname(hostname)
    except OSError as e:
        logger.warning(f"Could not resolve '{hostname}' to an IP: {e}")
        return None


def discover_device(
    task: Task,
    settings: dict,
    sink: Sink,
    run_collectors: dict[str, Collector],
) -> Result:
    """
    Full discovery pipeline for a single device: collect → process → sync.

    Neither half is hard-wired. `run_collectors` holds one instance of each
    backend this run needs, keyed by name (built once in main(), because a
    collector is constructed per run and called per device), and `sink` is
    whichever source of truth the run writes to. What is left here is the part
    that is neither vendor- nor target-specific.
    """
    host = task.host
    hostname = host.name.upper()
    platform = host.platform or "unknown"
    start_time = datetime.now(timezone.utc).isoformat()
    collector_name = resolve_collector_name(host, settings)
    debug = settings.get("debug", False)

    # ── 1. Collect facts (collect_facts.yml) ─────────────────────────
    # run_collectors was built from the same resolution over the same inventory,
    # so a miss here means the inventory changed underneath the run. Name it
    # rather than letting a bare KeyError read as a collection failure.
    collector = run_collectors.get(collector_name)
    if collector is None:
        msg = (
            f"[{hostname}] No collector '{collector_name}' was built for this run "
            f"(have: {', '.join(run_collectors) or 'none'})"
        )
        logger.error(msg)
        return Result(host=host, result={"error": msg}, failed=True)

    try:
        facts = collector.collect(
            CollectContext(task=task, platform=platform, settings=settings)
        )
    except Exception as e:
        msg = f"[{hostname}] Collection failed: {e}"
        logger.error(msg)
        # Tell the sink this device did not answer, so a device we could not
        # gather from is visible there and not only in the run log. Looked up by
        # inventory name (which matches the NetBox name on a --from-netbox
        # re-discovery); sinks that have nowhere to record it do nothing.
        try:
            sink.mark_unreachable(host.name)
        except Exception as mark_error:
            logger.warning(f"[{hostname}] Could not flag device as unreachable: {mark_error}")
        return Result(host=host, result={"error": msg}, failed=True)

    # Optionally save raw data for debugging
    if settings.get("save_raw_data", False):
        save_raw_data(
            hostname,
            facts.to_napalm(),
            output_dir=settings.get("raw_data_path", "/tmp/netbox_discovery"),
        )

    # ── 2. Process & normalize data (process_data.yml) ───────────────
    data: DiscoveryResult = process_facts(
        facts,
        platform=platform,
        interface_filters=settings.get("interface_filters", {}),
        device_type_mapping=settings.get("device_type_mapping", {}),
        exclude_disabled=settings.get("interface_filters", {}).get("exclude_disabled", True),
        # The address Nornir connected to. Becomes the primary IP when the
        # device has no management interface. Resolved to an IP so a DNS-named
        # host (the Linux inventory) still matches its interface addresses.
        login_ip=_resolve_login_ip(host.hostname),
        # See VERBATIM_INTERFACE_PLATFORMS.
        normalize_names=platform.lower() not in VERBATIM_INTERFACE_PLATFORMS,
        collector=collector_name,
    )

    # Validate we got usable data
    if not data.is_usable():
        msg = f"[{hostname}] Processing produced no usable data (no hostname or interfaces)"
        logger.error(msg)
        return Result(host=host, result={"error": msg}, failed=True)

    # ── 3. Sync to the source of truth ───────────────────────────────
    stats: SyncReport = sink.sync(
        SyncContext(result=data, settings=settings, start_time=start_time)
    )

    # We reached this device and gathered its facts, so it is reachable. For
    # NetBox that recovers it to "active", but only from the down states
    # discovery itself sets -- a hand-set staged/planned/decommissioning status
    # is preserved. Mirrors the mark_unreachable() above.
    try:
        sink.mark_reachable(data.device_name)
    except Exception as mark_error:
        logger.warning(f"[{hostname}] Could not flag device as reachable: {mark_error}")

    # ── 4. Report (generate_report.yml) ──────────────────────────────
    if debug:
        print_host_report(hostname, data, stats, start_time, settings)

    if settings.get("save_raw_data", False):
        save_report(
            hostname, data, stats,
            output_dir=settings.get("raw_data_path", "/tmp/netbox_discovery"),
        )

    # ── 5. Cleanup (cleanup.yml) ─────────────────────────────────────
    # Nothing sensitive to clear in Python; garbage collection handles it.

    logger.info(
        f"[{hostname}] Discovery complete: "
        f"{stats.interfaces_created} interfaces, "
        f"{stats.ip_addresses_created} IPs, "
        f"{stats.cables_created} cables"
    )

    # A device whose facts were collected fine but only partly landed in NetBox
    # is not a success: reporting it as one is how a NetBox overloaded into 503s
    # produces silently incomplete data behind a green run and a zero exit code.
    if not stats.succeeded:
        logger.error(
            f"[{hostname}] Sync incomplete: {len(stats.errors)} object(s) failed, "
            f"{stats.ip_addresses_failed} IP(s) not written"
        )
        return Result(host=host, result={"data": data, "stats": stats}, failed=True)

    # A duplicate IP is a data condition on the device (the same address
    # configured in two places, or already owned in that VRF), not a sync
    # failure: surface it, but let the run stay green.
    if stats.ip_addresses_duplicate:
        logger.warning(
            f"[{hostname}] {stats.ip_addresses_duplicate} duplicate IP(s) skipped "
            f"(already present in their VRF); device otherwise synced"
        )

    return Result(host=host, result={"data": data, "stats": stats})


def _failure_reason(result: object) -> str:
    """
    A one-line reason for a failed host, so the run log stays readable. A
    collection/processing failure carries {"error": msg}; a partial sync carries
    {"data", "stats"} -- summarize the stat counts (and any object errors)
    rather than dumping the whole DiscoveryResult (every interface and IP) into
    the log, as the raw result dict otherwise would.
    """
    if isinstance(result, dict):
        if "error" in result:
            return str(result["error"])
        stats = result.get("stats")
        if stats is not None:
            return stats.failure_summary()
    return str(result)


# ── Main ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Network discovery to NetBox via Nornir + NAPALM/Scrapli"
    )
    parser.add_argument("--config", default="config.yaml", help="Nornir config file")
    parser.add_argument("--settings", default="settings.yaml", help="Discovery settings file")
    parser.add_argument("--site-name", help="Override site name")
    parser.add_argument(
        "--device-role",
        help="Override the NetBox device role assigned to discovered devices "
        "(created in NetBox if it doesn't exist)",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--hosts", nargs="*", help="Limit to specific hosts")
    parser.add_argument(
        "--collector",
        default=None,
        # No fixed choices: an installed plugin's name is as valid as a built-in
        # one, and hard-coding the list here would be one more place a plugin
        # author has to be let in by hand. An unknown name is rejected when the
        # run builds its collectors, with the installed list in the message.
        help="Collection backend: napalm, scrapli, netmiko, or an installed "
        "plugin (see --list-plugins). Default: settings.yaml, or napalm. "
        "Groups that pin one in the inventory (srlinux) ignore this.",
    )
    parser.add_argument(
        "--sink",
        default=None,
        help="Where to write the discovered data: netbox, or an installed "
        "plugin (see --list-plugins). Default: settings.yaml, or netbox.",
    )
    parser.add_argument(
        "--list-plugins",
        action="store_true",
        help="List the installed collectors and sinks, then exit. Answers "
        "'is my plugin actually installed' without starting a run.",
    )
    parser.add_argument("--save-raw", action="store_true", help="Save raw data to disk")
    parser.add_argument(
        "--log-file",
        nargs="?",
        const="AUTO",
        metavar="PATH",
        help="Also write logs to a file (in addition to the console). Give a path, "
        "or pass the flag alone to auto-name logs/discovery_run_<timestamp>.log. "
        "Opened in append mode, so re-runs never truncate an earlier log. Also "
        "settable with LOG_FILE (use LOG_FILE=AUTO to auto-name).",
    )
    parser.add_argument(
        "--output-file", "-o",
        nargs="?",
        const="AUTO",
        metavar="PATH",
        help="Write the run result to a JSON file for verifying results and "
        "behaviour: run metadata, aggregate totals, failed hosts, and a per-host "
        "device summary + sync stats. Give a path, or pass the flag alone to "
        "auto-name logs/discovery_report_<timestamp>.json. Also settable with "
        "OUTPUT_FILE (use OUTPUT_FILE=AUTO to auto-name).",
    )
    parser.add_argument(
        "--create-cables",
        action="store_true",
        help="Create LLDP-derived cables in NetBox (off by default; a neighbour "
        "whose device is not already in NetBox is skipped, not cabled)",
    )
    parser.add_argument(
        "--no-update-existing",
        action="store_true",
        help="Do not re-type devices already in NetBox. By default a device "
        "whose NetBox device type differs from the one discovery resolved is "
        "re-pointed at the resolved type (device_type only; nothing else on an "
        "existing device is touched)",
    )

    # Ad-hoc device: import a single new device without editing hosts.yaml
    parser.add_argument("--device-name", help="Name of a new device to import ad-hoc")
    parser.add_argument("--device-ip", help="Management IP/hostname of the ad-hoc device")
    parser.add_argument(
        "--device-platform",
        help="Inventory platform group for the ad-hoc device (e.g. ios, eos, iosxr, nxos, linux, paloalto)",
    )

    # Inventory sourced from NetBox: re-discover devices already in NetBox,
    # reached at their primary IP, instead of reading inventory/hosts.yaml.
    parser.add_argument(
        "--from-netbox",
        action="store_true",
        help="Source the inventory from NetBox (re-discovery) instead of "
        "inventory/hosts.yaml. Implied when any --filter-* is given.",
    )
    parser.add_argument(
        "--filter-site", nargs="*", metavar="SLUG",
        help="With --from-netbox: only devices in these site(s)",
    )
    parser.add_argument(
        "--filter-location", nargs="*", metavar="SLUG",
        help="With --from-netbox: only devices in these location(s)",
    )
    parser.add_argument(
        "--filter-platform", nargs="*", metavar="SLUG",
        help="With --from-netbox: only devices with these platform(s)",
    )
    parser.add_argument(
        "--filter-device", nargs="*", metavar="NAME",
        help="With --from-netbox: only these device name(s) (case-insensitive)",
    )

    # NetBox branch: apply the whole run (reads and writes) inside a NetBox
    # branch instead of on main. Pairs naturally with --from-netbox to
    # re-discover into a branch you review and merge afterwards.
    parser.add_argument(
        "--netbox-branch", metavar="NAME",
        help="Scope all NetBox reads and writes to this NetBox branch "
        "(netbox-branching plugin) instead of committing directly to main. The "
        "branch must already exist in NetBox; review and merge it there once the "
        "run looks right. Works with any run mode, but is meant for --from-netbox "
        "re-discovery.",
    )
    args = parser.parse_args()

    if args.list_plugins:
        print(describe_plugins())
        return 0

    if args.device_name and not (args.device_ip and args.device_platform):
        parser.error("--device-name requires --device-ip and --device-platform")

    # Any --filter-* only makes sense against a NetBox-sourced inventory, so
    # treat it as implying --from-netbox rather than silently ignoring it.
    netbox_filters = {
        "site": args.filter_site,
        "location": args.filter_location,
        "platform": args.filter_platform,
        # Case-insensitive exact match: discovery stores names upper-cased, but a
        # user re-discovering will just as likely type them lower-cased.
        "name__ie": args.filter_device,
    }
    if any(netbox_filters.values()):
        args.from_netbox = True

    if args.from_netbox and args.device_name:
        parser.error("--from-netbox cannot be combined with --device-name (ad-hoc import)")
    if args.from_netbox and args.hosts:
        parser.error("--from-netbox cannot be combined with --hosts; use --filter-device instead")

    # ── Load .env ────────────────────────────────────────────────────
    # .env (git-ignored) keeps secrets like NETBOX_TOKEN out of settings.yaml
    # and out of version control. Loaded here, before logging and settings, so
    # its values feed the LOG_FILE / OUTPUT_FILE fallbacks just below and the
    # NETBOX_URL / NETBOX_TOKEN (and other) overrides in load_settings; a real
    # environment variable still takes precedence over the file.
    env_loaded = load_dotenv_file()

    # ── Logging ──────────────────────────────────────────────────────
    # Precedence: CLI flag → LOG_FILE / OUTPUT_FILE env var (CI parity). A value
    # of "AUTO" (or passing the flag alone) auto-names the file under logs/.
    if args.log_file is None and os.environ.get("LOG_FILE"):
        args.log_file = os.environ["LOG_FILE"]
    if args.output_file is None and os.environ.get("OUTPUT_FILE"):
        args.output_file = os.environ["OUTPUT_FILE"]
    if args.log_file == "AUTO":
        args.log_file = _auto_path("run", "log")
    if args.output_file == "AUTO":
        args.output_file = _auto_path("report", "json")

    log_level = logging.DEBUG if args.debug else logging.INFO
    setup_logging(log_level, args.log_file)
    if env_loaded:
        logger.debug(f"Loaded {env_loaded} variable(s) from {ENV_FILE}")
    if args.log_file:
        logger.info(f"Logging to {args.log_file}")

    # ── Load settings ────────────────────────────────────────────────
    settings = load_settings(args.settings)
    if args.site_name:
        settings["site_name"] = args.site_name
    if args.device_role:
        settings["device_role"] = args.device_role
    if args.debug:
        settings["debug"] = True
    if args.save_raw:
        settings["save_raw_data"] = True
    if args.create_cables:
        settings["create_cables"] = True
    if args.no_update_existing:
        settings["update_existing"] = False
    if args.collector:
        settings["collector"] = args.collector
    if args.sink:
        settings["sink"] = args.sink
    if args.netbox_branch:
        settings["netbox_branch"] = args.netbox_branch
    settings.setdefault("collector", "napalm")
    settings.setdefault("sink", "netbox")
    settings.setdefault("create_cables", False)
    settings.setdefault("update_existing", True)
    # A NetBox-sourced inventory already has each device (and its site) in
    # NetBox, so sync_to_netbox reuses that site instead of ensuring site_name.
    settings["from_netbox"] = args.from_netbox

    # ── Initialize Nornir ────────────────────────────────────────────
    nr = InitNornir(config_file=args.config)

    # Device credentials from environment (override inventory defaults)
    if os.environ.get("DEVICE_USERNAME"):
        nr.inventory.defaults.username = os.environ["DEVICE_USERNAME"]
    if os.environ.get("DEVICE_PASSWORD"):
        nr.inventory.defaults.password = os.environ["DEVICE_PASSWORD"]

    # ── Open the sink ────────────────────────────────────────────────
    # Opened before validation because --from-netbox sources the inventory from
    # it; it is also what the run pushes results through. A NetBox sink resolves
    # and activates any requested branch as it connects, so a bad branch name
    # fails here, before anything is collected.
    sink_name = settings.get("sink", "netbox")
    # How many devices are synced at once. Any sink needs this to size its own
    # connection pool, so it goes in settings rather than being handed to one
    # sink as a special case.
    settings["num_workers"] = nr.config.runner.options.get("num_workers", 10)
    try:
        sink = sinks.create(sink_name, settings)
        sink.open()
    except PluginNotFound as e:
        logger.error(str(e))
        sys.exit(1)
    except (ValueError, RuntimeError, KeyError) as e:
        logger.error(f"Could not open the '{sink_name}' sink: {e}")
        sys.exit(1)

    # ── Inventory source / scope ─────────────────────────────────────
    if args.from_netbox:
        load_inventory_from_netbox(nr, sink.client, netbox_filters, settings)
        if not nr.inventory.hosts:
            logger.error("No NetBox devices matched the given filters; nothing to do")
            sys.exit(1)
    elif args.device_name:
        # Ad-hoc device: inject it and limit the run to just that device
        add_ad_hoc_device(nr, args.device_name, args.device_ip, args.device_platform)
        nr = nr.filter(filter_func=lambda h: h.name == args.device_name)
    elif args.hosts:
        nr = nr.filter(filter_func=lambda h: h.name in args.hosts)

    # ── Build this run's collectors ──────────────────────────────────
    # One instance per backend the inventory actually needs, built here rather
    # than per device: a collector is documented as constructed once per run, so
    # anything expensive and device-independent can live in its __init__. Doing
    # it up front also means an unknown collector name fails before the first
    # SSH session rather than once per host.
    try:
        run_collectors = {
            name: collectors.create(name, settings)
            for name in sorted(
                {resolve_collector_name(h, settings) for h in nr.inventory.hosts.values()}
            )
        }
    except PluginNotFound as e:
        logger.error(str(e))
        sys.exit(1)
    logger.debug(f"Collectors for this run: {', '.join(run_collectors) or 'none'}")

    # ── 1. Validate (validate.yml) ───────────────────────────────────
    if not run_validation(settings, nr):
        sys.exit(1)
    # ── 2-5. Run discovery on all hosts ──────────────────────────────
    logger.info(f"Starting discovery for {len(nr.inventory.hosts)} hosts")
    start = datetime.now(timezone.utc)

    results = nr.run(
        task=discover_device,
        settings=settings,
        sink=sink,
        run_collectors=run_collectors,
    )

    # Branching is specific to the NetBox sink (the netbox-branching plugin);
    # any other sink simply has no branch to report.
    branch_name = getattr(getattr(sink, "client", None), "branch_name", None)

    # ── Aggregate results ────────────────────────────────────────────
    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    host_results: dict[str, tuple[DiscoveryResult, SyncReport]] = {}
    # name → {"reason", and (for a partial sync) "data"/"stats"} so a failed host
    # still appears in the summary and JSON report with what it managed to land,
    # not just as a bare name.
    failed_hosts: dict[str, dict] = {}

    for host_name, multi_result in results.items():
        # multi_result.failed is any(subtask.failed), which trips on failures
        # discover_device already handled: a NAPALM attempt that succeeded on
        # retry, or the optional CDP command on platforms without CDP. Only the
        # main task's result decides whether the host was actually discovered.
        main_result = multi_result[0]
        result = main_result.result
        if main_result.failed:
            reason = _failure_reason(result)
            logger.error(f"[{host_name.upper()}] FAILED: {reason}")
            detail: dict = {"reason": reason}
            # A partial sync carries the data/stats gathered before it failed;
            # a collection/processing failure only carries an error string.
            if isinstance(result, dict) and "data" in result:
                detail["data"] = result["data"]
                detail["stats"] = result["stats"]
            failed_hosts[host_name] = detail
            continue
        if isinstance(result, dict) and "data" in result:
            host_results[host_name] = (result["data"], result["stats"])

    # ── Final summary ────────────────────────────────────────────────
    print_summary_report(host_results, failed_hosts, elapsed)

    # Persist the whole run to a JSON file when asked, so results can be diffed
    # and verified after the fact (the point of a first production run).
    if args.output_file:
        run_meta = {
            "started_at": start.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(elapsed, 1),
            "command": " ".join(sys.argv),
            "from_netbox": args.from_netbox,
            "filters": {k: v for k, v in netbox_filters.items() if v},
            "netbox_url": settings.get("netbox_url"),
            "netbox_branch": branch_name,
            "site_name": settings.get("site_name"),
            "collector": settings.get("collector"),
            "sink": sink.name,
        }
        save_summary_report(
            host_results, failed_hosts, elapsed, args.output_file, run_meta
        )

    if branch_name:
        logger.info(
            f"Changes were written to NetBox branch '{branch_name}' "
            f"(schema_id={sink.client.branch_schema_id}), not to main. Review the diff "
            f"and merge the branch in NetBox to apply them."
        )

    sink.close()

    if failed_hosts:
        sys.exit(1)


if __name__ == "__main__":
    main()
