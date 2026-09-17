"""
The sinks this project ships.

One so far. It is an ordinary plugin -- advertised from pyproject's
"net2sot.sinks" entry points, found by the same registry lookup a third
party's sink would be -- and it is the worked example to read before writing
another (Infrahub, a CMDB, a directory of YAML files).
"""

from net2sot.sinks.netbox import NetBoxSink

__all__ = ["NetBoxSink"]
