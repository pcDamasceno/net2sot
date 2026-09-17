# Contributing

Thanks for taking the time. Bug reports, vendor support and documentation fixes
are all welcome.

## Before you open a pull request

Run what CI runs. It is fast and needs neither a device nor a NetBox:

```bash
uv sync --extra dev
uv run --extra dev python -m pytest tests/ -q
uv run --extra dev ruff check net2sot tests examples
uv run python -m compileall -q net2sot
cd net2sot && uv run python discovery.py --help
```

Add a test for anything that parses device output or decides what NetBox
receives. Those are the parts that quietly ruin an inventory, and they are all
testable without hardware — see `tests/` for the fixture style.

## Adding a vendor or platform

**Don't patch the core for it.** The pipeline is extensible at both ends and
neither extension point requires changing this repository:

- a **collector** adds a vendor, platform or transport;
- a **sink** adds a source of truth to write to.

Read [docs/plugins.md](docs/plugins.md) first, then
`net2sot/collectors/paloalto.py` and `collectors/f5.py` as worked
examples. `examples/net2sot-mikrotik/` is a complete plugin in its own
installable package — a plugin can live in your own repository and be wired in
with an entry point.

A collector belongs in *this* repository only when it is a close variant of a
platform the `netmiko` collector already handles and plain SSH plus
ntc-templates will do. Anything else is better off as its own package, where it
can bring its own dependencies and release on its own schedule.

## Reporting a bug

Include the device platform and OS version, the collector used, and the run's
output. `--debug --save-raw` writes the raw device output and a per-device JSON
report under `RAW_DATA_PATH`, and `--output-file` dumps the structured run
result — a redacted excerpt of either says more than a description.

**Never paste real credentials, tokens, or a full unredacted inventory into an
issue.**

## Style

- `ruff` with the config in `pyproject.toml` (line length 100) is the arbiter.
- Comments should explain *why*, not restate the code. The existing ones are
  the house style: they exist where a decision is non-obvious or where a past
  bug taught something.
- Commit messages: a short imperative subject, and a body explaining the
  reasoning when the change is not self-evident.

## License

By contributing you agree that your contributions are licensed under the
[MIT License](LICENSE) that covers this project.
