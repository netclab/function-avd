# netclab-avd

[![CI](https://github.com/netclab/netclab-avd/actions/workflows/ci.yml/badge.svg)](https://github.com/netclab/netclab-avd/actions/workflows/ci.yml)

**Arista AVD configs, kept live by Kubernetes.** Instead of running Ansible to produce
device configs once, a fabric is modelled as a Crossplane composite resource: edit the
`Fabric`, and every affected device's config re-renders on reconcile — including the
devices you didn't touch, because AVD's facts are fabric-wide.

The engine is [pyavd](https://pypi.org/project/pyavd/) (pure Python, no Ansible),
wrapped in a Crossplane composite function. Built against
[Arista AVD](https://github.com/aristanetworks/avd) v6.3.0, pinned as a submodule under
`avd/` — a read-only reference, and the source of the golden configs the tests diff
against.

> The distribution is `netclab-avd`; the import package is `avd_live_model`.

## Quick start

Needs `docker`, `kind`, `kubectl`, `helm`, the `crossplane` CLI, and
[`uv`](https://docs.astral.sh/uv/).

```bash
scripts/kind-up.sh          # registry + kind + Crossplane + the function + XRDs
kubectl --context kind-avd apply -f examples/fabric/single-dc-l3ls.yaml
kubectl --context kind-avd -n default get fabric,device
```

One `Fabric` fans out to 8 `Device`s, each composing a ConfigMap that holds its AVD
structured config and rendered `eos.cfg`:

```bash
kubectl --context kind-avd -n default get cm -l avd.netclab.dev/device
kubectl --context kind-avd -n default get cm -l avd.netclab.dev/device=dc1-leaf1a \
  -o jsonpath='{.items[0].data.eos\.cfg}'
```

To see the model actually live, patch a spine's `bgp_as` under `spec.design` — the
change reaches that spine *and* its leaves' BGP neighbours, because the pipeline
recomputes fabric-wide facts. `scripts/kind-down.sh` tears it all down.

**No cluster?** The engine and the function both run locally:

```bash
uv run avd-verify           # pyavd vs AVD's own golden structured configs
uv run avd-verify-xr        # same, through the Fabric-XR fold
crossplane render examples/fabric/single-dc-l3ls.yaml \
  apis/fabric/composition.yaml apis/function/function-render.yaml
```

## How it works

Two composite types, served by one function image that dispatches on `kind`:

**`Fabric`** — one AVD fabric, meaning a single `fabric_name`. `spec.design` carries a
fabric-wide `eos_designs` document (structurally open, validated by pyavd); device roles
resolve inside it via `default_node_types`. The function runs the fabric-wide pyavd
pipeline (`validate_inputs → get_avd_facts → get_device_structured_config`) and composes
one `Device` per host, carrying that device's config inline in `spec.structuredConfig`.
Multi-DC topologies are still one fabric when they share a `fabric_name` (DCs stitched
via `evpn_gateway`).

**`Device`** — validates and renders its own config (`validate_structured_config` +
`get_device_config`), reports per-device status (`configHash`, `managementAddress`,
`nodeType`, and `Validated`/`Rendered` conditions), and composes the artifact: a
ConfigMap with `structured_config.yaml` + `eos.cfg`.

Why the split? Facts are fabric-wide, so the fabric has to be the reconcile unit — one
spine's `bgp_as` must reach its peers. But the config push to the device (or CVP)
attaches *per device*, so a Device has to be a real reconcile unit with its own status,
not a passive ConfigMap. The e2e test pins exactly that contract: pause the Fabric and
patch a Device directly, and it still renders on its own; unpause, and the Fabric takes
the change back. The Device is live, but not authoritative.

## Status

The full path works on a real cluster: applying two `Fabric` XRs in one namespace
reconciles to `Synced=True Ready=True`, fanning out to 24 `Device`s → 24 ConfigMaps, all
`Ready`, contents matching AVD golden.

Liveness is fabric-wide, not just local: bumping a spine's `bgp_as` in `spec.design`
re-renders that spine *and* rewrites `remote-as` on all six leaves that peer with it,
because the pipeline recomputes AVD facts for the whole fabric. Typically a few seconds
end to end — though a device whose watch circuit breaker has tripped is throttled and can
take ~45s (see [Gotchas](#gotchas)).

### Engine fidelity

Before wrapping pyavd in Crossplane, the point was to prove the pyavd path reproduces
AVD's own output. Each bundled AVD example is rebuilt into `all_inputs` the way Ansible
would (per-host group_vars merge, in precedence order), run through pyavd, and deep-diffed
against the example's checked-in `intended/structured_configs`.

| Example | Devices | From Ansible inputs | Folded into one `Fabric` |
|---------|--------:|:-------------------:|:------------------------:|
| single-dc-l3ls | 8 | ✅ | ✅ |
| single-dc-l3ls-ipv6 | 8 | ✅ | ✅ |
| single-dc-multipod-l3ls | 10 | ✅ | ✅ |
| dual-dc-l3ls | 16 | ✅ | ✅ |
| l2ls-fabric | 6 | ✅ | ✅ |
| isis-ldp-ipvpn | 9 | ✅ | ✅ |
| campus-fabric | 10 | ✅ | ⏸ deferred |
| cv-pathfinder | 17 | ⏸ deferred | ⏸ deferred |

Zero diffs everywhere it is ticked, across both group_vars layouts (per-group directories
and flat files) and explicit and implicit `all` inventories. The fold
(`avd_live_model.xr`) unions each DC's node-type blocks and pushes per-DC/per-pod
`defaults` down to node_groups/nodes (which override defaults in AVD), so multi-DC
fabrics collapse losslessly into one document.

The two deferrals are understood, not mysterious:

- **campus-fabric** — leaves carry RADIUS in `aaa_settings`, spines don't: a
  fabric-global key that genuinely differs by role, with no node-scoped equivalent.
- **cv-pathfinder** — SD-WAN multi-site (a WAN gateway across 2 routers) plus
  ansible-vault secrets.

They live in `verify_xr.DEFERRED` as *strict* expected failures: if one starts folding,
the suite fails and says to remove it, so a deferral can't quietly rot.

## Testing

```bash
uv run pytest              # offline: engine fidelity, the XR fold, the Struct gotcha (~7s)
uv run pytest -m e2e       # live cluster: needs kind-up.sh + an applied fabric (~2min)
```

| Path | Covers |
|------|---------|
| `tests/test_engine_fidelity.py` | the examples above reproduce golden structured config |
| `tests/test_xr_fold.py` | the Ansible→XR fold, over every discovered example |
| `tests/test_normalize_numbers.py` | the protobuf-`Struct` double→int coercion |
| `tests/test_e2e_device_layer.py` | drift/reclaim + steady-state idempotency, on a cluster |

CI gates every push and PR on the offline suite (with `submodules: true` — the golden
configs live there). The e2e job builds the function image and installs Crossplane, so it
is `workflow_dispatch` only.

## Gotchas

The non-obvious things this repo encodes, each of which cost a debugging session:

- **`Struct` has no integers.** Crossplane passes resources as protobuf `Struct`, whose
  only numeric type is `double`, so VLAN ids and ASNs arrive as floats and pyavd's schema
  rejects them. `composite_fn._normalize_numbers` coerces whole-number floats back to
  `int` (leaving bools and genuine fractionals alone).
- **Never write a value that changes every reconcile.** An unconditional timestamp causes
  a perpetual reconcile and a watch storm. `lastRenderedTime` is only bumped when
  `configHash` actually changes.
- **Readiness does not propagate by itself.** With function pipelines Crossplane does
  *not* derive a composed resource's readiness from its `Ready` condition — the Fabric
  function reads each observed Device's readiness and sets `ready` explicitly, so
  `Fabric.Ready` reflects real per-device state.
- **Two image pullers, one reference.** The function image must be pulled by the
  Crossplane pod (cluster network) *and* the node's containerd (node network, which can't
  resolve cluster DNS). Referencing the registry by its kind-network IP over plain HTTP
  satisfies both; containerd is told the registry is insecure.
- **Composed names are XR-scoped**, not `fabric_name`-scoped, so two fabrics with the same
  `fabric_name` and overlapping hostnames never collide over a Device or a ConfigMap.
- **Non-root runtime.** The image entrypoint runs the installed console script directly,
  not `uv run`, which would need a writable cache under `$HOME`.
- **A tripped watch circuit breaker throttles reconciles.** After the initial create burst
  a Device can report `Responsive=False WatchCircuitOpen` ("Too many watch events from
  ConfigMap/…"); while it does, its re-render is throttled — observed at ~45s, against ~5s
  for the same change on an unthrottled device. Nothing is wrong: at rest the model is
  idempotent and rewrites nothing. But it does mean a re-render that "didn't happen" has
  usually just not happened *yet*, and the e2e timeouts allow for it.

## Layout

| Path | Purpose |
|------|---------|
| `src/avd_live_model/engine.py` | pyavd pipeline wrapper; `render_fabric_design` is the function's core |
| `src/avd_live_model/composite_fn.py` | the Crossplane composite function (`avd-function`) |
| `src/avd_live_model/xr.py` | fold an Ansible example into a `Fabric` document (block union + defaults push-down) |
| `src/avd_live_model/ansible_inputs.py` | rebuild `all_inputs` from an Ansible example (inventory + group_vars merge) |
| `src/avd_live_model/verify_example.py`, `verify_xr.py` | golden-diff harnesses (`avd-verify`, `avd-verify-xr`) |
| `apis/fabric/`, `apis/device/` | XRD + Composition for each layer |
| `apis/function/` | `Function` manifests (cluster install, and local `crossplane render`) |
| `examples/fabric/` | example `Fabric` XRs (each reproduces golden) |
| `Dockerfile`, `package/crossplane.yaml` | function runtime image + package metadata |
| `scripts/kind-up.sh`, `kind-down.sh` | reproducible cluster bring-up / teardown |
| `avd/` | AVD v6.3.0 submodule (read-only) |

Versions are pinned deliberately: `pyavd` matches the `avd` submodule tag, because the
golden configs come from the submodule — the two only ever move together, which is why
Renovate leaves both alone (`renovate.json`).

## License

Apache-2.0. Builds on [Arista AVD](https://github.com/aristanetworks/avd), also
Apache-2.0; the example fabrics are folded from AVD's own published examples.
