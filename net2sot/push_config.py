#!/usr/bin/env python3
"""Push NetBox-rendered device configurations through Nornir and NAPALM."""

import argparse
import logging
import os
import sys
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

from nornir import InitNornir
from nornir.core.task import Result, Task
from nornir_napalm.plugins.tasks import napalm_configure
from nornir_utils.plugins.functions import print_result

from net2sot.discovery import load_dotenv_file, load_inventory_from_netbox, load_settings
from net2sot.netbox_client import NetboxClient

logger = logging.getLogger("push_config")


def push_rendered_config(
    task: Task,
    nb: NetboxClient,
    dry_run: bool,
    replace: bool,
    commit_message: str | None = None,
) -> Result:
    """Fetch one host's rendered config from NetBox and load it with NAPALM."""
    device = nb.get_device(task.host.name)
    if not device:
        message = f"NetBox device '{task.host.name}' was not found"
        logger.error(message)
        return Result(host=task.host, result=message, failed=True)

    try:
        configuration = nb.get_rendered_config(device.id)
    except RuntimeError as e:
        logger.error(f"[{task.host.name}] {e}")
        return Result(host=task.host, result=str(e), failed=True)

    try:
        configure_result = napalm_configure(
            task,
            configuration=configuration,
            dry_run=dry_run,
            replace=replace,
            commit_message=commit_message,
        )
    except Exception as e:
        detail = " ".join(str(e).split()) or type(e).__name__
        message = f"Device connection/configuration failed: {detail}"
        logger.error(f"[{task.host.name}] {message}")
        logger.debug(f"[{task.host.name}] NAPALM traceback", exc_info=True)
        return Result(host=task.host, result=message, failed=True)

    return Result(
        host=task.host,
        result="Configuration preview complete" if dry_run else "Configuration applied",
        changed=configure_result.changed,
        diff=getattr(configure_result, "diff", ""),
        failed=configure_result.failed,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Push configurations rendered by NetBox using Nornir + NAPALM"
    )
    parser.add_argument("--config", default="config.yaml", help="Nornir config file")
    parser.add_argument("--settings", default="settings.yaml", help="Discovery settings file")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "--netbox-branch",
        metavar="NAME",
        help="Render from this NetBox branch instead of main",
    )

    parser.add_argument("--filter-site", nargs="*", metavar="SLUG")
    parser.add_argument("--filter-location", nargs="*", metavar="SLUG")
    parser.add_argument("--filter-platform", nargs="*", metavar="SLUG")
    parser.add_argument("--filter-device", nargs="*", metavar="NAME")
    parser.add_argument(
        "--filter-tag",
        nargs="*",
        metavar="SLUG",
        help="Only devices carrying any of these NetBox tag slugs",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Target every usable NetBox device; required when no filter is supplied",
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="Preview the candidate diff without committing (default)",
    )
    mode.add_argument(
        "--apply",
        dest="dry_run",
        action="store_false",
        help="Commit the candidate configuration to each device",
    )

    strategy = parser.add_mutually_exclusive_group()
    strategy.add_argument(
        "--merge",
        dest="replace",
        action="store_false",
        default=False,
        help="Merge the rendered configuration (default)",
    )
    strategy.add_argument(
        "--replace",
        dest="replace",
        action="store_true",
        help="Replace the device configuration with the rendered configuration",
    )
    parser.add_argument(
        "--commit-message",
        help="Optional commit message for platforms that support it",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    netbox_filters = {
        "site": args.filter_site,
        "location": args.filter_location,
        "platform": args.filter_platform,
        "name__ie": args.filter_device,
        "tag": args.filter_tag,
    }
    if not args.all and not any(netbox_filters.values()):
        parser.error("supply at least one --filter-* option, or use --all explicitly")
    if args.all and any(netbox_filters.values()):
        parser.error("--all cannot be combined with --filter-* options")

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    load_dotenv_file()
    settings = load_settings(args.settings)
    if args.netbox_branch:
        settings["netbox_branch"] = args.netbox_branch

    nr = InitNornir(config_file=args.config)
    if os.environ.get("DEVICE_USERNAME"):
        nr.inventory.defaults.username = os.environ["DEVICE_USERNAME"]
    if os.environ.get("DEVICE_PASSWORD"):
        nr.inventory.defaults.password = os.environ["DEVICE_PASSWORD"]

    try:
        nb = NetboxClient(
            url=settings["netbox_url"],
            token=settings["netbox_token"],
            validate_certs=settings.get("netbox_validate_certs", False),
            branch=settings.get("netbox_branch"),
            pool_size=nr.config.runner.options.get("num_workers", 10),
        )
    except (KeyError, ValueError, RuntimeError) as e:
        logger.error(str(e))
        sys.exit(1)

    load_inventory_from_netbox(nr, nb, netbox_filters, settings)
    if not nr.inventory.hosts:
        logger.error("No usable NetBox devices matched the given filters")
        sys.exit(1)

    action = "PREVIEW" if args.dry_run else "APPLY"
    strategy_name = "replace" if args.replace else "merge"
    logger.info(
        f"{action}: {strategy_name} rendered configuration for "
        f"{len(nr.inventory.hosts)} device(s)"
    )

    results = nr.run(
        task=push_rendered_config,
        nb=nb,
        dry_run=args.dry_run,
        replace=args.replace,
        commit_message=args.commit_message,
    )
    print_result(results)

    failed_hosts = [name for name, result in results.items() if result[0].failed]
    if failed_hosts:
        logger.error(f"Configuration failed for: {', '.join(failed_hosts)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
