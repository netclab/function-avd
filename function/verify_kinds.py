"""Prove the input-kind model reproduces Ansible's variable resolution.

Translates an AVD inventory into input XRs, resolves them with
:func:`function.kinds.resolve` -- which never looks at the inventory again --
and compares the resulting hostvars against faithfully reproduced Ansible.

Byte equality is the assertion. It is stricter than necessary (a hostvar AVD
never reads cannot change a rendered config) and that is deliberate: it fails
before a render can hide a difference.

The translation reads the inventory; the resolution does not. Only the second
half is the model. What it establishes:

* ``spec.requires`` order reproduces Ansible precedence. Ansible sorts groups by
  (depth, name), which is a *global* total order, so one flat list restricted to
  the inputs that apply to a device reproduces that device's view.
* ``appliesTo`` reproduces group membership without groups.
* One device list -- what NodeSets declare -- reproduces the inventory.

Usage:
    uv run avd-verify-kinds [EXAMPLE_DIR ...]   # default: every bundled example
"""

from __future__ import annotations

import sys
from pathlib import Path

from .ansible_inputs import (
    ALL_GROUP,
    AnsibleInventory,
    _load_group_vars,
    _strip_ansible_keys,
    _yaml_load,
)
from .kinds import Input, classify, hosts_in_blocks, is_node_block, resolve

EXAMPLES_ROOT = Path("avd/ansible_collections/arista/avd/examples")
MOLECULE_ROOT = Path("avd/ansible_collections/arista/avd/extensions/molecule")

# Examples this harness cannot translate, with the reason. Expected-failure
# semantics, as in verify_xr: one that starts passing IS reported, so a deferral
# can never rot silently.
DEFERRED: dict[str, str] = {}


def inline_host_vars(inventory_file: Path) -> dict[str, dict]:
    """Host variables written straight into the inventory.

    ``AnsibleInventory`` iterates the keys under ``hosts:`` and drops the values,
    so ``dc1-spine1: {type: spine}`` is invisible to it. Several molecule
    scenarios declare device types that way and nothing else does.
    """
    found: dict[str, dict] = {}

    def walk(node: object) -> None:
        if not isinstance(node, dict):
            return
        for host, hostvars in (node.get("hosts") or {}).items():
            if isinstance(hostvars, dict):
                stripped = _strip_ansible_keys(hostvars)
                if stripped:
                    found.setdefault(host, {}).update(stripped)
        for child in (node.get("children") or {}).values():
            walk(child)

    for value in (_yaml_load(inventory_file) or {}).values():
        walk(value)
    return found


def _layout(root: Path) -> tuple[Path, Path]:
    """(directory holding group_vars, inventory file) for either layout."""
    if (root / "inventory.yml").is_file():
        return root, root / "inventory.yml"
    return root / "inventory", root / "inventory" / "hosts.yml"


def ansible_hostvars(root: Path) -> dict[str, dict]:
    """What ansible-playbook would hand to AVD, from every source it reads."""
    var_dir, inventory_file = _layout(root)
    inventory = AnsibleInventory.from_file(inventory_file)
    inline = inline_host_vars(inventory_file)
    host_var_dir = var_dir / "host_vars"
    files = (
        {f.stem: _strip_ansible_keys(_yaml_load(f)) for f in host_var_dir.glob("*.yml")}
        if host_var_dir.is_dir()
        else {}
    )
    cache: dict[str, dict] = {}
    out: dict[str, dict] = {}
    for host in sorted(inventory.hosts()):
        hostvars: dict = {}
        for group in inventory.groups_for_host(host):
            if group not in cache:
                cache[group] = _load_group_vars(var_dir / "group_vars", group)
            hostvars.update(cache[group])
        hostvars = _strip_ansible_keys(hostvars)
        hostvars.update(inline.get(host, {}))
        hostvars.update(files.get(host, {}))
        out[host] = hostvars
    return out


def inputs_from_inventory(root: Path) -> list[Input]:
    """Translate an AVD inventory into ordered input XRs -- a migration.

    Each group_vars file becomes one or two inputs: a ``NodeSet`` for its
    node-type blocks and one input for the rest. The split is not cosmetic --
    the two halves have different scopes whenever a block names fewer devices
    than the group holds, which is what a 5-stage CLOS does.
    """
    var_dir, inventory_file = _layout(root)
    inventory = AnsibleInventory.from_file(inventory_file)
    group_var_dir = var_dir / "group_vars"
    every_device = inventory.hosts()

    groups = []
    if group_var_dir.is_dir():
        named = {p.stem for p in group_var_dir.glob("*.yml")} | {
            d.name for d in group_var_dir.iterdir() if d.is_dir()
        }
        groups = [g for g in named if g in inventory.depth or g == ALL_GROUP]
    # Ansible precedence: `all` first, then (depth, name). This is requires order.
    ordered = sorted(groups, key=lambda g: (inventory.depth.get(g, 0), g))

    def group_devices(group: str) -> set[str]:
        if group == ALL_GROUP:
            return set(every_device)
        return {h for h in every_device if group in inventory.groups_for_host(h)}

    designs = {g: _strip_ansible_keys(_load_group_vars(group_var_dir, g)) for g in ordered}
    declared_by: dict[str, set[str]] = {}
    for group, design in designs.items():
        blocks = {k: v for k, v in design.items() if is_node_block(v)}
        if blocks:
            declared_by[group] = hosts_in_blocks(blocks) & set(every_device)

    # Devices no block mentions -- fixtures with no node-type blocks at all, and
    # hosts that only the inventory knows about. Declared at the narrowest group
    # holding each one rather than in a single fabric-wide NodeSet, so the
    # resulting NodeSets line up with real groups and other inputs can name them.
    device_sets: dict[str, set[str]] = {}
    for host in sorted(set(every_device) - set().union(*declared_by.values() or [set()])):
        deepest = inventory.groups_for_host(host)[-1]  # already (depth, name) sorted
        device_sets.setdefault(f"{deepest}-devices", set()).add(host)
    declared_by.update(device_sets)

    inputs = [
        Input(name, "NodeSet", {}, node_sets=[name], declares=sorted(hosts))
        for name, hosts in sorted(device_sets.items())
    ]

    for group in ordered:
        design = designs[group]
        if not design:
            continue
        blocks = {k: v for k, v in design.items() if is_node_block(v)}
        rest = {k: v for k, v in design.items() if not is_node_block(v)}
        want = group_devices(group)

        def scoped(name: str, kind: str, payload: dict, want: set[str] = want) -> Input:
            inp = Input(name=name, kind=kind, design=payload)
            if want == set(every_device):
                inp.all_devices = True
            else:
                cover = [n for n, hs in declared_by.items() if hs and hs <= want]
                covered: set[str] = set()
                for n in cover:
                    covered |= declared_by[n]
                if covered == want:
                    inp.node_sets = sorted(cover)
                else:
                    # No union of NodeSets is this group -- name the devices.
                    inp.hosts = sorted(want)
            return inp

        if blocks:
            node_set = scoped(group, "NodeSet", blocks)
            node_set.declares = sorted(declared_by[group])
            inputs.append(node_set)
        if rest:
            inputs.append(scoped(f"{group}-settings" if blocks else group, classify(rest), rest))

    # host_vars last, as Ansible does: inventory inline first, then files.
    for host, design in sorted(inline_host_vars(inventory_file).items()):
        inputs.append(Input(f"{host}-inline", classify(design), design, hosts=[host]))
    host_var_dir = var_dir / "host_vars"
    if host_var_dir.is_dir():
        for f in sorted(host_var_dir.glob("*.yml")):
            design = _strip_ansible_keys(_yaml_load(f))
            if design:
                inputs.append(Input(f.stem, classify(design), design, hosts=[f.stem]))
    return inputs


def verify_one(root: Path) -> tuple[str, int]:
    """Return (status, difference count). status in {ok, differs, error}."""
    try:
        from_kinds = resolve(inputs_from_inventory(root))
        from_ansible = ansible_hostvars(root)
    except Exception as err:  # noqa: BLE001 - surface any translation failure
        return f"error: {type(err).__name__}: {str(err)[:70]}", -1

    notes: list[str] = []
    for host in sorted(set(from_kinds) | set(from_ansible)):
        if host not in from_kinds:
            notes.append(f"{host}: missing")
            continue
        if host not in from_ansible:
            notes.append(f"{host}: extra")
            continue
        a, b = from_kinds[host], from_ansible[host]
        notes += [f"{host}.{k}" for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]
    if notes:
        return f"differs ({len(notes)}): {', '.join(notes[:3])}", len(notes)
    return "ok", 0


def _discover(root: Path) -> list[Path]:
    return sorted(d for d in root.iterdir() if (d / "inventory.yml").is_file())


def _discover_molecule(root: Path = MOLECULE_ROOT) -> list[Path]:
    """Molecule scenarios carrying an inventory of their own.

    The wider corpus, and the harder one: these reach 501 devices and lean on
    inventory-inline host vars, which the examples barely use.
    """
    if not root.is_dir():
        return []
    return sorted(
        d
        for d in root.iterdir()
        if (d / "inventory" / "hosts.yml").is_file() and (d / "inventory" / "group_vars").is_dir()
    )


def main() -> int:
    roots = [Path(a) for a in sys.argv[1:]] or _discover(EXAMPLES_ROOT)
    failures = deferred = 0
    for root in roots:
        status, _ = verify_one(root)
        ok = status == "ok"
        reason = DEFERRED.get(root.name)
        if reason and not ok:
            mark, deferred = "DEFER", deferred + 1
            status = f"deferred: {reason}"
        elif reason and ok:
            mark, failures = "XPASS", failures + 1
            status = "resolves now -- remove from DEFERRED"
        elif ok:
            mark = "OK  "
        else:
            mark, failures = "FAIL", failures + 1
        print(f"[{mark}] {root.name:26s} {status}")
    expected = len(roots) - deferred
    print(f"\n{expected - failures}/{expected} inventories resolve identically to Ansible.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
