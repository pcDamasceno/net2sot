# GitLab CI

`.gitlab-ci.yml` runs the discovery pipeline from CI. The runner must be able to
reach both NetBox and the devices' management IPs — use a runner inside your
network, not a shared SaaS runner.

The file is heavily commented; this page is the short version.

## One-time setup

Set these as **masked** CI/CD variables (Settings → CI/CD → Variables):

| Variable          | Purpose                        |
|-------------------|--------------------------------|
| `NETBOX_URL`      | NetBox base URL                |
| `NETBOX_TOKEN`    | NetBox API token (write)       |
| `DEVICE_USERNAME` | SSH username for devices       |
| `DEVICE_PASSWORD` | SSH password for devices       |

`NETBOX_URL` and `NETBOX_TOKEN` are enforced by a pre-flight check on every
`import`-stage job. Without `NETBOX_URL`, a run would silently fall back to the
placeholder committed in `settings.yaml` — in a fork that points it at a real
instance, that is how a pipeline writes to the wrong NetBox.

To trigger pipelines from the API, create a **pipeline trigger token** under
Settings → CI/CD → Pipeline trigger tokens. A personal access token is rejected
by the trigger endpoint with a bare 404; it works on the `/pipeline` endpoint
instead.

## Jobs

| Job | When | What it does |
|-----|------|--------------|
| `lint` | every push / MR | byte-compiles `net2sot/`, validates all YAML, runs `pytest` and `ruff`, smoke-tests the CLI |
| `import:new-device` | `device_name` + `device_ip` given | imports that one device |
| `rediscover:from-netbox` | any `from_netbox` / `filter_*` given, or manual on the default branch | re-discovers devices already in NetBox |
| `import:all-devices` | manual, default branch | re-imports the whole static inventory |

Every import job uploads `discovery_output/` as an artifact — kept for one week,
even when the job fails, so a failed run still ships the reason it failed.

## Inputs and variables

The pipeline declares [inputs](https://docs.gitlab.com/ee/ci/inputs/) in its
`spec:` header. Inputs are validated at pipeline-compile time and cannot be
overridden by a stray project-level variable, so they are the safer channel.

The re-discovery filters additionally accept CI/CD **variables** of the same
name in upper case (`FILTER_SITE`, `FROM_NETBOX`, …), which is what makes them
usable from a pipeline schedule or from *Run pipeline* without the inputs form.
When both are set, the input wins.

| Input | Variable | Notes |
|---|---|---|
| `device_name` | — | Device to import ad-hoc |
| `device_ip` | — | Management IP / hostname; required with `device_name` |
| `device_platform` | — | `ios` \| `eos` \| `iosxr` \| `nxos` \| `srlinux` \| `linux` (default `ios`) |
| `site_name` | — | Overrides the site from `settings.yaml` |
| `device_role` | — | NetBox role for the device(s) |
| `collector` | `COLLECTOR_BACKEND` | `napalm` \| `scrapli` \| `netmiko`; a group that pins one ignores it |
| `from_netbox` | `FROM_NETBOX` | Re-discover from NetBox instead of `hosts.yaml` |
| `filter_site` | `FILTER_SITE` | Site slug(s), space-separated |
| `filter_location` | `FILTER_LOCATION` | Location slug(s) |
| `filter_platform` | `FILTER_PLATFORM` | Platform slug(s) |
| `filter_device` | `FILTER_DEVICE` | Device name(s), case-insensitive |
| `netbox_branch` | `NETBOX_BRANCH` | Apply the run inside an existing netbox-branching branch |

Every filter takes a **space-separated list**. Filters AND across kinds and OR
within one kind. Any `filter_*` implies `from_netbox`, so `from_netbox=true` is
only needed to re-discover **everything** — which is a legitimate but loud
request, and the job logs a warning when no filter is given.

## Onboarding a new device

```bash
curl -X POST \
  -F "token=$TRIGGER_TOKEN" \
  -F "ref=main" \
  -F "inputs[device_name]=edge-01" \
  -F "inputs[device_ip]=192.0.2.10" \
  -F "inputs[device_platform]=ios" \
  -F "inputs[site_name]=lab" \
  -F "inputs[device_role]=edge-router" \
  "https://gitlab.example.com/api/v4/projects/<project-id>/trigger/pipeline"
```

With a personal access token, use the pipeline endpoint instead:

```bash
curl -X POST \
  -H "PRIVATE-TOKEN: $GITLAB_PAT" \
  -H "Content-Type: application/json" \
  -d '{"ref":"main","inputs":{"device_name":"edge-01",
       "device_ip":"192.0.2.10","device_platform":"ios",
       "site_name":"lab","device_role":"edge-router"}}' \
  "https://gitlab.example.com/api/v4/projects/<project-id>/pipeline"
```

Both need the `spec:` header present **on the ref being triggered**, otherwise
the API rejects the call with `Given inputs not defined in the 'spec' section`.

## Re-discovering devices already in NetBox

As an input:

```bash
curl -X POST \
  -F "token=$TRIGGER_TOKEN" \
  -F "ref=main" \
  -F "inputs[filter_site]=datacenter-1" \
  "https://gitlab.example.com/api/v4/projects/<project-id>/trigger/pipeline"
```

As a variable — works from a schedule too:

```bash
curl -X POST \
  -F "token=$TRIGGER_TOKEN" \
  -F "ref=main" \
  -F "variables[FILTER_SITE]=datacenter-1" \
  "https://gitlab.example.com/api/v4/projects/<project-id>/trigger/pipeline"
```

A nightly schedule with `FILTER_SITE` set and `NETBOX_BRANCH` pointing at a
branch is a good way to see drift without committing to main: review the
branch's diff in NetBox and merge it when it looks right.

## Troubleshooting

- **`Given inputs not defined in the 'spec' section`** — the ref you triggered
  doesn't carry the `spec:` header. Trigger a ref that has this file.
- **A bare 404 from the trigger endpoint** — you used a personal access token
  where a pipeline trigger token is required.
- **`Failed to resolve '<some host>'` for a host you didn't configure** —
  `NETBOX_URL` wasn't set, so the run fell back to the `netbox_url` committed in
  `net2sot/settings.yaml`. Set the variable.
- **The GitLab host and project ID in the examples are placeholders.** Use your
  own — Settings → General shows the project ID.
