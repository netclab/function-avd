# function-avd

[![CI](https://github.com/netclab/function-avd/actions/workflows/ci.yml/badge.svg)](https://github.com/netclab/function-avd/actions/workflows/ci.yml)
[![Release](https://github.com/netclab/function-avd/actions/workflows/release.yml/badge.svg)](https://github.com/netclab/function-avd/actions/workflows/release.yml)

**Arista AVD configs, kept live by Kubernetes.** Instead of running Ansible to produce
device configs once, a fabric is a Crossplane composite resource: edit it, and every
affected device re-renders on reconcile — including the devices you did not touch,
because AVD's facts are fabric-wide.

The engine is [pyavd](https://pypi.org/project/pyavd/) (pure Python, no Ansible), wrapped
in a Crossplane composite function. Built against
[Arista AVD](https://github.com/aristanetworks/avd) v6.3.0, pinned as a submodule under
`avd/` — a read-only reference, and the source of the golden configs the tests diff
against.

## Quick start

Needs `docker`, `kind`, `kubectl`, `helm`, the `crossplane` CLI, and
[`uv`](https://docs.astral.sh/uv/).

```bash
scripts/kind-up.sh          # registry + kind + Crossplane + the function + XRDs
kubectl --context kind-avd apply -f examples/fabric/single-dc-l3ls.yaml
kubectl --context kind-avd -n default get fabric,device
```

One `Fabric` fans out to 8 `Device`s, each composing a ConfigMap with that device's AVD
structured config and rendered `eos.cfg`:

```bash
kubectl --context kind-avd -n default get cm -l avd.netclab.dev/device=dc1-leaf1a \
  -o jsonpath='{.items[0].data.eos\.cfg}'
```

Patch a spine's `bgp_as` to see the model live: the change reaches that spine *and* its
leaves' BGP neighbours. `scripts/kind-down.sh` tears it all down.

## The API

Six composite kinds, one function image dispatching on `kind`:

| Kind | What it is |
|------|------------|
| `Fabric` | one AVD fabric. Design comes from `spec.design` inline, from the inputs `spec.requires` names, or both. Composes one `Device` per host. |
| `Device` | one switch: validates and renders its own config, composes the ConfigMap, and with `spec.push` a provider-http `Request` that holds the box in sync over eAPI. |
| `NodeSet` | node-type blocks, and the devices they declare — the fabric's device list. |
| `NetworkServiceSet` | tenants, VRFs, SVIs. |
| `ConnectedEndpointSet` | servers, ports, port profiles. |
| `SettingSet` | everything fabric-wide: BGP peer groups, AAA, NTP, addressing pools. |

`spec.requires` is an ordered list; per host, the inputs that apply are layered in that
order with `dict.update()` — Ansible's `hash_behaviour=replace`. Nothing is merged, so
two `NodeSet`s carrying the same node-type key never meet. `spec.appliesTo` scopes an
input (`all`, `nodeSets`, `hosts`, `matchHostnames`); on a `NodeSet`, left out, it means
the devices that `NodeSet` declares.

⚠ **A Fabric whose named inputs have not arrived renders nothing** and names what it is
waiting for. Requirements are answered on the *next* reconcile, so the first always
arrives empty — and a push replaces a device's whole configuration.

`examples/fabric/` holds a `Fabric` per AVD example using `spec.design`;
`examples/fabric/inputs/` holds the same fabrics through `spec.requires`. Neither names a
namespace, so `kubectl -n <ns> apply -f <file>` decides where they land.

## Migrating an AVD inventory

`avd-migrate` turns an Ansible inventory into a `Fabric` plus its input XRs — one fabric
per eos_designs play, one input per ownership fragment per category.

```bash
uv run avd-migrate avd/ansible_collections/arista/avd/examples/single-dc-l3ls
uv run avd-migrate <inventory> --emit /tmp/xrs      # write them out as manifests
```

It reimplements no part of Ansible: `ansible-inventory --list --export` says which group
carries which variable, an ad-hoc `debug` run says what a play resolved it to. The one
rule Ansible does not print is group order (depth, then name) — the migration layers
fragments with it and **refuses to emit anything** unless the result equals what Ansible
reports.

What it cannot carry it names rather than dropping. Four flags carry more:

| Flag | What it does |
|------|--------------|
| `--namespace NS` | pin the manifests to one namespace instead of naming none |
| `--emit-pool-seed` | carry the node-ID assignments as a ConfigMap the Fabric seeds from |
| `--compat-ip-addressing` | point `ip_addressing` at `function.avd_compat` where the pinned templates are the ones it transcribes |
| `--drop-description-templates` | drop interface-description templates so the rest renders |

Measured against AVD's own corpus: hostvars byte-identical to Ansible for all 8 bundled
examples and 19 molecule scenarios, up to 501 devices in one play; rendered configs
matching AVD's checked-in golden for **8 of 8 examples on a cluster**, every device.

## The lab

`WITH_NETCLAB=1` additionally brings up containerised cEOS nodes, cabled from the fabric
itself — `avd-topology` derives the netclab topology from the same model that renders the
configs, so the two cannot drift apart.

```bash
WITH_NETCLAB=1 scripts/kind-up.sh
kubectl --context kind-avd apply -k examples/lab/
```

`examples/lab/` is that same fabric with eAPI bound to the default VRF — the one thing a
lab needs and production does not — and with `spec.push` set, so each Device keeps the
box itself in sync. A change made by hand on the device is replaced with the golden
config on the next provider poll; EOS's own `show running-config digest` certifies both
states. Deleting a Device orphans the box rather than wiping it: an empty switch is not a
desired state.

cEOS is licensed and cannot be pulled; import it once and it outlives teardown in the
registry's data volume (`kind-up.sh` says how). `LAB_HOSTS` picks the subset, defaulting
to the two that make one link, because all eight is 16Gi of cEOS.

## Testing

```bash
uv run pytest              # offline, no cluster (~90s)
uv run pytest -m corpus    # the whole molecule corpus: 501 devices, 71 plays (~4min)
uv run pytest -m e2e       # live cluster: needs kind-up.sh (~6min)
```

| Path | Covers |
|------|--------|
| `tests/test_kinds_equivalence.py` | the inputs resolve exactly as Ansible does, and render golden |
| `tests/test_fabric_collect.py` | collection and the gate, driving `RunFunction` directly |
| `tests/test_nulls.py` | an explicit null survives an API server that prunes it |
| `tests/test_categories.py` | which kind a key belongs to still matches AVD's own schema |
| `tests/test_avd_compat.py` | the v2.x addressing class against AVD's golden |
| `tests/test_e2e_migrated_corpus.py` | all 8 examples migrated, applied, rendered, diffed — on a cluster |
| `tests/test_e2e_node_id_pool.py` | the node-ID pool, seeded and kept, on a cluster |
| `tests/test_e2e_device_layer.py` | drift/reclaim + steady-state idempotency (needs an applied fabric) |
| `tests/test_e2e_push_layer.py` | on-box drift reclaim on the netclab lab (skips without it) |

CI gates every push and PR on the offline suite (with `submodules: true` — the goldens
live there). The e2e job builds the image and installs Crossplane, so it is
`workflow_dispatch` only.

## Releasing

`[project].version` in `pyproject.toml` is the only version number; a tag that disagrees
fails the run rather than publishing under either number. ⚠ `uv.lock` carries the project
version too, and CI runs `uv sync --locked` — bump both in one commit.

```bash
uv version 0.2.0 && uv sync
git commit -am 'Release v0.2.0' && git tag v0.2.0
git push origin main v0.2.0
```

The workflow builds the runtime image for `linux/amd64` and `linux/arm64`, embeds each
into an xpkg, and pushes both as one multi-platform package — plus a **second package**,
`configuration-avd`, the API this function serves. Both go to `ghcr.io/netclab/` always,
and to `xpkg.upbound.io/netclab/` when the `UPBOUND_TOKEN` secret is set. Only the latter
feeds [the Marketplace](https://marketplace.upbound.io/functions/netclab).

Installing the Configuration pulls the function in as a dependency, so this is the one
line a consumer needs:

```yaml
apiVersion: pkg.crossplane.io/v1
kind: Configuration
metadata:
  name: configuration-avd
spec:
  package: ghcr.io/netclab/configuration-avd:<version>
```

There is no `latest`, deliberately: a function whose version can move under a running
cluster is not one you can reason about.

## Layout

| Path | Purpose |
|------|---------|
| `function/main.py` | gRPC entrypoint (`avd-function`, the image's ENTRYPOINT) |
| `function/fn.py` | the composite function: dispatch, the six kinds, composition |
| `function/engine.py` | pyavd pipeline wrapper |
| `function/kinds.py` | the input model: `Input`, `resolve`, which kind a key belongs to |
| `function/pools.py` | the node-ID pool a Fabric composes and seeds from |
| `function/nulls.py` | an explicit null carried past an API server that prunes it |
| `function/push.py` | eAPI push protocol: the provider-http `Request` builders |
| `function/avd_compat.py` | AVD behaviours pyavd cannot reach, as the classes AVD asks for |
| `function/ansible_cli.py` | Ansible asked rather than reimplemented (dev-only) |
| `function/migrate.py` | an AVD inventory → `Fabric` + input XRs (`avd-migrate`) |
| `apis/` | XRD + Composition per kind; `apis/crossplane.yaml` is the Configuration's metadata |
| `dev/` | `Function` manifests for local use; outside `apis/` so the build needs no exclusions |
| `examples/fabric/`, `examples/fabric/inputs/` | the same fabrics, by design and by inputs |
| `examples/lab/` | kustomize overlay: that fabric as run on the netclab lab |
| `scripts/kind-up.sh`, `kind-down.sh` | reproducible cluster bring-up / teardown |
| `avd/` | AVD v6.3.0 submodule (read-only) |

`pyavd` matches the `avd` submodule tag — the goldens come from the submodule, so the two
only ever move together, which is why Renovate leaves both alone.

## License

Apache-2.0. Builds on [Arista AVD](https://github.com/aristanetworks/avd), also
Apache-2.0; the example fabrics are derived from AVD's own published examples.
