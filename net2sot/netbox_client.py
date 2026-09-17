"""
NetBox client - handles all NetBox API interactions via pynetbox.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
from collections.abc import Iterator, Sequence

import pynetbox
import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# Statuses that mean "NetBox is busy, come back later" rather than "your request
# was wrong": 429 rate limit, and the 502/503/504 a load balancer returns when
# the backend worker pool is saturated. Everything else is a real error and must
# surface immediately.
_RETRY_STATUSES = (429, 502, 503, 504)
_RETRY_TOTAL = 5
_RETRY_BACKOFF = 1.0

# Interfaces created per bulk POST. A device with a few hundred ports then costs
# a handful of requests instead of one per port, without building a request body
# big enough for NetBox to reject.
_BULK_CHUNK = 100


def _build_http_session(validate_certs: bool, pool_size: int) -> requests.Session:
    """
    The HTTP session pynetbox makes every call through.

    Discovery drives this from several Nornir workers at once, so a hosted NetBox
    answers a burst of writes with 503s once its worker pool is saturated. Those
    are transient, but pynetbox surfaces them as exceptions that the callers here
    turn into skipped objects -- i.e. silently missing data in an otherwise green
    run. Retrying with backoff is what makes overload cost time instead of data.

    Retries cover POST/PATCH/DELETE too, not just the idempotent methods urllib3
    retries by default: a 5xx from the load balancer means the request never
    reached the application, and NetBox's uniqueness constraints reject a genuine
    duplicate anyway. Jitter keeps workers that were rejected together from
    retrying together.
    """
    session = requests.Session()
    if not validate_certs:
        session.verify = False
        urllib3.disable_warnings()

    retry = Retry(
        total=_RETRY_TOTAL,
        backoff_factor=_RETRY_BACKOFF,
        backoff_jitter=1.0,
        status_forcelist=_RETRY_STATUSES,
        allowed_methods=None,  # None = retry every method, including POST
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    # Size the pool to the worker count so concurrent calls reuse connections
    # instead of urllib3 discarding and rebuilding them under load.
    adapter = HTTPAdapter(
        max_retries=retry, pool_connections=pool_size, pool_maxsize=pool_size
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _chunks(items: Sequence, size: int) -> Iterator[Sequence]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _canonical_cidr(value: str) -> str:
    """
    Normalize an "address/prefix" string to a single canonical spelling so a
    discovered IP and the one stored in NetBox compare equal even when they
    differ only in IPv6 compression (e.g. "2001:db8:0:0::1/64" vs
    "2001:db8::1/64"). Anything unparseable is returned unchanged.
    """
    try:
        iface = ipaddress.ip_interface(value)
    except ValueError:
        return value
    return f"{iface.ip}/{iface.network.prefixlen}"


def slugify(value: str) -> str:
    """
    NetBox slugs accept only letters, digits, hyphens and underscores.

    Model strings reported by real devices do not: "ISR4331/K9" has a slash and
    "7220 IXR-D3L" a space, and NetBox rejects both. Collapse anything invalid
    to a hyphen. Underscores are kept, so a device_type_mapping entry that pins
    "cisco_iol" still resolves to the slug it already has in NetBox.
    """
    slug = re.sub(r"[^a-z0-9_-]+", "-", (value or "").lower()).strip("-")
    return slug or "unknown"


class NetboxClient:
    def __init__(
        self, url: str, token: str, validate_certs: bool = False,
        branch: str | None = None, pool_size: int = 10,
    ):
        self._token = token
        # One client serves every Nornir worker, so shared-object resolution is
        # serialized and memoized here rather than raced -- see _get_or_create.
        self._shared_lock = threading.RLock()
        self._shared_cache: dict[tuple, object] = {}
        self.nb = pynetbox.api(url, token=token)
        # Always replace the session, not just when certs are skipped: the retry
        # and pooling behaviour it carries is what keeps a saturated NetBox from
        # costing us data, and that matters regardless of TLS verification.
        self.nb.http_session = _build_http_session(validate_certs, pool_size)

        # NetBox branching: scope every subsequent API call -- both the reads
        # that source a re-discovery inventory (--from-netbox) and the writes
        # that sync results -- to a branch instead of touching main directly.
        # Resolved against main first (branches are global objects, not
        # per-branch), then pinned as the X-NetBox-Branch header on the shared
        # HTTP session so it rides along on every request pynetbox makes.
        self.branch_name: str | None = branch or None
        self.branch_schema_id: str | None = None
        if self.branch_name:
            self._activate_branch(self.branch_name)

    # ── Branching (netbox-branching plugin) ──────────────────────────

    def _activate_branch(self, name: str) -> None:
        """
        Look up the branch by name and pin its schema_id as the X-NetBox-Branch
        header, so all later reads and writes happen inside the branch. Raises if
        the branch does not exist or the plugin is not installed -- better to
        fail fast than silently write to main.
        """
        branch = self._get_branch(name)
        if not branch:
            raise ValueError(
                f"NetBox branch '{name}' not found. Create it in NetBox "
                f"(Branching plugin) first, or check the name."
            )

        schema_id = branch.get("schema_id")
        if not schema_id:
            raise ValueError(
                f"NetBox branch '{name}' has no schema_id; cannot activate it"
            )

        status = branch.get("status") or {}
        status_value = status.get("value") if isinstance(status, dict) else status
        if status_value and status_value != "ready":
            logger.warning(
                f"NetBox branch '{name}' status is '{status_value}', not 'ready'; "
                "changes may not apply as expected"
            )

        self.branch_schema_id = schema_id
        self.nb.http_session.headers["X-NetBox-Branch"] = schema_id
        logger.info(
            f"NetBox branch '{name}' active (schema_id={schema_id}); all reads and "
            f"writes are scoped to this branch until it is merged in NetBox"
        )

    def _get_branch(self, name: str) -> dict | None:
        """
        Fetch a branch record by exact name from the netbox-branching plugin API.

        Done with a raw authenticated request rather than pynetbox's ORM: it must
        run before the X-NetBox-Branch header is set (branches live outside any
        branch), and pynetbox attaches the auth token per request, not on the
        session, so the header is passed explicitly here.
        """
        url = f"{self.nb.base_url}/plugins/branching/branches/"
        headers = {
            "Authorization": f"Token {self._token}",
            "Accept": "application/json",
        }
        try:
            resp = self.nb.http_session.get(url, params={"name": name}, headers=headers)
            resp.raise_for_status()
        except Exception as e:
            raise RuntimeError(
                f"Failed to query NetBox branches at {url}: {e}. Is the "
                f"netbox-branching plugin installed on this NetBox?"
            ) from e

        results = resp.json().get("results", [])
        # The name filter is exact, but match defensively in case the plugin ever
        # returns near-matches.
        for b in results:
            if b.get("name") == name:
                return b
        return results[0] if results else None

    def test_connection(self) -> bool:
        try:
            self.nb.status()
            logger.info("NetBox API connection successful")
            return True
        except Exception as e:
            logger.error(f"NetBox API connection failed: {e}")
            return False

    def _get_or_create(
        self, endpoint, lookup: dict, payload: dict, label: str,
        conflict_lookup: dict | None = None,
    ) -> object:
        """
        Resolve a shared object, creating it once if NetBox does not have it.

        Serialized across workers, because every host in a run wants the same
        site, tenant, role, platform and VRF at the same moment. NetBox does not
        enforce uniqueness on all of them -- two VRFs may share a name -- so
        without the lock, workers that read "missing" together all create it, and
        every later read then finds several. The cache is the other half: once
        one worker has resolved an object, the rest take it without a request.

        `conflict_lookup` covers the reverse case: an object NetBox *does*
        enforce as unique on some other field (a VRF's route distinguisher), so
        the primary lookup can miss while a create still 400s on that field. When
        given, it is used to find the pre-existing object by that unique field
        and reuse it instead of failing the host.
        """
        key = (endpoint.url, tuple(sorted(lookup.items())))
        with self._shared_lock:
            cached = self._shared_cache.get(key)
            if cached is not None:
                return cached

            obj = self._first(endpoint, lookup)
            if obj is None and conflict_lookup:
                obj = self._first(endpoint, conflict_lookup)
                if obj is not None:
                    logger.warning(
                        f"{label} not found by {lookup}, but {conflict_lookup} "
                        f"already belongs to '{getattr(obj, 'name', obj)}' "
                        f"(id={obj.id}); reusing it."
                    )
            if obj is not None:
                logger.debug(f"{label} already exists (id={obj.id})")
            else:
                try:
                    obj = endpoint.create(**payload)
                except pynetbox.RequestError as e:
                    # Belt and braces for anything that races outside this lock
                    # (another discovery run, a human in the UI): NetBox answers
                    # 400 "already exists", so re-read instead of failing the
                    # host. A unique field other than the lookup (a VRF's RD) can
                    # also 400 without the lookup ever matching, hence the
                    # conflict re-read.
                    obj = self._first(endpoint, lookup)
                    if obj is None and conflict_lookup:
                        obj = self._first(endpoint, conflict_lookup)
                        if obj is not None:
                            logger.warning(
                                f"{label} create hit {e}; matched existing "
                                f"'{getattr(obj, 'name', obj)}' (id={obj.id}) "
                                f"via {conflict_lookup}; reusing it."
                            )
                    if obj is None:
                        raise
                else:
                    logger.info(f"Created {label} (id={obj.id})")

            self._shared_cache[key] = obj
            return obj

    @staticmethod
    def _first(endpoint, lookup: dict) -> object | None:
        """
        The single object matching `lookup`, or None.

        Uses filter() rather than get(): get() raises outright when NetBox holds
        more than one match, which is exactly the state an earlier race leaves
        behind, and failing there would make a cosmetic duplicate permanently
        fatal. Picking the lowest id keeps every worker and every later run on
        the same object.
        """
        matches = list(endpoint.filter(**lookup))
        if not matches:
            return None

        chosen = min(matches, key=lambda o: o.id)
        if len(matches) > 1:
            logger.warning(
                f"NetBox holds {len(matches)} objects matching {lookup}; using "
                f"id={chosen.id}. Remove the duplicates in NetBox."
            )
        return chosen

    # ── Site ──────────────────────────────────────────────────────────

    def ensure_site(self, name: str, tenant: str | None = None) -> object:
        slug = slugify(name)
        # Checked ahead of _get_or_create so an existing site skips the tenant
        # lookup entirely. _first, not get(), for the same reason as there.
        site = self._first(self.nb.dcim.sites, {"slug": slug})
        if site:
            logger.info(f"Site '{name}' already exists (id={site.id})")
            return site

        # Tenancy is optional: with no tenant configured the site is created
        # without one rather than inventing a tenant nobody asked for.
        tenant_obj = self._ensure_tenant(tenant)

        return self._get_or_create(
            self.nb.dcim.sites,
            lookup={"slug": slug},
            payload={
                "name": name,
                "slug": slug,
                "status": "active",
                "tenant": tenant_obj.id if tenant_obj else None,
                "description": f"Auto-discovered site: {name}",
            },
            label=f"site '{name}'",
        )

    # ── Manufacturer ─────────────────────────────────────────────────

    def ensure_manufacturer(self, name: str) -> object:
        slug = slugify(name)
        return self._get_or_create(
            self.nb.dcim.manufacturers,
            lookup={"slug": slug},
            payload={"name": name, "slug": slug},
            label=f"manufacturer '{name}'",
        )

    # ── Device Type ──────────────────────────────────────────────────

    def ensure_device_type(self, model: str, manufacturer_id: int) -> object:
        slug = slugify(model)
        return self._get_or_create(
            self.nb.dcim.device_types,
            lookup={"slug": slug},
            payload={"model": model, "slug": slug, "manufacturer": manufacturer_id},
            label=f"device type '{model}'",
        )

    # ── Device Role ──────────────────────────────────────────────────

    def ensure_device_role(self, name: str) -> object:
        slug = slugify(name)
        return self._get_or_create(
            self.nb.dcim.device_roles,
            lookup={"slug": slug},
            payload={"name": name, "slug": slug},
            label=f"device role '{name}'",
        )

    # ── Platform ─────────────────────────────────────────────────────

    def ensure_platform(self, name: str) -> object:
        slug = slugify(name)
        return self._get_or_create(
            self.nb.dcim.platforms,
            lookup={"slug": slug},
            payload={"name": name, "slug": slug},
            label=f"platform '{name}'",
        )

    # ── Tenant ───────────────────────────────────────────────────────

    def _ensure_tenant(self, name: str | None) -> object | None:
        # No tenant configured is a valid answer, not an error: NetBox tenancy is
        # optional, and a blank name would otherwise create a tenant called "".
        if not name:
            return None
        # Looked up by name, not slug: the uniqueness constraint NetBox enforces
        # is on the name, and an existing tenant's slug may not match the one we
        # would compute (e.g. "NET_OPS" stored as "net_ops", not "net-ops").
        # Keying on slug missed it and then tripped the unique-name constraint
        # on create.
        return self._get_or_create(
            self.nb.tenancy.tenants,
            lookup={"name": name},
            payload={"name": name, "slug": slugify(name.replace("_", "-"))},
            label=f"tenant '{name}'",
        )

    # ── Device ───────────────────────────────────────────────────────

    def ensure_device(
        self, name: str, site_id: int, device_type_id: int,
        device_role_id: int, platform_id: int,
        serial: str = "", comments: str = "",
        update_existing: bool = False,
    ) -> object:
        """
        Create the device, or return the one already in NetBox.

        `update_existing` re-points an existing device at `device_type_id`. It
        defaults to off because this method also creates LLDP placeholder
        devices, and that path passes the *local* device's type -- re-typing a
        real device with it would be actively wrong. Only the caller that
        resolved the type from this device's own facts should opt in.
        """
        device = self._get_or_create(
            self.nb.dcim.devices,
            lookup={"name": name},
            payload={
                "name": name,
                "site": site_id,
                "device_type": device_type_id,
                "role": device_role_id,
                "platform": platform_id,
                "serial": serial,
                "status": "active",
                "comments": comments,
            },
            label=f"device '{name}'",
        )

        if update_existing:
            self._update_existing_device(device, device_type_id, comments)

        return device

    def _update_existing_device(
        self, device, device_type_id: int, comments: str
    ) -> bool:
        """
        Refresh the discovery-owned fields on a device already in NetBox.

        Two fields, both authoritative from this device's own facts:
          - device_type: re-pointed when discovery resolved a different type,
            which is how a device stuck on a stale "cisco-generic" gets fixed.
          - comments: the auto-discovered block (timestamp, OS version, uptime,
            serial). It goes stale between runs, so it is always rewritten to the
            freshly discovered values.

        Serial, platform, role and site are deliberately left alone: they may be
        hand-curated and discovery is not the authority on them. None of this
        runs under --no-update-existing.

        Returns True if NetBox was changed.
        """
        payload: dict = {}

        current_type_id = getattr(device.device_type, "id", None)
        if current_type_id != device_type_id:
            payload["device_type"] = device_type_id

        # comments carry a fresh timestamp every run, so this practically always
        # differs — which is the point: keep the discovered facts current.
        if comments and (device.comments or "") != comments:
            payload["comments"] = comments

        if not payload:
            return False

        try:
            device.update(payload)
        except Exception as e:
            logger.warning(f"Failed to update existing device '{device.name}': {e}")
            return False

        logger.info(
            f"Device '{device.name}': updated {', '.join(payload)}"
        )
        return True

    def set_device_status(
        self, name: str, status: str = "failed",
        only_if_current_in: set[str] | None = None,
    ) -> bool:
        """
        Set the status of a device already in NetBox, looked up by name.

        Discovery owns the reachability side of a device's status: an unreachable
        device is flagged "failed" and a reachable one is recovered to "active".
        `only_if_current_in` guards that so discovery never clobbers a
        deliberately hand-set status -- staged, planned, decommissioning and the
        like: the change is applied only when the device's current status is one
        of the given values (e.g. recover to "active" only from
        "failed"/"offline"). Left None, any current status is overwritten.

        A device not in NetBox is left alone (there is nothing to flag). Returns
        True only when the status was actually changed -- an unchanged or guarded
        status is skipped, so re-runs don't churn the NetBox changelog.
        """
        device = self.nb.dcim.devices.get(name=name)
        if not device:
            logger.debug(f"Cannot set status '{status}' on '{name}': not in NetBox")
            return False
        current = getattr(device.status, "value", device.status)
        if current == status:
            return False
        if only_if_current_in is not None and current not in only_if_current_in:
            logger.debug(
                f"Preserving status '{current}' on '{name}' "
                f"(not setting '{status}': only recovers from {sorted(only_if_current_in)})"
            )
            return False
        try:
            device.update({"status": status})
            logger.info(f"Set status='{status}' on device '{name}' (was '{current}')")
            return True
        except Exception as e:
            logger.warning(f"Failed to set status '{status}' on device '{name}': {e}")
            return False

    # ── Interfaces ───────────────────────────────────────────────────

    def ensure_interfaces(
        self, device_id: int, intfs: list[dict], update_existing: bool = False,
        label: str = "",
    ) -> dict[str, object]:
        """
        Sync a device's whole interface list, and return {name: NetBox record}
        for every one that exists in NetBox afterwards. A name missing from the
        result is one that could not be created, and the caller must treat that
        as an error rather than an empty device.

        This is the bulk counterpart to ensure_interface, and the reason it
        exists: the per-interface version costs a GET and a POST each, so a
        65-port switch alone was ~130 requests, and ten Nornir workers doing that
        at once is what saturates NetBox into answering 503. Here it is one list
        read plus one POST per _BULK_CHUNK interfaces.

        `label` is the device name, carried only so the log lines say which
        device a failure belongs to -- one shared client serves every worker, so
        without it two devices that both have an 'Ethernet1/10' are
        indistinguishable in the log.
        """
        prefix = f"[{label}] " if label else ""
        existing = {i.name: i for i in self.nb.dcim.interfaces.filter(device_id=device_id)}

        synced: dict[str, object] = {}
        to_create: list[dict] = []
        for intf in intfs:
            name = intf["name"]
            if name in existing:
                synced[name] = existing[name]
            else:
                to_create.append(self._interface_payload(device_id, intf))

        for chunk in _chunks(to_create, _BULK_CHUNK):
            for iface in self._create_interfaces(chunk, prefix):
                synced[iface.name] = iface

        # Port descriptions change over time and the device is authoritative for
        # them, so bring existing interfaces in line with what was just
        # discovered (including clearing one the device no longer reports).
        # Gated by update_existing so --no-update-existing stays fully
        # hands-off. Other fields are left untouched.
        if update_existing:
            self._sync_interface_descriptions(
                [
                    (existing[i["name"]], i.get("description", ""))
                    for i in intfs
                    if i["name"] in existing
                ],
                prefix,
            )
            self._sync_interface_vrfs(
                [
                    (existing[i["name"]], i.get("vrf"))
                    for i in intfs
                    if i["name"] in existing
                ],
                prefix,
            )

        return synced

    def _create_interfaces(self, payloads: Sequence[dict], prefix: str = "") -> list:
        """
        Create a batch of interfaces in one POST, falling back to one request per
        interface if the batch is rejected. NetBox applies a bulk create
        atomically, so without the fallback a single unacceptable payload would
        take every other interface on the device down with it.
        """
        if not payloads:
            return []

        try:
            created = self.nb.dcim.interfaces.create(list(payloads))
        except Exception as e:
            logger.warning(
                f"{prefix}Bulk create of {len(payloads)} interface(s) failed ({e}); "
                f"retrying them individually"
            )
            return self._create_interfaces_individually(payloads, prefix)

        # A single-element list still comes back as a list, but normalize in case
        # a future NetBox answers a batch with a bare object.
        if not isinstance(created, list):
            created = [created]
        logger.debug(f"{prefix}Created {len(created)} interface(s) in one request")
        return created

    def _create_interfaces_individually(
        self, payloads: Sequence[dict], prefix: str = ""
    ) -> list:
        created = []
        for payload in payloads:
            try:
                created.append(self.nb.dcim.interfaces.create(**payload))
            except Exception as e:
                logger.warning(
                    f"{prefix}Failed to create interface '{payload['name']}': {e}"
                )
        return created

    def ensure_interface(
        self, device_id: int, intf: dict, update_existing: bool = False
    ) -> object | None:
        """
        Single-interface variant, for the paths that genuinely have only one to
        do (an LLDP neighbour's remote port). Prefer ensure_interfaces whenever a
        whole device's list is in hand.
        """
        name = intf["name"]
        existing = self.nb.dcim.interfaces.get(device_id=device_id, name=name)
        if existing:
            logger.debug(f"Interface '{name}' already exists on device {device_id}")
            if update_existing:
                self._sync_interface_description(existing, intf.get("description", ""))
            return existing

        try:
            iface = self.nb.dcim.interfaces.create(
                **self._interface_payload(device_id, intf)
            )
            logger.debug(f"Created interface '{name}' (id={iface.id})")
            return iface
        except Exception as e:
            logger.warning(f"Failed to create interface '{name}' on device {device_id}: {e}")
            return None

    @staticmethod
    def _interface_payload(device_id: int, intf: dict) -> dict:
        payload = {
            "device": device_id,
            "name": intf["name"],
            "type": intf.get("type", "other"),
            "enabled": intf.get("enabled", True),
        }
        if intf.get("mac_address"):
            payload["mac_address"] = intf["mac_address"]
        if intf.get("mtu"):
            payload["mtu"] = intf["mtu"]
        if intf.get("description"):
            payload["description"] = intf["description"]
        if intf.get("vrf"):
            payload["vrf"] = intf["vrf"]
        # Positive, not merely truthy: NX-OS reports speed -1 on a virtual
        # interface whose speed is meaningless (nve1, the VXLAN tunnel), and
        # NetBox rejects a negative speed outright -- which used to take the
        # whole interface with it. Unknown speed means send no speed.
        if (intf.get("speed") or 0) > 0:
            # Collectors carry speed in Mbps (NAPALM's unit); NetBox's field is
            # Kbps. Passing it straight through reports a 1G port as 1 Mbps.
            payload["speed"] = int(intf["speed"] * 1000)
        return payload

    def _sync_interface_description(self, iface, description: str) -> bool:
        """
        Set an existing interface's description to `description` (the discovered
        value), writing only when it actually differs. An empty `description`
        clears a stale one, since the device reporting no description is itself
        the current truth.

        Returns True if NetBox was changed.
        All interface descriptions are stored in lower case to avoid duplicates and confusion.
        """
        payload = self._description_update(iface, description)
        if payload is None:
            return False

        try:
            iface.update({"description": payload["description"]})
        except Exception as e:
            logger.warning(
                f"Failed to update description on interface '{iface.name}': {e}"
            )
            return False

        logger.debug(f"Updated description on interface '{iface.name}'")
        return True

    def _sync_interface_descriptions(
        self, pairs: Sequence[tuple], prefix: str = ""
    ) -> int:
        """
        Bulk form of _sync_interface_description: one PATCH for every interface
        on the device whose description actually changed, instead of one request
        per interface. `pairs` is (existing NetBox interface, discovered
        description). Returns the number of interfaces written.
        """
        payload = [
            update
            for iface, description in pairs
            if (update := self._description_update(iface, description)) is not None
        ]
        if not payload:
            return 0

        try:
            self.nb.dcim.interfaces.update(payload)
        except Exception as e:
            logger.warning(
                f"{prefix}Failed to update {len(payload)} interface description(s): {e}"
            )
            return 0

        logger.debug(f"{prefix}Updated {len(payload)} interface description(s)")
        return len(payload)

    @staticmethod
    def _description_update(iface, description: str) -> dict | None:
        """
        The PATCH body needed to bring `iface`'s description in line with the
        discovered one, or None when it already matches and no write is needed.
        Descriptions are stored lower case to avoid duplicates and confusion.
        """
        description = (description or "").lower()
        if (iface.description or "") == description:
            return None
        return {"id": iface.id, "description": description}

    def _sync_interface_vrfs(
        self, pairs: Sequence[tuple], prefix: str = ""
    ) -> int:
        """
        Bring existing interfaces' VRF in line with what was discovered: one
        PATCH per interface whose VRF actually changed. `pairs` is (existing
        NetBox interface, discovered VRF id or None -- None means the global
        table). Returns the number of interfaces written.
        """
        payload = [
            update
            for iface, vrf_id in pairs
            if (update := self._vrf_update(iface, vrf_id)) is not None
        ]
        if not payload:
            return 0

        try:
            self.nb.dcim.interfaces.update(payload)
        except Exception as e:
            logger.warning(
                f"{prefix}Failed to update {len(payload)} interface VRF(s): {e}"
            )
            return 0

        logger.debug(f"{prefix}Updated {len(payload)} interface VRF(s)")
        return len(payload)

    @staticmethod
    def _vrf_update(iface, vrf_id: int | None) -> dict | None:
        """
        The PATCH body to set `iface`'s VRF to `vrf_id`, or None when it already
        matches. `vrf_id` None clears the VRF (interface back in the global
        table). iface.vrf is a nested brief or None.
        """
        current = iface.vrf.id if iface.vrf else None
        if current == vrf_id:
            return None
        return {"id": iface.id, "vrf": vrf_id}

    # ── VRF ──────────────────────────────────────────────────────────

    def ensure_route_target(self, name: str) -> object:
        # Route targets are shared, name-unique objects (e.g. "65000:100"), so
        # resolve them the same locked/cached way as everything else.
        return self._get_or_create(
            self.nb.ipam.route_targets,
            lookup={"name": name},
            payload={"name": name},
            label=f"Route target '{name}'",
        )

    def _ensure_route_targets(self, names) -> list[int]:
        ids: list[int] = []
        for name in names or []:
            rt = self.ensure_route_target(name)
            if rt:
                ids.append(rt.id)
        return ids

    def ensure_vrf(
        self, name: str, rd: str | None = None, tenant: str | None = None,
        description: str = "", import_targets=None, export_targets=None,
        update_existing: bool = False,
    ) -> object:
        # Looked up by name only, so a VRF that already exists keeps its
        # hand-set RD/tenant; the extra fields are applied only on creation.
        # NetBox enforces RD as globally unique, so a name that misses can still
        # collide on RD -- conflict_lookup finds and reuses that VRF rather than
        # failing the host on a 400.
        logger.debug(f"Ensuring VRF name={name!r} rd={rd!r}")
        import_ids = self._ensure_route_targets(import_targets)
        export_ids = self._ensure_route_targets(export_targets)
        payload: dict = {"name": name}
        if rd:
            payload["rd"] = rd
        if description:
            payload["description"] = description
        if tenant:
            tenant_obj = self._ensure_tenant(tenant)
            if tenant_obj:
                payload["tenant"] = tenant_obj.id
        # Only stamp targets we actually discovered, so a create is complete but
        # a driver that can't report targets never sends empty lists.
        if import_ids or export_ids:
            payload["import_targets"] = import_ids
            payload["export_targets"] = export_ids
        vrf = self._get_or_create(
            self.nb.ipam.vrfs,
            lookup={"name": name},
            payload=payload,
            label=f"VRF '{name}' (rd={rd or 'none'})",
            conflict_lookup={"rd": rd} if rd else None,
        )
        # _get_or_create never writes to an existing VRF, so bring its route
        # targets in line here -- gated by update_existing (like descriptions)
        # and skipped when nothing was discovered, so hand-set targets survive.
        if update_existing and (import_ids or export_ids):
            self._sync_vrf_route_targets(vrf, import_ids, export_ids)
        return vrf

    @staticmethod
    def _target_ids(targets) -> set[int]:
        """
        The route-target ids on a VRF, whatever shape pynetbox is holding them
        in: nested Records on a freshly read VRF, but bare ids straight after an
        update() writes the request body back onto the record -- and the shared
        cache then hands that same record to the next host in the run.
        """
        ids: set[int] = set()
        for target in targets or []:
            if isinstance(target, int):
                ids.add(target)
            elif isinstance(target, dict):
                if target.get("id") is not None:
                    ids.add(target["id"])
            elif getattr(target, "id", None) is not None:
                ids.add(target.id)
        return ids

    def _sync_vrf_route_targets(
        self, vrf, import_ids: list[int], export_ids: list[int]
    ) -> bool:
        """
        Replace a VRF's import/export route targets with the discovered ones,
        writing only when they differ. Discovery is authoritative here, so this
        is a wholesale set -- a target dropped on the device is dropped in
        NetBox. Returns True if NetBox was changed.
        """
        update: dict = {}
        if self._target_ids(vrf.import_targets) != set(import_ids):
            update["import_targets"] = import_ids
        if self._target_ids(vrf.export_targets) != set(export_ids):
            update["export_targets"] = export_ids
        if not update:
            return False

        try:
            vrf.update(update)
        except Exception as e:
            logger.warning(f"Failed to sync route targets on VRF '{vrf.name}': {e}")
            return False

        logger.info(
            f"Synced route targets on VRF '{vrf.name}' "
            f"(import={len(import_ids)}, export={len(export_ids)})"
        )
        return True

    # ── IP Addresses ─────────────────────────────────────────────────

    def ensure_ip_address(
        self, address: str, interface_id: int, vrf_id: int | None = None,
        status: str = "active", role: str = "", description: str = "",
    ) -> tuple[object | None, str]:
        """
        Create (or find) the IP and return (ip_obj, outcome). outcome is one of
        "exists"/"created" (ip_obj set), "duplicate" (a NetBox 400: the address
        is already owned elsewhere in the VRF -- a data condition on the device,
        not a write failure), or "error" (anything else). The caller uses the
        outcome to decide whether a miss should fail the host.
        """
        params = {"address": address}
        if interface_id:
            params["interface_id"] = interface_id

        existing = self.nb.ipam.ip_addresses.filter(**params)
        for ip in existing:
            if ip.assigned_object_id == interface_id:
                logger.debug(f"IP '{address}' already assigned to interface {interface_id}")
                return ip, "exists"

        payload = {
            "address": address,
            "status": status,
            "assigned_object_type": "dcim.interface",
            "assigned_object_id": interface_id,
        }
        if vrf_id:
            payload["vrf"] = vrf_id
        if role:
            payload["role"] = role
        if description:
            payload["description"] = description

        try:
            ip = self.nb.ipam.ip_addresses.create(**payload)
            logger.debug(f"Created IP '{address}' (id={ip.id})")
            return ip, "created"
        except Exception as e:
            # A duplicate is NetBox rejecting an address already present in the
            # VRF (the device has it configured twice, or it lives on another
            # device): expected data, so flag it apart from a real write error.
            duplicate = "duplicate ip address" in str(e).lower()
            logger.warning(f"Failed to create IP '{address}': {e}")
            return None, "duplicate" if duplicate else "error"

    def set_primary_ip(self, device_id: int, ip_id: int, family: str = "ipv4"):
        device = self.nb.dcim.devices.get(device_id)
        if not device:
            return
        field = "primary_ip4" if family == "ipv4" else "primary_ip6"
        device.update({field: ip_id})
        logger.info(f"Set {field} on device {device.name} to ip_id={ip_id}")

    def prune_device_ips(
        self, device_id: int, keep_by_interface: dict[int, set[str]]
    ) -> int:
        """
        Delete every IP in NetBox that sits on one of this device's synced
        interfaces but was not rediscovered this run. This is what stops a
        changed management/interface IP from leaving its predecessor behind as an
        orphan.

        `keep_by_interface` maps a NetBox interface id to the addresses
        discovered on it. An interface absent from the mapping was not synced
        this run and is left completely alone; one mapped to an empty set had no
        IP reported by the device and so has all of its NetBox IPs removed.

        One query for the whole device, rather than one per interface -- the
        per-interface version was 65 more requests on a 65-port switch, for no
        extra information.

        Returns the number of IPs deleted.
        """
        removed = 0
        keep_canon = {
            intf_id: {_canonical_cidr(a) for a in addresses}
            for intf_id, addresses in keep_by_interface.items()
        }
        # Materialize before deleting: iterating a paginated pynetbox result set
        # while removing from it shifts the offsets and skips records.
        for ip in list(self.nb.ipam.ip_addresses.filter(device_id=device_id)):
            # The filter is device-wide and can be fuzzy, so confirm which
            # interface each IP actually hangs off before deleting anything.
            intf_id = getattr(ip, "assigned_object_id", None)
            if intf_id not in keep_canon:
                continue
            if _canonical_cidr(ip.address) in keep_canon[intf_id]:
                continue
            if self._delete_ip(ip, device_id):
                removed += 1
        return removed

    def _delete_ip(self, ip, device_id: int | None = None) -> bool:
        """
        Delete an IP address object, working around NetBox refusing to delete one
        that is still a device's primary_ip: clear that pointer, then retry once.
        """
        try:
            ip.delete()
            logger.info(f"Deleted stale IP '{ip.address}' (id={ip.id})")
            return True
        except Exception as e:
            if device_id is not None and self._clear_primary_if_matches(device_id, ip.id):
                try:
                    ip.delete()
                    logger.info(f"Deleted stale primary IP '{ip.address}' (id={ip.id})")
                    return True
                except Exception as e2:
                    logger.warning(f"Failed to delete IP '{ip.address}': {e2}")
                    return False
            logger.warning(f"Failed to delete IP '{ip.address}': {e}")
            return False

    def _clear_primary_if_matches(self, device_id: int, ip_id: int) -> bool:
        """
        If `ip_id` is the device's primary_ip4/6, null that field so the IP can
        be deleted. Returns True if a pointer was cleared.
        """
        device = self.nb.dcim.devices.get(device_id)
        if not device:
            return False

        payload: dict = {}
        if getattr(device.primary_ip4, "id", None) == ip_id:
            payload["primary_ip4"] = None
        if getattr(device.primary_ip6, "id", None) == ip_id:
            payload["primary_ip6"] = None
        if not payload:
            return False

        try:
            device.update(payload)
            logger.info(
                f"Cleared {', '.join(payload)} on device '{device.name}' "
                f"to remove stale IP id={ip_id}"
            )
            return True
        except Exception as e:
            logger.warning(f"Failed to clear primary IP on device '{device.name}': {e}")
            return False

    # ── Cables ───────────────────────────────────────────────────────

    def create_cable(
        self, a_intf_id: int, b_intf_id: int,
        cable_type: str = "cat6", status: str = "connected", label: str = "",
    ) -> object | None:
        # Check if cable already exists on either termination
        existing_a = self.nb.dcim.interfaces.get(a_intf_id)
        if existing_a and existing_a.cable:
            logger.debug(f"Cable already exists on interface {a_intf_id}")
            return None

        try:
            cable = self.nb.dcim.cables.create(
                a_terminations=[{"object_type": "dcim.interface", "object_id": a_intf_id}],
                b_terminations=[{"object_type": "dcim.interface", "object_id": b_intf_id}],
                type=cable_type,
                status=status,
                label=label,
            )
            logger.info(f"Created cable between interfaces {a_intf_id} <-> {b_intf_id}")
            return cable
        except Exception as e:
            logger.warning(f"Failed to create cable: {e}")
            return None

    def get_device(self, name: str) -> object | None:
        return self.nb.dcim.devices.get(name=name)

    def get_devices(self, **filters):
        """
        List devices, filtered server-side (site, location, platform, name, …).

        Empty/None filter values are dropped, so `get_devices()` with nothing set
        returns every device. A value may be a single string or a list — NetBox
        treats a repeated query param as OR, so `platform=["ios", "eos"]` matches
        either. Returns a pynetbox RecordSet (iterable of device records).
        """
        active = {k: v for k, v in filters.items() if v}
        if active:
            return self.nb.dcim.devices.filter(**active)
        return self.nb.dcim.devices.all()

    def get_rendered_config(self, device_id: int) -> str:
        """Return the configuration rendered by NetBox for one device."""
        url = f"{self.nb.base_url}/dcim/devices/{device_id}/render-config/"
        headers = {
            "Authorization": f"Token {self._token}",
            "Accept": "application/json",
        }
        try:
            response = self.nb.http_session.post(url, headers=headers)
            response.raise_for_status()
            content = response.json().get("content")
        except Exception as e:
            raise RuntimeError(
                f"Failed to render configuration for NetBox device {device_id}: {e}"
            ) from e

        if not isinstance(content, str) or not content.strip():
            raise RuntimeError(
                f"NetBox returned no rendered configuration for device {device_id}"
            )
        return content

    def get_interface(self, device_id: int, name: str) -> object | None:
        return self.nb.dcim.interfaces.get(device_id=device_id, name=name)

    # ── Custom Fields ────────────────────────────────────

    def ensure_custom_field(
        self, name: str, label: str, cf_type: str,
        object_types: list[str], description: str = "",
    ) -> object | None:
        """
        Ensure a custom-field *definition* exists in NetBox, creating it if it is
        missing. Returns the existing or newly created field, or None if creation
        failed. Idempotent: an already-defined field is returned untouched.
        """
        existing = self.nb.extras.custom_fields.get(name=name)
        if existing:
            return existing
        base = {"name": name, "label": label, "type": cf_type, "description": description}
        # NetBox 4.x calls the model list "object_types"; 3.x used "content_types".
        # Try the modern name first and fall back so this works on either.
        last_error: Exception | None = None
        for key in ("object_types", "content_types"):
            try:
                cf = self.nb.extras.custom_fields.create(**base, **{key: object_types})
                logger.info(f"Created custom field '{name}' ({cf_type}) for {object_types}")
                return cf
            except Exception as e:
                last_error = e
        logger.warning(f"Failed to create custom field '{name}': {last_error}")
        return None

    def set_custom_fields(self, obj, values: dict) -> bool:
        """
        Write custom-field values onto a NetBox object (a device, etc.) in one
        PATCH. `values` maps custom-field name -> value; NetBox merges it with the
        object's other custom fields, so unspecified ones are left alone. The
        caller decides which fields to include (e.g. honoring each field's
        update_existing flag); this just writes what it is given. Returns True on
        success.
        """
        if not values:
            return False
        label = getattr(obj, "name", obj)
        try:
            obj.update({"custom_fields": values})
            logger.info(f"Set custom field(s) {sorted(values)} on '{label}'")
            return True
        except Exception as e:
            logger.warning(f"Failed to set custom fields on '{label}': {e}")
            return False
