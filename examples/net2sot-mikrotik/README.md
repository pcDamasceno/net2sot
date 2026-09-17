# net2sot-mikrotik

A MikroTik RouterOS collector for
[net2sot](https://github.com/pcDamasceno/net2sot), and a template for
writing your own vendor plugin.

```bash
pip install -e .
python discovery.py --list-plugins        # mikrotik should appear
```

Add a host on `platform: routeros` to the inventory and it is collected by this
plugin automatically — no `--collector` flag and no `groups.yaml` pin, because
the class declares `platforms = ("routeros", "mikrotik")`.

To pin it explicitly anyway:

```yaml
# inventory/groups.yaml
mikrotik:
  platform: routeros
  data:
    collector: mikrotik
```

## Using this as a template

1. Rename the package and the `name` / `platforms` on the class.
2. Replace the three `_collect_*` methods with your own parsing. Return a
   `CollectedFacts`; everything downstream — canonical names, filtering,
   device-type mapping, VRFs, primary IP, the sync itself — is done for you.
3. Point the `net2sot.collectors` entry point at your class.

See [docs/plugins.md](../../docs/plugins.md) for the full contract.

## Status

The RouterOS parsing here is a starting point, not a finished driver: it has
been exercised against the contract, not against a rack of routers. Verify the
`print detail` output on your own RouterOS version before trusting it.
