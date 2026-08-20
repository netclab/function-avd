"""Turn an AVD Ansible inventory into a Fabric and its input XRs.

The translation reads nothing itself: :mod:`function.ansible_cli` asks Ansible
which group carries which variable, which devices a play runs on, and what the
merged result is. This module only decides how those fragments become XRs.

Four rules, and the last is what makes the others safe:

* **One fabric per play.** AVD renders a play, not an inventory. Where the two
  differ the inventory is wider -- cv-pathfinder carries `cloudvision`, an API
  server that is not a switch -- and a declared device with no node type fails
  the *whole* fabric's render, not just its own.
* **One input per ownership fragment per category.** A group's variables are one
  fragment because Ansible merges them into one namespace; splitting them by
  file would preserve a boundary Ansible does not keep. Splitting the merged
  fragment by category afterwards costs nothing and is what the kinds are for.
* **Values are what the play produced, not what the file says.** An XR has no
  templating engine, no vault and no playbook directory, so anything Ansible
  resolves at play time is resolved before it is written down. See
  :func:`templated`.
* **The precedence model is checked, not trusted.** Group order is (depth, name),
  the one rule the Ansible CLI does not print. :func:`migrate` layers the
  fragments with it and refuses to emit anything unless the result equals the
  per-device variables Ansible reports. A wrong order cannot reach an XR.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .ansible_cli import ALL_GROUP, Inventory, Play, design_plays, plays, read_inventory
from .kinds import Input, KINDS, Vocabulary, by_kind, hosts_in_blocks, is_node_block

#: kinds whose fragment gets a name suffix; a NodeSet keeps the plain name
SUFFIX = {
    "NetworkServiceSet": "services",
    "ConnectedEndpointSet": "endpoints",
    "SettingSet": "settings",
}


class MigrationError(RuntimeError):
    """The migration could not produce inputs it is able to stand behind."""


def slug(name: str) -> str:
    """A Kubernetes object name from an Ansible group or host name.

    Ansible group names are conventionally SHOUTED and use underscores; neither
    survives RFC 1123. Collisions are the caller's to detect -- see
    :func:`_unique`.
    """
    out = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return out or "unnamed"


@dataclass
class Fabric:
    """One play, translated."""

    name: str
    devices: tuple[str, ...]
    inputs: list[Input] = field(default_factory=list)
    play: Play | None = None
    #: AVD's own `fabric_name`, promoted to `spec.fabricName`
    fabric_name: str = ""
    #: things the XRs cannot express, said out loud rather than dropped
    notes: list[str] = field(default_factory=list)

    @property
    def requires(self) -> list[tuple[str, str]]:
        """``spec.requires`` as (kind, name), in precedence order."""
        return [(i.kind, i.name) for i in self.inputs]


@dataclass(frozen=True)
class Fragment:
    """One ownership unit: a group's variables, or a host's."""

    name: str
    design: dict
    #: devices that see it
    scope: frozenset[str]


def _fragments(inv: Inventory, devices: frozenset[str]) -> list[Fragment]:
    """Every fragment that reaches a device, in Ansible's precedence order.

    `all` first, then groups by (depth, name), then host variables. That is a
    global total order, so restricting one flat list per device reproduces that
    device's view -- which is why `spec.requires` can be a single list.
    """
    depth = inv.depth
    ordered = sorted(
        (g for g, v in inv.group_vars.items() if v),
        key=lambda g: (depth.get(g, 0), g),
    )
    out = [
        Fragment(group, inv.group_vars[group],
                 frozenset(devices if group == ALL_GROUP else inv.members(group) & devices))
        for group in ordered
    ]
    out += [
        Fragment(host, inv.host_vars[host], frozenset({host}))
        for host in sorted(inv.host_vars)
        if host in devices and inv.host_vars[host]
    ]
    return out


def _winners(fragments: list[Fragment]) -> dict[tuple[int, str], set[str]]:
    """``(fragment index, key) -> devices where that fragment's value survives``.

    Layering is last-wins, so a fragment's own value is only observable on the
    devices no later fragment overrides it for. That is exactly where a
    templated value may be read back from the play's output.
    """
    last: dict[str, dict[str, int]] = {}
    for index, fragment in enumerate(fragments):
        for device in fragment.scope:
            for key in fragment.design:
                last.setdefault(device, {})[key] = index
    out: dict[tuple[int, str], set[str]] = {}
    for device, keys in last.items():
        for key, index in keys.items():
            out.setdefault((index, key), set()).add(device)
    return out


def templated(fragments: list[Fragment], hostvars: dict[str, dict]) -> list[Fragment]:
    """Replace each fragment value with what the play actually produced.

    An XR has no templating engine and no playbook directory, so whatever
    Ansible resolves at play time has to be resolved before the value is
    written down. The trigger is **not** "does this look like Jinja" -- it is
    "did the play produce something else", which needs no pattern and catches
    every mechanism at once:

    * Jinja -- `{{ spine_bgp_defaults }}` reaches pyavd as a `Str` where AVD
      wants a `List`;
    * ansible-vault -- ⚠ `ansible-inventory` does **not** decrypt. Both `--list`
      and `--export` emit `{"__ansible_vault": "$ANSIBLE_VAULT;1.1;AES256..."}`,
      which looks resolved in a diff and is not. A play decrypts it.

    Two rules keep it honest:

    * a value is only read back on devices where **this** fragment wins the key
      (:func:`_winners`); reading it off a device some later fragment overrode
      would copy the wrong fragment's value;
    * measured over AVD's whole corpus, no group-level value resolves to
      different things on different devices. An input XR carries one value for
      many devices, so if that ever stops holding the fragment is not
      expressible and this refuses rather than picking one.
    """
    winners = _winners(fragments)
    out: list[Fragment] = []
    for index, fragment in enumerate(fragments):
        design = dict(fragment.design)
        for key, value in fragment.design.items():
            raw = json.dumps(value, sort_keys=True, default=str)
            seen: dict[str, object] = {}
            for device in sorted(winners.get((index, key), ())):
                if key in hostvars.get(device, {}):
                    seen[json.dumps(hostvars[device][key], sort_keys=True, default=str)] = (
                        hostvars[device][key]
                    )
            if len(seen) > 1:
                raise MigrationError(
                    f"{fragment.name}.{key} resolves to {len(seen)} different values "
                    f"across the devices that see it; one input XR carries one value"
                )
            if seen and next(iter(seen)) != raw:
                design[key] = next(iter(seen.values()))
        out.append(Fragment(fragment.name, design, fragment.scope))
    return out


def layer(fragments: list[Fragment], devices: frozenset[str]) -> dict[str, dict]:
    """Apply the fragments in order -- Ansible's ``hash_behaviour=replace``."""
    out: dict[str, dict] = {device: {} for device in devices}
    for fragment in fragments:
        for device in fragment.scope:
            out[device].update(fragment.design)
    return out


def _node_owner(fragments: list[Fragment], devices: frozenset[str],
                vocabulary: Vocabulary) -> dict[str, str]:
    """Which fragment declares each device: the narrowest one with node content.

    "Narrowest" is last in precedence order, which is what depth already sorts
    by. A device typed only through `default_node_types` matches no fragment and
    is left for the caller to place.
    """
    owner: dict[str, str] = {}
    for fragment in fragments:
        parts = by_kind(fragment.design, vocabulary)
        if "NodeSet" not in parts:
            continue
        named = hosts_in_blocks(fragment.design) & devices
        for device in fragment.scope | named:
            if device in devices:
                owner[device] = fragment.name
    return owner


def _unique(name: str, taken: set[str]) -> str:
    candidate, n = name, 2
    while candidate in taken:
        candidate, n = f"{name}-{n}", n + 1
    taken.add(candidate)
    return candidate


def _applies_to(inp: Input, scope: frozenset[str], devices: frozenset[str],
                declared_by: dict[str, set[str]]) -> None:
    """Say which devices see the input, as narrowly as it can be said.

    Silence means the whole fabric -- except on a NodeSet, where it means the
    devices it declares. Both defaults carry the common case, so `appliesTo`
    appears only where the answer is unusual: a DC-wide node block seen by more
    devices than it declares.
    """
    if inp.kind == "NodeSet" and scope == set(inp.declares):
        return
    if scope == devices:
        inp.all_devices = True
        return
    cover = sorted(n for n, hosts in declared_by.items() if hosts and hosts <= scope)
    covered: set[str] = set()
    for name in cover:
        covered |= declared_by[name]
    if covered == scope:
        inp.node_sets = cover
    else:
        inp.hosts = sorted(scope)


def _inputs(fragments: list[Fragment], devices: frozenset[str],
            vocabulary: Vocabulary) -> list[Input]:
    """Every input XR for one fabric, in precedence order.

    Two passes, because `appliesTo` may name a NodeSet that comes later in the
    order: a group at depth 1 is commonly scoped to node sets defined at depth 2.
    """
    owner = _node_owner(fragments, devices, vocabulary)
    declares: dict[str, set[str]] = {}
    for device, name in owner.items():
        declares.setdefault(name, set()).add(device)

    taken: set[str] = set()
    declared_by: dict[str, set[str]] = {}
    planned: list[tuple[Fragment, Input]] = []

    # A device no fragment types is reached by `default_node_types`, by pattern.
    # It still has to exist, so give it a NodeSet of its own, named after the
    # narrowest fragment that reaches it.
    orphans: dict[str, set[str]] = {}
    for device in sorted(devices - set(owner)):
        reaching = [f.name for f in fragments if device in f.scope]
        orphans.setdefault(reaching[-1] if reaching else device, set()).add(device)
    for base, hosts in sorted(orphans.items()):
        name = _unique(f"{slug(base)}-devices", taken)
        inp = Input(name, "NodeSet", {}, declares=sorted(hosts))
        declared_by[name] = set(hosts)
        planned.append((Fragment(base, {}, frozenset(hosts)), inp))

    for fragment in fragments:
        parts = by_kind(fragment.design, vocabulary)
        if not parts:
            continue
        base = slug(fragment.name)
        for kind, design in parts.items():
            plain = len(parts) == 1 or kind == "NodeSet"
            name = _unique(base if plain else f"{base}-{SUFFIX[kind]}", taken)
            inp = Input(name=name, kind=kind, design=design)
            if kind == "NodeSet":
                inp.declares = sorted(declares.get(fragment.name, ()))
                declared_by[name] = set(inp.declares)
            if not fragment.scope and not inp.declares:
                # Reaches nobody -- and "nobody" is not expressible in
                # appliesTo, where silence means everybody. Emitting it would
                # invert its meaning.
                taken.discard(name)
                declared_by.pop(name, None)
                continue
            planned.append((fragment, inp))

    for fragment, inp in planned:
        _applies_to(inp, fragment.scope, devices, declared_by)
    return [inp for _, inp in planned]


def _fabric_name(root: Path, play: Play, many: bool, taken: set[str]) -> str:
    """A name per play, and never the same one twice.

    ⚠ The pattern does not always tell two plays apart:
    `eos_designs-twodc-5stage-clos` runs eos_designs twice over the same
    `hosts: TWODC_5STAGE_CLOS`, so naming by pattern gave both fabrics one name —
    and `--emit` wrote one file, the second silently overwriting the first.
    """
    if not many:
        return _unique(slug(root.name), taken)
    base = slug(f"{root.name}-{play.pattern}") or slug(root.name)
    if base in taken:
        base = slug(f"{base}-{play.playbook.removesuffix('.yml')}-{play.index}")
    return _unique(base, taken)


def migrate(root: Path, collections: Path | None = None, inventory: Path | None = None,
            inv: Inventory | None = None, drop_descriptions: bool = False) -> list[Fabric]:
    """Translate every eos_designs play under ``root`` into a Fabric.

    Raises :class:`MigrationError` when the layered fragments disagree with the
    per-device variables Ansible reports. That is the whole safety property: the
    only rule this module supplies is the group order, and it is never allowed
    to be wrong silently.
    """
    root = Path(root).resolve()
    # Reading an inventory costs two subprocesses; a caller that already has one
    # (a harness measuring the whole corpus) may hand it over.
    inv = inv if inv is not None else read_inventory(root, inventory, collections)
    found = design_plays(root, collections, inventory)
    if not found:
        # No play runs eos_designs -- eos_cli_config_gen scenarios carry
        # structured config directly and render at the device layer. Say so
        # rather than inventing a fabric out of the inventory.
        raise MigrationError(
            f"{root.name}: no play runs eos_designs "
            f"({len(plays(root, collections, inventory))} plays seen)"
        )

    vocabulary = Vocabulary.default()
    fabrics: list[Fabric] = []
    named: set[str] = set()
    for play in found:
        devices = frozenset(play.hosts)
        if not devices:
            continue
        fragments = _fragments(inv, devices)
        if inv.templated:
            fragments = templated(fragments, inv.hostvars)

        expected = {d: inv.hostvars.get(d, {}) for d in devices}
        if layer(fragments, devices) != expected:
            differing = sorted(
                f"{d}.{k}" for d in devices
                for k in set(expected[d]) | set(layer(fragments, devices)[d])
                if expected[d].get(k) != layer(fragments, devices)[d].get(k)
            )
            raise MigrationError(
                f"{root.name} play #{play.index}: layering the fragments does not "
                f"reproduce what Ansible reports ({len(differing)} differences, "
                f"first: {', '.join(differing[:5])})"
            )

        fabric = Fabric(
            name=_fabric_name(root, play, len(found) > 1, named),
            devices=tuple(sorted(devices)),
            inputs=_inputs(fragments, devices, vocabulary),
            play=play,
        )
        _fabric_name_of(fabric, inv.hostvars)
        _report_play_vars(fabric, root, play)
        # Only now, with the translation proven faithful. Dropping anything
        # before the comparison above would weaken the one gate this module has.
        _report_unsupported(fabric, drop_descriptions)
        fabrics.append(fabric)
    return fabrics


def _pooled_ids(design: dict) -> dict:
    node_id = (design.get("fabric_numbering") or {}).get("node_id")
    if isinstance(node_id, dict) and node_id.get("algorithm") == "pool_manager":
        return node_id
    return {}


def _report_play_vars(fabric: Fabric, root: Path, play: Play) -> None:
    """Variables set on the play itself, which no input XR carries.

    ⚠ A source `ansible-inventory` cannot see, and one that changes the render:
    `eos_designs-twodc-5stage-clos` runs eos_designs twice over the same hosts
    and the second play sets `avd_digital_twin_mode: true`, producing a
    different config into a different golden directory. Migrated without it, the
    two fabrics come out identical and one of them is wrong.

    **Reported, not carried.** Play vars outrank group and host vars, but the
    oracle this migration checks itself against is per-host hostvars, which do
    not include them -- so carrying them would silently weaken the one gate this
    module has. Whoever migrates such a play adds the keys to an input by hand.
    """
    import yaml as _yaml

    playbook = root / play.playbook
    try:
        document = _yaml.safe_load(playbook.read_text())
    except (OSError, _yaml.YAMLError):
        return
    if not isinstance(document, list) or play.index > len(document):
        return
    variables = document[play.index - 1].get("vars") if isinstance(
        document[play.index - 1], dict) else None
    if isinstance(variables, dict) and variables:
        fabric.notes.append(
            f"{play.playbook} play #{play.index} sets {len(variables)} variable(s) on the "
            f"play itself ({', '.join(sorted(variables))}); play vars outrank group and "
            f"host vars and are NOT carried into any input"
        )


def _report_unsupported(fabric: Fabric, drop_descriptions: bool) -> None:
    """Note -- and optionally drop -- what pyavd will not honour."""
    # State, not settings. An inventory already running `pool_manager` keeps its
    # node IDs in a file AVD generated; this translates the *setting* and leaves
    # the *assignments* behind. Applied to a fabric that is already deployed,
    # that renumbers every device -- and a render reaches a switch as a full
    # configuration replacement.
    for inp in fabric.inputs:
        pooled = _pooled_ids(inp.design)
        if pooled:
            where = pooled.get("pools_file") or "<root_dir>/intended/data/<fabric>-ids.yml"
            fabric.notes.append(
                f"node IDs come from a pool; its assignments live in {where} and do "
                f"NOT travel with this migration. Seed them into the fabric "
                f"(spec.nodeIdPool.seedConfigMapName) or every device is renumbered"
            )
            break

    if drop_descriptions:
        dropped = [
            f"{inp.name}.{path}"
            for inp in fabric.inputs
            for path in drop_description_templates(inp.design)
        ]
        if dropped:
            fabric.notes.append(
                f"dropped {len(dropped)} interface-description template(s); AVD's own "
                f"descriptions apply instead: {', '.join(dropped[:3])}"
                + (" ..." if len(dropped) > 3 else "")
            )

    found: dict[str, list[str]] = {}
    for inp in fabric.inputs:
        for owner, paths in unsupported(inp.design).items():
            found.setdefault(owner, []).extend(f"{inp.name}.{p}" for p in paths)
    for owner, paths in sorted(found.items()):
        what = (
            "descriptions only -- `--drop-description-templates` renders without them"
            if owner == COSMETIC
            else "this decides addresses, not wording; dropping it would emit a "
                 "different network"
            if owner == "ip_addressing"
            else "custom code; a function image is immutable and loads no arbitrary Python"
            if owner == "python_module"
            else "pyavd implements no Jinja templating"
        )
        fabric.notes.append(f"{len(paths)} x {owner} pyavd cannot honour ({what}): {paths[0]}")


#: A value pyavd cannot honour, and whether losing it is cosmetic.
#:
#: pyavd implements no Jinja templating: `get_device_structured_config` passes
#: `templar=None` and the call raises `NotImplementedError`. AVD's own Ansible
#: action plugin reaches into pyavd's internal API precisely to hand in a
#: templar built from Ansible's own -- which needs Ansible at render time, and
#: there is none in a cluster.
#:
#: So a template path cannot travel. What differs is what is lost with it:
#: an `interface_descriptions` template decides a `description` string, while an
#: `ip_addressing` template decides an address. Measured on
#: `evpn_underlay_ebgp_overlay_ebgp`: dropping its description templates renders
#: all 16 devices and differs from AVD's golden in 168 places, **all of them a
#: `description` field**. Dropping an addressing template would silently emit a
#: different network.
COSMETIC = "interface_descriptions"


def unsupported(design: dict) -> dict[str, list[str]]:
    """Values in a fragment that pyavd cannot honour, by the key that owns them.

    Detected by shape rather than by a list of key names: a Jinja template is a
    string naming a `.j2` file, and custom logic is a `python_module`. Both are
    code paths into a filesystem that a cluster does not have.
    """
    found: dict[str, list[str]] = {}

    def walk(node: object, path: list[str], owner: str | None) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, [*path, str(key)],
                     str(key) if str(key) in (COSMETIC, "ip_addressing") else owner)
                if key == "python_module" and isinstance(value, str):
                    found.setdefault("python_module", []).append(".".join([*path, str(key)]))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, [*path, f"[{index}]"], owner)
        elif isinstance(node, str) and node.endswith(".j2"):
            found.setdefault(owner or "template", []).append(".".join(path))

    walk(design, [], None)
    return found


def drop_description_templates(design: dict) -> list[str]:
    """Remove interface-description templates so the rest can render.

    Returns the paths removed, which the caller is expected to report -- a
    migration that quietly drops input is worse than one that refuses, because
    the render is pushed to a device as a full configuration replacement.

    AVD falls back to its own built-in descriptions, so what is lost is exactly
    the wording. Nothing else in the document depends on it.
    """
    removed: list[str] = []

    def walk(node: object, path: list[str]) -> None:
        if isinstance(node, dict):
            for key in list(node):
                value = node[key]
                if key == COSMETIC and isinstance(value, dict):
                    gone = [n for n, v in value.items() if isinstance(v, str) and v.endswith(".j2")]
                    for name in gone:
                        del value[name]
                        removed.append(".".join([*path, key, name]))
                    if not value:
                        del node[key]
                    continue
                walk(value, [*path, str(key)])
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, [*path, f"[{index}]"])

    walk(design, [])
    return removed


def _fabric_name_of(fabric: Fabric, hostvars: dict[str, dict]) -> None:
    """Fill ``spec.fabricName`` and note it when the devices disagree.

    `fn.py` writes one `fabric_name` into every device's document, so a Fabric
    has exactly one. Ansible does not: `eos_designs_unit_tests` runs one
    eos_designs play over 501 devices carrying **six** different `fabric_name`
    values. That is not expressible, and quietly picking one would render every
    device under a name AVD never gave it -- so it is named as a note.
    """
    seen: dict[str, int] = {}
    for device in fabric.devices:
        name = hostvars.get(device, {}).get("fabric_name")
        if isinstance(name, str) and name:
            seen[name] = seen.get(name, 0) + 1
    if not seen:
        return
    fabric.fabric_name = max(seen, key=lambda n: seen[n])
    if len(seen) > 1:
        others = ", ".join(f"{n} ({c})" for n, c in sorted(seen.items(), key=lambda kv: -kv[1]))
        fabric.notes.append(
            f"devices disagree on fabric_name and a Fabric carries one: {others}"
        )


def to_manifests(fabric: Fabric, namespace: str = "default") -> list[dict]:
    """The Fabric and its inputs, as manifests in ``spec.requires`` order."""
    group = "avd.netclab.dev/v1alpha1"
    out: list[dict] = []
    for inp in fabric.inputs:
        spec: dict = {"design": inp.design}
        if inp.declares:
            spec["declares"] = inp.declares
        applies: dict = {}
        if inp.all_devices:
            applies["all"] = True
        if inp.node_sets:
            applies["nodeSets"] = inp.node_sets
        if inp.hosts:
            applies["hosts"] = inp.hosts
        if inp.match_hostnames:
            applies["matchHostnames"] = inp.match_hostnames
        if applies:
            spec["appliesTo"] = applies
        out.append({
            "apiVersion": group, "kind": inp.kind,
            "metadata": {"name": inp.name, "namespace": namespace},
            "spec": spec,
        })
    out.append({
        "apiVersion": group, "kind": "Fabric",
        "metadata": {"name": fabric.name, "namespace": namespace},
        "spec": {
            "fabricName": fabric.fabric_name or fabric.name,
            "requires": [
                {"kind": i.kind, "name": i.name, "namespace": namespace}
                for i in fabric.inputs
            ],
        },
    })
    return out


def _discover(root: Path) -> list[Path]:
    """Every inventory under a directory, in either layout AVD ships."""
    if (root / "inventory.yml").is_file() or (root / "inventory" / "hosts.yml").is_file():
        return [root]
    return sorted(
        d for d in root.iterdir()
        if d.is_dir()
        and ((d / "inventory.yml").is_file() or (d / "inventory" / "hosts.yml").is_file())
    )


def main() -> int:
    """``avd-migrate [ROOT ...] [--emit DIR] [--namespace NS]``"""
    import argparse

    import yaml

    parser = argparse.ArgumentParser(
        prog="avd-migrate",
        description="Translate AVD Ansible inventories into Fabric and input XRs.",
    )
    parser.add_argument("roots", nargs="*", type=Path,
                        default=[Path("avd/ansible_collections/arista/avd/examples")])
    parser.add_argument("--emit", type=Path, help="write manifests under this directory")
    parser.add_argument("--namespace", default="default")
    parser.add_argument(
        "--drop-description-templates", action="store_true",
        help="drop interface-description templates so the rest renders. Cosmetic "
             "by measurement -- on AVD's own corpus it costs `description` fields "
             "and nothing else. Addressing templates are never dropped.",
    )
    parser.add_argument("--collections", type=Path,
                        default=Path("avd"), help="ANSIBLE_COLLECTIONS_PATH")
    args = parser.parse_args()

    collections = args.collections.resolve() if args.collections.is_dir() else None
    failures = 0
    for top in args.roots:
        for root in _discover(top):
            try:
                fabrics = migrate(root, collections=collections,
                                  drop_descriptions=args.drop_description_templates)
            except MigrationError as err:
                print(f"[skip] {root.name:38s} {str(err).split(': ', 1)[-1]}")
                continue
            except Exception as err:  # noqa: BLE001 - surface any ansible failure
                failures += 1
                print(f"[FAIL] {root.name:38s} {type(err).__name__}: "
                      f"{str(err).splitlines()[0][:70]}")
                continue
            for fabric in fabrics:
                kinds = {k: sum(1 for i in fabric.inputs if i.kind == k) for k in KINDS}
                shape = " ".join(f"{k[:-3] if k.endswith('Set') else k}={v}"
                                 for k, v in kinds.items() if v)
                print(f"[ ok ] {fabric.name:38s} {len(fabric.devices):4d} devices  {shape}")
                for note in fabric.notes:
                    print(f"       ! {note}")
                if args.emit:
                    target = args.emit / f"{fabric.name}.yaml"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(yaml.safe_dump_all(
                        to_manifests(fabric, args.namespace), sort_keys=False))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
