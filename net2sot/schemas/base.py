"""
schemas/base.py - Shared base model and field coercions for the discovery
contract.

Everything a plugin exchanges with this project is a Pydantic model rooted
here. Two rules shape the base configuration, and both exist for plugin
authors rather than for us:

  extra="allow"   A vendor knows things we did not model. Rejecting those keys
                  would mean every new platform needs a change to this file
                  before it can carry its own data -- exactly the coupling that
                  makes a plugin system pointless. Undeclared keys survive
                  validation and round-trip through model_dump(); the declared
                  fields are the contract, the rest is payload. Put anything
                  you want a sink to see deliberately in the `custom` dict that
                  the normalized models carry, which is addressable from
                  settings.yaml (custom_fields) instead of being anonymous.

  coercion        CLI output is strings, and a parser that found nothing hands
                  back "" rather than None. A contract that rejected those
                  would push the same five lines of cleanup into every plugin.
                  The annotated types below absorb that at the boundary, so a
                  collector may return speed="1000" or mtu="" and the model
                  still yields int | None.

Only pydantic and the standard library are imported here, deliberately: a
third-party plugin depends on this package to speak the contract, and it should
not drag in napalm, scrapli, pynetbox or Nornir to do so.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict

# Version of the contract in this package. Bumped major on a breaking change to
# a declared field (removal, rename, or a narrowing of its type), minor when
# fields are added. `CollectedFacts.schema_version` stamps it onto collected
# data so a payload archived to JSON (save_raw_data) stays interpretable, and so
# a sink can tell which vocabulary it is being handed.
SCHEMA_VERSION = "1.0"

# What a parser emits when a field was absent. Distinct from a real value: a
# device that reports no serial has no serial, and writing the literal string
# "N/A" into NetBox is worse than writing nothing.
#
# "unknown" is deliberately NOT in here. It is this project's own default for a
# model and a device type ("Unknown", "cisco-generic" territory), so treating it
# as absent would erase those defaults on every round-trip -- and some platforms
# report it as a real value. Emptiness is spelled by the placeholders below.
_UNSET_STRINGS = {"", "n/a", "na", "none", "null", "nil", "-", "--", "not set"}


def _is_unset(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip().lower() in _UNSET_STRINGS)


def _coerce_str(value: Any) -> Any:
    """Absent → "". Anything scalar → its string form, so an int model number
    from a JSON-speaking platform (SR Linux, PAN-OS) lands as text."""
    if _is_unset(value):
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    return value


def _coerce_optional_int(value: Any) -> Any:
    """
    Absent → None. "1000" → 1000. 1000.0 → 1000.

    NAPALM reports speed as a float and several drivers report it in Mbps as a
    string; TextFSM reports every number as a string; a missing value arrives as
    "" or "unknown". None (rather than 0) is what the pipeline treats as "not
    known", so that a 0 does not read as a real zero-speed interface.
    """
    if _is_unset(value):
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        try:
            return int(float(text))
        except ValueError:
            return None
    return value


def _coerce_int(value: Any) -> Any:
    """As _coerce_optional_int, but an absent value is 0 (uptime, counters)."""
    coerced = _coerce_optional_int(value)
    return 0 if coerced is None else coerced


_TRUE_STRINGS = {"true", "yes", "y", "up", "enabled", "enable", "1", "on", "active"}
_FALSE_STRINGS = {"false", "no", "n", "down", "disabled", "disable", "0", "off", "inactive"}


def _coerce_bool(value: Any) -> Any:
    """
    "up"/"enabled"/"yes" → True, "down"/"disabled"/"no" → False.

    Link and admin state reach us as whatever word the platform prints, and
    every collector was otherwise writing its own mapping for it.
    """
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_STRINGS:
            return True
        if text in _FALSE_STRINGS:
            return False
    return value


def _coerce_str_list(value: Any) -> Any:
    """
    A single string → a one-element list.

    TextFSM List-type values come back as lists, their scalar counterparts as
    bare strings, and the same field (route targets, capabilities) is spelled
    both ways across templates.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    return value


# Annotated aliases used throughout the contract. Reading `Speed` at a field
# tells you both the type and that the coercion above applies to it.
CleanStr = Annotated[str, BeforeValidator(_coerce_str)]
OptionalInt = Annotated[int | None, BeforeValidator(_coerce_optional_int)]
CoercedInt = Annotated[int, BeforeValidator(_coerce_int)]
CoercedBool = Annotated[bool, BeforeValidator(_coerce_bool)]
StrList = Annotated[list[str], BeforeValidator(_coerce_str_list)]


class DiscoverySchema(BaseModel):
    """Base for every model in the discovery contract."""

    model_config = ConfigDict(
        # See the module docstring: a vendor's own keys survive validation.
        extra="allow",
        # Fields may be set by their alias or their Python name, so a payload
        # that already speaks NAPALM's spelling validates unchanged.
        populate_by_name=True,
        # CLI-parsed strings arrive padded far more often than not.
        str_strip_whitespace=True,
        # The pipeline mutates models in place (process.py fills in VRF
        # membership after construction); validate those assignments too, so a
        # bad write is caught where it happens rather than at the sink.
        validate_assignment=True,
    )
