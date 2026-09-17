"""
report.py - Generate discovery reports.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from net2sot.tasks.netbox_sync import SyncStats
from net2sot.tasks.process import ProcessedData

logger = logging.getLogger("discovery.report")


def print_host_report(
    hostname: str,
    data: ProcessedData,
    stats: SyncStats,
    start_time: str,
    settings: dict,
) -> None:
    """Print per-host discovery report matching generate_report.yml output."""
    physical = sum(1 for i in data.interfaces if not i.is_virtual)
    virtual = sum(1 for i in data.interfaces if i.is_virtual)
    enabled = sum(1 for i in data.interfaces if i.enabled)
    ips_with_intf = len(set(ip.interface for ip in data.ip_addresses))

    report = f"""
{'=' * 50}
NETBOX DISCOVERY REPORT - {hostname}
{'=' * 50}
  Device Information:
  ├─ Hostname     : {data.device.hostname}
  ├─ Model        : {data.device.model}
  ├─ Serial       : {data.device.serial_number}
  ├─ OS Version   : {data.device.os_version}
  ├─ Vendor       : {data.device.vendor}
  ├─ FQDN         : {data.device.fqdn}
  └─ Device Type  : {data.device.device_type}

  Discovery Results:
  ├─ Site          : {'Created' if stats.site_created else 'Exists'}
  ├─ Device        : {'Created' if stats.device_created else 'Updated'}
  ├─ Interfaces    : {stats.interfaces_created} created
  ├─ VRFs          : {stats.vrfs_created} synced
  ├─ IP Addresses  : {stats.ip_addresses_created} created, {stats.ip_addresses_deleted} deleted, {stats.ip_addresses_failed} failed
  ├─ Cables        : {stats.cables_created} created, {stats.cables_skipped} skipped
  └─ Primary IPv4  : {data.primary_ipv4 or 'Not set'}

  Interface Breakdown:
  ├─ Total         : {len(data.interfaces)}
  ├─ Physical      : {physical}
  ├─ Virtual       : {virtual}
  ├─ Enabled       : {enabled}
  └─ With IPs      : {ips_with_intf}

  LLDP Neighbors:
  ├─ Discovered    : {len(data.lldp_neighbors)}"""

    for n in data.lldp_neighbors:
        report += f"\n  │  {n.local_interface} → {n.remote_hostname}:{n.remote_interface}"

    if stats.errors:
        report += f"\n\n  Errors ({len(stats.errors)}):"
        for err in stats.errors:
            report += f"\n  ├─ {err}"

    report += f"\n{'=' * 50}"
    logger.info(report)


def print_summary_report(
    host_results: dict[str, tuple[ProcessedData, SyncStats]],
    failed_hosts: dict[str, dict],
    elapsed: float,
) -> None:
    """Print the final aggregate summary for the whole run."""
    total_intf = 0
    total_ips = 0
    total_cables = 0

    for _data, stats in host_results.values():
        total_intf += stats.interfaces_created
        total_ips += stats.ip_addresses_created
        total_cables += stats.cables_created

    print(f"\n{'=' * 60}")
    print("DISCOVERY SUMMARY")
    print("=" * 60)
    print(f"  Hosts processed : {len(host_results) + len(failed_hosts)}")
    print(f"  Successful      : {len(host_results)}")
    print(f"  Failed          : {len(failed_hosts)} {list(failed_hosts) if failed_hosts else ''}")
    print(f"  Interfaces      : {total_intf}")
    print(f"  IP Addresses    : {total_ips}")
    print(f"  Cables (LLDP)   : {total_cables}")
    print(f"  Elapsed time    : {elapsed:.1f}s")
    print("=" * 60)

    if host_results:
        print("\n  Per-host breakdown:")
        for hostname, (_data, stats) in host_results.items():
            print(
                f"    {hostname:20s} | "
                f"intf={stats.interfaces_created:3d} "
                f"ips={stats.ip_addresses_created:3d} "
                f"ipdel={stats.ip_addresses_deleted:2d} "
                f"cables={stats.cables_created:2d} "
                f"errors={len(stats.errors)}"
            )
        print()

    if failed_hosts:
        print("  Failed hosts:")
        for hostname, detail in failed_hosts.items():
            print(f"    {hostname:20s} | {detail.get('reason', 'unknown')}")
        print()


def save_report(
    hostname: str,
    data: ProcessedData,
    stats: SyncStats,
    output_dir: str = "/tmp/netbox_discovery",
) -> None:
    """Save structured report JSON to disk (mirrors save_raw_data + generate_report)."""
    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)

    report = {
        "hostname": hostname,
        "device": data.device.model_dump(),
        "interfaces_count": len(data.interfaces),
        "ip_addresses_count": len(data.ip_addresses),
        "lldp_neighbors_count": len(data.lldp_neighbors),
        "primary_ipv4": data.primary_ipv4,
        "stats": stats.model_dump(),
    }

    filepath = path / f"{hostname}_report.json"
    filepath.write_text(json.dumps(report, indent=2, default=str))
    logger.info(f"[{hostname}] Report saved to {filepath}")


def save_summary_report(
    host_results: dict[str, tuple[ProcessedData, SyncStats]],
    failed_hosts: dict[str, dict],
    elapsed: float,
    output_path: str,
    run_meta: dict | None = None,
) -> None:
    """
    Write the whole run to a single JSON file so results can be diffed and
    verified after the fact. Captures run metadata, aggregate totals, the failed
    hosts (each with its failure reason and, for a partial sync, what did land),
    and a per-host block (discovered device summary + full sync stats).
    """
    totals = {
        "hosts_processed": len(host_results) + len(failed_hosts),
        "successful": len(host_results),
        "failed": len(failed_hosts),
        "interfaces_created": sum(s.interfaces_created for _d, s in host_results.values()),
        "ip_addresses_created": sum(s.ip_addresses_created for _d, s in host_results.values()),
        "ip_addresses_deleted": sum(s.ip_addresses_deleted for _d, s in host_results.values()),
        "ip_addresses_failed": sum(s.ip_addresses_failed for _d, s in host_results.values()),
        "ip_addresses_duplicate": sum(s.ip_addresses_duplicate for _d, s in host_results.values()),
        "cables_created": sum(s.cables_created for _d, s in host_results.values()),
        "elapsed_seconds": round(elapsed, 1),
    }

    hosts = {
        hostname: {
            "device": data.device.model_dump(),
            "primary_ipv4": data.primary_ipv4,
            "interfaces_count": len(data.interfaces),
            "ip_addresses_count": len(data.ip_addresses),
            "lldp_neighbors_count": len(data.lldp_neighbors),
            "vrfs_count": len(data.vrfs),
            "stats": stats.model_dump(),
        }
        for hostname, (data, stats) in host_results.items()
    }

    # A failed host always has a reason; a partial sync (as opposed to a
    # collection failure) also carries the data/stats it managed before failing,
    # so the report shows what did land rather than just the name.
    failed = {}
    for hostname, detail in failed_hosts.items():
        entry: dict = {"reason": detail.get("reason")}
        data = detail.get("data")
        stats = detail.get("stats")
        if data is not None and stats is not None:
            entry.update(
                {
                    "device": data.device.model_dump(),
                    "primary_ipv4": data.primary_ipv4,
                    "interfaces_count": len(data.interfaces),
                    "ip_addresses_count": len(data.ip_addresses),
                    "stats": stats.model_dump(),
                }
            )
        failed[hostname] = entry

    report = {
        "run": run_meta or {},
        "totals": totals,
        "failed_hosts": failed,
        "hosts": hosts,
    }

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str))
    logger.info(f"Run report saved to {path}")

