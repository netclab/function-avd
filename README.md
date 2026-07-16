# avd-live-model

A **living AVD supermodel** driven by Crossplane XRs on Kubernetes.

Builds on [Arista AVD](https://github.com/aristanetworks/avd) v6.3.0, pinned as a
git submodule under `avd/` (read-only reference).

## Goal

Make the AVD structured config *living* — regenerated dynamically from input
objects delivered as **Crossplane Composite Resources (XRs)**, instead of a
static Ansible run. A Crossplane **Python composite function** reconciles one
`Fabric` XR into per-device structured configs.

The engine is [`pyavd`](https://pypi.org/project/pyavd/) (pure Python, no
Ansible). Pipeline: `validate_inputs → get_avd_facts (fabric-wide) →
get_device_structured_config (per device) → …`.

## Milestone 1 — engine fidelity (done)

Before wrapping pyavd in Crossplane, prove the pyavd path reproduces AVD's own
output. The harness:

1. reads an AVD Ansible example (`inventory.yml` + `group_vars/`),
2. rebuilds `all_inputs` (`{hostname: hostvars}`) exactly as Ansible would —
   per-host group-vars merge in Ansible precedence order, `ansible_*` transport
   vars stripped,
3. runs the pyavd pipeline,
4. deep-diffs the result against the example's checked-in
   `intended/structured_configs/*.yml`.

```bash
uv run avd-verify                        # single-dc-l3ls  → 8/8 devices match
uv run avd-verify avd/ansible_collections/arista/avd/examples/dual-dc-l3ls
                                         # dual-dc-l3ls    → 16/16 devices match
```

**7 of the 9 bundled examples reproduce golden structured config with zero
diffs** (67 devices total), across both group_vars layouts (per-group
directories and flat files), explicit and implicit `all` inventories:

| Example | Devices | Result |
|---------|--------:|--------|
| single-dc-l3ls | 8 | ✅ 0 diffs |
| single-dc-l3ls-ipv6 | 8 | ✅ 0 diffs |
| single-dc-multipod-l3ls | 10 | ✅ 0 diffs |
| dual-dc-l3ls | 16 | ✅ 0 diffs |
| campus-fabric | 10 | ✅ 0 diffs |
| l2ls-fabric | 6 | ✅ 0 diffs |
| isis-ldp-ipvpn | 9 | ✅ 0 diffs |
| cv-pathfinder | 17 | ⏸ deferred — SD-WAN design uses `default_node_types` + ansible-vault secrets |
| common | — | shared vars only, not a runnable fabric |

The intermediate `all_inputs` shape is effectively the schema of the future
`Fabric` XR `spec`.

## Milestone 2 — Fabric XRD (in progress)

The `Fabric` custom resource is the input object of the living model. API group
`avd.netclab.dev`, kind `Fabric`, `v1alpha1`, Crossplane v2 (namespaced).

**One `Fabric` == one AVD fabric** (a single `fabric_name`). AVD facts are
fabric-wide, so the fabric is the reconcile unit; a composite function expands
one `Fabric` into per-device structured configs. Multi-DC topologies are still
one fabric when they share a `fabric_name` (DCs stitched via `evpn_gateway`).

`spec.design` carries a fabric-wide AVD eos_designs document (structurally open,
validated by pyavd); device roles resolve inside it via `default_node_types`.
This mapping is proven: `spec.design` → `engine.render_fabric_design` reproduces
golden structured config with **zero diffs** for **6 of 8** bundled examples,
including the multi-DC and multipod fabrics:

```bash
uv run avd-verify-xr   # fold every example → render → diff golden
#   ok: single-dc-l3ls, single-dc-l3ls-ipv6, l2ls-fabric, isis-ldp-ipvpn,
#       dual-dc-l3ls (16 dev), single-dc-multipod-l3ls (10 dev)
```

The Ansible→XR fold (`avd_live_model.xr`) unions each DC's node-type blocks and
**pushes per-DC/per-pod `defaults` down to node_groups/nodes** (which override
defaults in AVD), so multi-DC fabrics collapse losslessly into one document.

Two examples don't fold and are deferred, for understood reasons:

- **campus-fabric** — leaves carry RADIUS in `aaa_settings`, spines don't; a
  fabric-global key that genuinely differs by role (no node-scoped equivalent).
- **cv-pathfinder** — SD-WAN multi-site (WAN gateway across 2 routers) plus
  ansible-vault secrets.

Fixtures for the 6 folding examples live in `examples/fabric/*.yaml`.

### Composite function (Milestone 2b — working)

`avd_live_model.composite_fn` is a Crossplane **Python composite function**
(function-sdk-python) that reconciles one `Fabric` into the living model: it runs
`render_fabric_design` and emits **one ConfigMap per device** holding its AVD
structured config, and reports discovered devices on `status`.

```bash
uv run avd-function --insecure                       # serve on :9443 (dev)
crossplane render examples/fabric/single-dc-l3ls.yaml \
  apis/fabric/composition.yaml apis/function/function-render.yaml
```

Proven end-to-end through `crossplane render`: the rendered ConfigMaps match
AVD golden structured config with **zero diffs** (single-dc 8/8, dual-dc 16/16),
and the XR `status` reports `deviceCount`, `devices[].nodeType`, and validation.

> **Struct number gotcha:** Crossplane passes resources as protobuf `Struct`,
> whose only numeric type is `double`, so integers (VLAN ids, ASNs) arrive as
> floats. `composite_fn._normalize_numbers` coerces whole-number floats back to
> `int` before handing the design to pyavd.

| Path | Purpose |
|------|---------|
| `apis/fabric/composition.yaml` | Composition (`mode: Pipeline`) |
| `apis/function/function.yaml` | `Function` package install (cluster; registry image) |
| `apis/function/function-render.yaml` | `Function` ref for local `crossplane render` (Development runtime) |

### On a kind cluster (Milestone 2c — working)

The function is packaged as an xpkg and installed on a real cluster; applying a
`Fabric` triggers a genuine Crossplane reconcile that materializes the model.

```bash
scripts/kind-up.sh                                   # registry + kind + Crossplane v2 + function + XRD/Composition
kubectl --context kind-avd apply -f examples/fabric/single-dc-l3ls.yaml
kubectl --context kind-avd -n default get fabric,cm -l avd.netclab.dev/device
scripts/kind-down.sh                                 # tear down
```

Verified on kind (Crossplane v2.3.3): two `Fabric` XRs in one namespace reconcile
to `Synced=True Ready=True`, fanning out to **24 `Device` XRs** → **24 rendered
ConfigMaps**, all `Ready`, contents matching AVD golden.

### Two layers: Fabric → Device

The model is split into two composite types (one function image serves both,
dispatching on kind):

- **`Fabric`** — runs the fabric-wide pyavd pipeline (facts) and composes one
  **`Device`** per host, with that device's config inline in
  `spec.structuredConfig`. `Fabric.Ready` aggregates its Devices' readiness.
- **`Device`** — validates and renders *its own* config
  (`validate_structured_config` + `get_device_config`), reports per-device
  status (`Validated`/`Rendered` conditions, `configHash`, `managementAddress`,
  `nodeType`), and composes the rendered artifact (a ConfigMap with
  `structured_config.yaml` + `eos.cfg`). The config-push managed resource attaches
  here in the consumption phase — which is why per-device status lives on Device,
  not on a flat ConfigMap.

**Liveness through both layers** (~2s): patching `Fabric.spec.design` (e.g. a
spine `bgp_as`) propagates to `Device.spec.structuredConfig`, the Device
re-renders, `configHash` changes and the rendered ConfigMap updates — and
fabric-wide facts mean the change also reaches *other* devices' BGP peers.

Two subtleties the function must get right (both encoded in `composite_fn.py`):

- **Idempotency:** never write a value that changes every reconcile (e.g. a
  timestamp) unconditionally — it causes a perpetual reconcile / watch storm.
  `lastRenderedTime` is only bumped when `configHash` actually changes.
- **Readiness propagation:** with function pipelines Crossplane does *not*
  auto-derive a composed resource's readiness from its `Ready` condition — the
  Fabric function reads each observed Device's readiness and sets `ready`
  explicitly, so `Fabric.Ready` reflects real per-device state.

Gotchas encoded in `scripts/kind-up.sh` and the function:

- **Two image pullers, one reference.** The function image must be pulled by the
  Crossplane pod (cluster network) *and* the node's containerd (node network,
  which can't resolve cluster DNS). Referencing the registry by its kind-network
  IP over plain HTTP satisfies both; containerd is told the registry is insecure.
- **Composed names are XR-scoped**, not `fabric_name`-scoped, so two fabrics with
  the same `fabric_name`/overlapping hostnames never collide over a ConfigMap.
- **Non-root runtime.** The image entrypoint runs the installed console script
  directly (not `uv run`, which needs a writable cache).

| Path | Purpose |
|------|---------|
| `apis/device/xrd.yaml`, `apis/device/composition.yaml` | `Device` XRD + Composition (per-device layer) |
| `Dockerfile`, `package/crossplane.yaml` | Function runtime image + package metadata |
| `scripts/kind-up.sh`, `scripts/kind-down.sh` | Reproducible cluster bring-up / teardown |

## Testing

```bash
uv run pytest              # offline: engine fidelity, XR fold, Struct gotcha (~7s)
uv run pytest -m e2e       # live cluster: kind-up.sh + an applied fabric (~2min)
```

| Path | Covers |
|------|---------|
| `tests/test_engine_fidelity.py` | Milestone 1 — 7 examples reproduce golden structured config |
| `tests/test_xr_fold.py` | Milestone 2 — the Ansible→XR fold, over every discovered example |
| `tests/test_normalize_numbers.py` | the protobuf-`Struct` double→int coercion |
| `tests/test_e2e_device_layer.py` | drift/reclaim + steady-state idempotency, on a real cluster |

`.github/workflows/ci.yml` gates every push and PR on the offline suite (it needs
`submodules: true` — the golden configs the tests diff against live there). The
e2e job builds the function image and installs Crossplane, so it is
`workflow_dispatch` only: run it by hand when the function, compositions, or
XRDs change.

The two examples that don't fold are declared in `verify_xr.DEFERRED` with their
reason, and treated as **strict** expected failures: if one starts folding, the
suite fails and tells you to drop it, so a deferral can't quietly rot. That also
makes `avd-verify-xr` exit 0 on the documented state, so it can gate CI.

**What the e2e test pins** — the contract of the two-layer split, which nothing
else checks. Pause the `Fabric` (`crossplane.io/paused=true`) so it stops
propagating, patch `Device.spec.structuredConfig` directly, and the change still
reaches the rendered ConfigMap: the Device is a *real* reconcile unit, which is
what lets a config-push managed resource attach there. Unpause, and the Fabric
rewrites `spec.structuredConfig` on its next pass (~1s) — `configHash` returns to
the exact baseline. The Device is live, but not authoritative.

> Under a tripped watch circuit breaker (`Responsive=False WatchCircuitOpen`,
> which Crossplane opens during the initial create burst) a Device reconciles on
> a throttle, so a *direct* Device patch can take ~15s to render rather than ~1s.
> The e2e timeouts allow for it.

## Layout

| Path | Purpose |
|------|---------|
| `src/avd_live_model/ansible_inputs.py` | Reconstruct `all_inputs` from an Ansible example (inventory + group_vars merge) |
| `src/avd_live_model/engine.py` | pyavd pipeline wrapper; `render_fabric_design` = composite-function core |
| `src/avd_live_model/xr.py` | Fold an Ansible example into a `Fabric` XR document (block union + defaults push-down) |
| `src/avd_live_model/verify_example.py` | Milestone 1 golden-diff harness (`avd-verify`) |
| `src/avd_live_model/verify_xr.py` | Milestone 2 Fabric-XR fold + golden-diff harness (`avd-verify-xr`) |
| `src/avd_live_model/composite_fn.py` | Crossplane Python composite function (`avd-function`) |
| `apis/fabric/xrd.yaml` | `Fabric` CompositeResourceDefinition |
| `apis/fabric/composition.yaml` | Fabric Composition (Pipeline) |
| `apis/function/` | shared `Function` manifests (install + render) |
| `examples/fabric/` | Example `Fabric` XRs (reproduce golden) |
| `avd/` | AVD v6.3.0 submodule (read-only) |

## Tooling

`uv` for everything (`uv add`, `uv run`). The AVD submodule is never modified;
`pyavd==6.3.0` (matching the pin) comes from PyPI.
