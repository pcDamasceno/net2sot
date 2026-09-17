"""
validate.py - Validates prerequisites before starting discovery.
"""

from __future__ import annotations

import logging
import re
import sys

from net2sot.netbox_client import NetboxClient

logger = logging.getLogger("discovery.validate")


def validate_settings(settings: dict) -> list[str]:
    """
    Assert all required settings are present and valid.
    Returns a list of error messages (empty = all good).
    """
    errors = []

    # Required keys
    for key in ("netbox_url", "netbox_token"):
        val = settings.get(key, "")
        if not val or val == "changeme":
            errors.append(f"'{key}' is missing or not configured in settings")

    site_name = settings.get("site_name", "")
    if not site_name:
        errors.append("'site_name' is not defined")
    elif not re.match(r"^[a-zA-Z0-9_-]+$", site_name) or len(site_name) > 50:
        errors.append(
            f"site_name '{site_name}' is invalid "
            "(max 50 chars, alphanumeric / underscore / hyphen only)"
        )

    return errors


def validate_inventory(nr) -> list[str]:
    """Check that every host has the minimum required attributes."""
    errors = []
    for name, host in nr.inventory.hosts.items():
        if not host.hostname:
            errors.append(f"Host '{name}' has no hostname/IP defined")
        if not host.username:
            errors.append(f"Host '{name}' has no username defined")
        if not host.password:
            errors.append(f"Host '{name}' has no password defined")
        if not host.platform:
            errors.append(f"Host '{name}' has no platform defined")
    return errors


def validate_netbox_connection(settings: dict) -> bool:
    """Test the NetBox API is reachable."""
    nb = NetboxClient(
        url=settings["netbox_url"],
        token=settings["netbox_token"],
        validate_certs=settings.get("netbox_validate_certs", False),
    )
    return nb.test_connection()


def run_validation(settings: dict, nr, *, exit_on_failure: bool = True) -> bool:
    """
    Full pre-flight validation (mirrors validate.yml).
    Returns True if everything passes.
    """
    logger.info("Running pre-flight validation...")
    all_errors: list[str] = []

    # 1. Settings
    all_errors.extend(validate_settings(settings))

    # 2. Inventory
    all_errors.extend(validate_inventory(nr))

    # 3. NetBox connectivity (only if URL/token look valid)
    if not any("netbox_url" in e or "netbox_token" in e for e in all_errors):
        if not validate_netbox_connection(settings):
            all_errors.append(
                f"Cannot reach NetBox API at {settings['netbox_url']}/api/"
            )

    if all_errors:
        logger.error("Validation failed:")
        for err in all_errors:
            logger.error(f"  - {err}")
        if exit_on_failure:
            sys.exit(1)
        return False

    # Summary
    site_name = settings.get("site_name", "")
    logger.info(
        "Validation passed\n"
        f"  NetBox URL   : {settings['netbox_url']}\n"
        f"  Site name    : {site_name}\n"
        f"  Hosts        : {len(nr.inventory.hosts)}\n"
        f"  NAPALM facts : {', '.join(settings.get('napalm_getters', []))}\n"
        f"  LLDP cables  : {settings.get('lldp_enabled', True)}"
    )
    return True
