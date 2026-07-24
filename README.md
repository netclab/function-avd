# function-avd

[![CI](https://github.com/netclab/function-avd/actions/workflows/ci.yml/badge.svg)](https://github.com/netclab/function-avd/actions/workflows/ci.yml)
[![Release](https://github.com/netclab/function-avd/actions/workflows/release.yml/badge.svg)](https://github.com/netclab/function-avd/actions/workflows/release.yml)

**Arista AVD configs, kept live by Kubernetes.** Instead of running Ansible to produce
device configs once, a fabric is modelled as a Crossplane composite resource: edit the
`Fabric`, and every affected device's config re-renders on reconcile — including the
devices you didn't touch, because AVD's facts are fabric-wide.

The engine is [pyavd](https://pypi.org/project/pyavd/) (pure Python, no Ansible),
wrapped in a Crossplane composite function. Built against
[Arista AVD](https://github.com/aristanetworks/avd) v6.3.0, pinned as a submodule under
`avd/` — a read-only reference, and the source of the golden configs the tests diff
against.

> The layout follows [function-template-python](https://github.com/crossplane/function-template-python):
> the code lives in a flat `function/` package, with `main.py` as the gRPC
> entrypoint and `fn.py` as the FunctionRunner.

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

### The lab

`WITH_NETCLAB=1` additionally brings up containerised cEOS nodes, cabled from the fabric
itself: AVD resolves the peering, so `avd-topology` derives the netclab topology from the
same model that renders the configs, and the two cannot drift apart.

```bash
WITH_NETCLAB=1 scripts/kind-up.sh     # + CNI plugins, Multus, netclab-chart, cEOS nodes
kubectl --context kind-avd apply -k examples/lab/
```

cEOS is licensed and cannot be pulled — import it once and it outlives teardown in the
registry's data volume; `kind-up.sh` says how if it is missing. `LAB_HOSTS` picks the
subset, defaulting to the two that make one link, because all eight is 16Gi of cEOS.

`examples/lab/` is that same fabric with eAPI bound to the default VRF, which is the one
thing a lab genuinely needs and production does not: AVD binds eAPI to VRF MGMT on
Management1, and a cEOS pod has neither. It also sets `spec.push`, so each lab Device
composes a provider-http `Request` that keeps the box itself in sync over eAPI:

```bash
kubectl --context kind-avd -n default get requests.http.m.crossplane.io,device
kubectl --context kind-avd -n default exec dc1-spine1 -- Cli -p 15 -c "show running-config digest"
```

The Request's OBSERVE reads the device's own `show running-config digest`; a mismatch --
whether the model changed or someone edited the box by hand -- makes the provider replay
a full `configure session` + `rollback clean-config` replace. Drift is reclaimed on the
provider's poll interval, the same rhythm that paces the rest of the model.

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
ConfigMap with `structured_config.yaml` + `eos.cfg`. With `spec.push` set (the Fabric
propagates it from its own `spec.push`, lab-only today), the Device also composes a
namespaced provider-http `Request` that pushes `eos.cfg` to the device over eAPI and
holds it there — `Deployed` condition, `status.push.digest`, and readiness then include
the box itself, not just the rendered artifact (see `push.py` for the protocol).

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
re-renders that spine *and* rewrites `remote-as` on each of the four L3 leaves that peer
with it, because the pipeline recomputes AVD facts for the whole fabric. What paces this
is Crossplane's poll interval, not the render — measured on kind, the touched spine went
in ~5s and its leaves within ~45s (see [Gotchas](#gotchas)).

The loop closes on the lab: with `spec.push` set, the same `bgp_as` bump reached the
touched spine's *running config* in ~47s and rewrote `remote-as` on its peering leaf's
running config in ~63s — and the reverse direction holds too, a `vlan 999` added by hand
on the box was replaced with the byte-identical golden config (EOS's own
`show running-config digest` certifying both states) on the next provider poll.
Deleting a Device orphans the box rather than wiping it: an empty switch is not a
desired state.

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
(`function/xr.py`) unions each DC's node-type blocks and pushes per-DC/per-pod
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
| `tests/test_push.py` | the eAPI push Request builders: session contents, digest provenance |
| `tests/test_e2e_device_layer.py` | drift/reclaim + steady-state idempotency, on a cluster |
| `tests/test_e2e_push_layer.py` | on-box drift reclaim + at-rest quiet, on the netclab lab (skips without it) |

CI gates every push and PR on the offline suite (with `submodules: true` — the golden
configs live there). The e2e job builds the function image and installs Crossplane, so it
is `workflow_dispatch` only.

## Releasing

`[project].version` in `pyproject.toml` is the only version number: the release workflow
publishes under it and `kind-up.sh` tags the local image with it, so a locally built
image and a released package can't mean different code under the same tag. Cutting a
release is a bump, a commit, and a matching tag:

```bash
uv version 0.2.0            # edits pyproject.toml
git commit -am 'Release v0.2.0' && git tag v0.2.0
git push origin main v0.2.0
```

A tag that disagrees with `pyproject.toml` fails the run rather than publishing under
either number. `workflow_dispatch` publishes too, taking no input — it releases whatever
version the checked-out tree declares.

The workflow reruns the offline suite, builds the runtime image for `linux/amd64` and
`linux/arm64`, embeds each into an xpkg, and pushes both as one multi-platform package:

| Destination | When |
|-------------|------|
| `ghcr.io/netclab/function-avd:<version>` | always — the workflow's `GITHUB_TOKEN` is enough |
| `xpkg.upbound.io/netclab/function-avd:<version>` | only when the `UPBOUND_TOKEN` secret is set |

The second one is what feeds
[marketplace.upbound.io/functions/netclab](https://marketplace.upbound.io/functions/netclab):
the Marketplace indexes `xpkg.upbound.io`, so a GHCR-only release is installable but not
listed. `UPBOUND_TOKEN` is an Upbound robot or personal access token with write access to
the `netclab` organization; without it the release still succeeds and logs a warning.
`up xpkg push --create` makes the repository on first push, but marking it *public* — the
part that puts it on the Marketplace — is a one-time toggle in the Upbound console.

Either registry makes the function installable without the local-registry dance in
`kind-up.sh`. Pick a tag from
[releases](https://github.com/netclab/function-avd/releases) — there is no `latest`,
deliberately: a function whose version can move under a running cluster is not one you
can reason about.

```yaml
apiVersion: pkg.crossplane.io/v1
kind: Function
metadata:
  name: function-avd
spec:
  package: ghcr.io/netclab/function-avd:<version>
```

## Gotchas

The non-obvious things this repo encodes, each of which cost a debugging session:

- **`Struct` has no integers.** Crossplane passes resources as protobuf `Struct`, whose
  only numeric type is `double`, so VLAN ids and ASNs arrive as floats and pyavd's schema
  rejects them. `fn._normalize_numbers` coerces whole-number floats back to
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
- **Propagation waits on the poll interval, not on the render.** Crossplane polls every
  60s by default, so a change that doesn't raise a prompt watch event just sits until the
  next pass. Measured on a fresh kind cluster: a direct `Device` patch renders in 2-41s,
  while the Fabric reclaiming that drift takes ~127s — roughly two chained intervals,
  since the Fabric has to notice first and the Device re-render after. A re-render that
  "didn't happen" has usually just not happened *yet*; the e2e budgets allow for it.
- **A pushed config has to keep its own transport alive.** `configure replace` is a
  *full* replace, so whatever eAPI the device was reached over is gone unless the pushed
  config re-states it. AVD renders `management api http-commands / protocol https` bound
  to the VRF from `mgmt_interface_vrf` (MGMT) — so a lab that bootstraps eAPI on plain
  HTTP, or reaches the device outside VRF MGMT, loses it on the first successful push.
  `examples/lab/` binds eAPI to the default VRF for exactly this reason; the netclab
  chart bootstraps the same https/443 AVD renders, so bootstrap and steady state agree.
  The same rule holds for *credentials*: the chart bootstraps `arista`/`arista`, and the
  model's `arista` user carries the sha512 of that very password — one Secret works
  before and after the first push. Change either side alone and the push locks itself out.
- **A config session must not have a fixed name.** EOS keeps exactly **one** completed
  session in history (`show configuration sessions`), so `configure session avd-<hash>`
  works once — and then every retry of the same revision, which is precisely the
  drift-reclaim re-push, fails with `already completed`. The push uses an *unnamed*
  session (the device picks a fresh name each attempt) and carries its revision in the
  JSON-RPC `id` instead.
- **eAPI reports failure inside an HTTP 200.** A failed command comes back as a JSON
  `error` member on a perfectly successful HTTP response, so provider-http counts the
  push as a success and `Synced` stays `True`. The loop still converges — the next
  OBSERVE sees the digest mismatch and retries — but the *only* honest failure signal
  is the Request's `status.response.body`, not its conditions.
- **Record the golden digest only from a response that proves its revision.** The
  running-config digest cannot be predicted from `eos.cfg` (EOS canonicalizes), so it is
  recorded from the device — but the Request's status can lag the spec by a reconcile.
  A push response is trusted via the revision-scoped JSON-RPC `id` it echoes; an observe
  response only via the pushed `alias avd_cfg_<hash>` marker, and even then only while
  no digest is recorded for that revision — otherwise manual drift observed at the wrong
  moment would be blessed as the new golden state and the model change would never land.
- **`management_eapi` is all-or-nothing.** Absent, it defaults to enabled (https, VRF
  MGMT) — which is what golden shows. But set *any* of it without `enabled: true` and the
  block defaults to disabled, rendering no `management_api_http` at all: a config that
  locks you out of the device it is pushed to.
- **`Responsive=False WatchCircuitOpen` looks like the culprit and isn't.** Devices report
  it ("Too many watch events from ConfigMap/…") for long stretches after the create burst,
  right next to every slow re-render — but with the breaker open, a direct Device patch
  still rendered in 2s. Blaming it costs you an afternoon; the poll interval above is the
  real pacing. At rest the model is idempotent and rewrites nothing regardless.

## Layout

| Path | Purpose |
|------|---------|
| `function/main.py` | gRPC entrypoint (`avd-function`, the image's ENTRYPOINT) |
| `function/fn.py` | the Crossplane composite function (FunctionRunner: Fabric + Device) |
| `function/engine.py` | pyavd pipeline wrapper; `render_fabric_design` is the function's core |
| `function/push.py` | eAPI push protocol: the provider-http `Request` builders |
| `function/xr.py` | fold an Ansible example into a `Fabric` document (block union + defaults push-down) |
| `function/ansible_inputs.py` | rebuild `all_inputs` from an Ansible example (inventory + group_vars merge) |
| `function/verify_example.py`, `verify_xr.py` | golden-diff harnesses (`avd-verify`, `avd-verify-xr`) |
| `apis/fabric/`, `apis/device/` | XRD + Composition for each layer |
| `apis/function/` | `Function` manifests (cluster install, and local `crossplane render`) |
| `examples/fabric/` | example `Fabric` XRs (each reproduces golden) |
| `examples/lab/` | kustomize overlay: the same fabric as run on the netclab lab |
| `Dockerfile`, `package/crossplane.yaml` | function runtime image + package metadata |
| `scripts/kind-up.sh`, `kind-down.sh` | reproducible cluster bring-up / teardown |
| `.github/workflows/` | `ci.yml` (offline suite + dispatchable e2e), `release.yml` (GHCR + Upbound) |
| `avd/` | AVD v6.3.0 submodule (read-only) |

Versions are pinned deliberately: `pyavd` matches the `avd` submodule tag, because the
golden configs come from the submodule — the two only ever move together, which is why
Renovate leaves both alone (`renovate.json`).

## License

Apache-2.0. Builds on [Arista AVD](https://github.com/aristanetworks/avd), also
Apache-2.0; the example fabrics are folded from AVD's own published examples.
