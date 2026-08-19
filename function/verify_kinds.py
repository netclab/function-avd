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

import yaml

from .ansible_inputs import (
    ALL_GROUP,
    AnsibleInventory,
    _load_group_vars,
    _strip_ansible_keys,
    _yaml_load,
)
from .kinds import KINDS, Input, classify, hosts_in_blocks, is_node_block, resolve

EXAMPLES_ROOT = Path("avd/ansible_collections/arista/avd/examples")
MOLECULE_ROOT = Path("avd/ansible_collections/arista/avd/extensions/molecule")

# Examples this harness cannot translate, with the reason. Expected-failure
# semantics, as in verify_xr: one that starts passing IS reported, so a deferral
# can never rot silently.
DEFERRED: dict[str, str] = {}

# The same, for --render. Resolution and rendering fail for different reasons:
# resolution is settled, rendering still runs into AVD features this path does
# not carry yet.
DEFERRED_RENDER: dict[str, str] = {
    "cv-pathfinder": "ansible-vault secrets; credentials cannot live in an XR spec",
}


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


def _play_targets(root: Path) -> tuple[set[str], set[str]]:
    """``(what every play targets, what the eos_designs plays target)``.

    Both, because they differ and only the second is the fabric. A directory's
    playbooks also carry plays that are not a render: molecule's ``create.yml``
    makes output folders, ``howto`` has a ``localhost`` play, and
    ``deploy.yml`` pushes what was already built.
    """
    every: set[str] = set()
    designs: set[str] = set()
    for playbook in sorted(root.glob("*.yml")) + sorted(root.glob("converge.yml")):
        if playbook.name == "inventory.yml":
            continue
        document = _yaml_load(playbook)
        if not isinstance(document, list):
            continue
        for play in document:
            if not isinstance(play, dict) or "hosts" not in play:
                continue
            every.add(str(play["hosts"]))
            # The plays name the role outright: `arista.avd.eos_designs`.
            if "eos_designs" in yaml.safe_dump(play):
                designs.add(str(play["hosts"]))
    return every, designs


def play_hosts(root: Path, inventory: AnsibleInventory) -> list[str]:
    """The hosts eos_designs is run on -- which is what a fabric's devices are.

    **An inventory is not a device list.** Ansible has two lists and AVD renders
    the second: `cv-pathfinder`'s `build.yml` says `hosts: WAN`, while its
    inventory also holds `cloudvision` -- the CloudVision API server, a host so
    that `cv_deploy` can reach it, and not a switch. Declaring it composes a
    Device for it, and a device with no node type fails the whole fabric's
    render, not just its own.

    A Fabric *is* the play, so this is the list `declares` reproduces. Reading it
    is not a heuristic: it is the statement AVD itself acts on.

    Two fallbacks, in order, each for a real case in the corpus:

    * no play runs eos_designs -- the three `eos_cli_config_gen` scenarios carry
      structured config directly. Their hosts are still devices, so fall back to
      what any play targets rather than declaring none of them;
    * no playbook at all -- an inventory handed to the migration on its own is
      still worth translating, so fall back to the whole inventory.
    """
    hosts = inventory.hosts()
    every, designs = _play_targets(root)
    targets = designs or every
    if not targets or "all" in targets:
        return hosts
    return [h for h in hosts if targets & (set(inventory.groups_for_host(h)) | {h})]


def ansible_hostvars(root: Path) -> dict[str, dict]:
    """What ansible-playbook would hand to AVD, from every source it reads.

    The play's hosts, not the inventory's -- see :func:`play_hosts`. Both this
    and the migration restrict to the same set, so the comparison stays an
    equality over what AVD is actually given.
    """
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
    for host in sorted(play_hosts(root, inventory)):
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


_SUFFIX = {
    "NetworkServiceSet": "services",
    "ConnectedEndpointSet": "endpoints",
    "SettingSet": "settings",
}


def _by_category(base: str, design: dict) -> list[tuple[str, str, dict]]:
    """Partition one group's vars into ``(name, kind, design)`` per category.

    **Merge first, split after** -- the order matters and it is Ansible's.
    A ``group_vars`` *directory* is merged into one namespace for the group, so
    two files setting the same top-level key resolve alphabetically-last-wins
    and never coexist. Splitting by file instead would preserve a boundary
    Ansible does not keep, and emit two inputs claiming one key.

    Splitting by category afterwards costs nothing and is what the kinds are
    for: `cv-pathfinder`'s `group_vars/WAN/` holds four files the author already
    separated -- settings, interface profiles, management, tenants -- which
    merged into one fragment whose single `tenants` key decided the kind for the
    other 21. Ownership only; `resolve` never reads a kind, and the parts are
    disjoint and consecutive, so the hostvars are unchanged either way.
    """
    buckets: dict[str, dict] = {}
    for key, value in design.items():
        buckets.setdefault(classify({key: value}), {})[key] = value
    parts = [(kind, buckets[kind]) for kind in KINDS if kind in buckets]
    return [
        (base if len(parts) == 1 or kind == "NodeSet" else f"{base}-{_SUFFIX[kind]}", kind, payload)
        for kind, payload in parts
    ]


def inputs_from_inventory(root: Path) -> list[Input]:
    """Translate an AVD inventory into ordered input XRs -- a migration.

    Each group's vars become one input per content category: a ``NodeSet`` for
    its node-type blocks, and one each for services, endpoints and settings.
    The node-type split is not cosmetic -- the two halves have different scopes
    whenever a block names fewer devices than the group holds, which is what a
    5-stage CLOS does. The rest is ownership: see :func:`_by_category`.
    """
    var_dir, inventory_file = _layout(root)
    inventory = AnsibleInventory.from_file(inventory_file)
    group_var_dir = var_dir / "group_vars"
    # The play's hosts, not the inventory's -- see play_hosts.
    every_device = play_hosts(root, inventory)

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
        Input(name, "NodeSet", {}, declares=sorted(hosts))
        for name, hosts in sorted(device_sets.items())
    ]

    for group in ordered:
        design = designs[group]
        if not design:
            continue
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

        for name, kind, payload in _by_category(group, design):
            inp = scoped(name, kind, payload)
            if kind == "NodeSet":
                inp.declares = sorted(declared_by[group])
            # A group with no host in the play, whose blocks declare nothing
            # either, reaches nobody -- and "reaches nobody" is not expressible
            # in appliesTo, where saying nothing means every device. Emitting it
            # would invert its meaning. Only reachable since the device list
            # became the play's rather than the inventory's.
            if not want and not (kind == "NodeSet" and inp.declares):
                continue
            if kind == "NodeSet":
                if want == set(inp.declares):
                    # The default -- a NodeSet is seen by what it declares.
                    # Saying it again would make every NodeSet name itself.
                    inp.all_devices = False
                    inp.node_sets = []
                    inp.hosts = []
            inputs.append(inp)

    # host_vars last, as Ansible does: inventory inline first, then files.
    for host, design in sorted(inline_host_vars(inventory_file).items()):
        for name, kind, payload in _by_category(f"{host}-inline", design):
            inputs.append(Input(name, kind, payload, hosts=[host]))
    host_var_dir = var_dir / "host_vars"
    if host_var_dir.is_dir():
        for f in sorted(host_var_dir.glob("*.yml")):
            design = _strip_ansible_keys(_yaml_load(f))
            for name, kind, payload in _by_category(f.stem, design):
                inputs.append(Input(name, kind, payload, hosts=[f.stem]))
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


def render_one(root: Path) -> tuple[str, int]:
    """Render through the kinds path and diff against the checked-in golden.

    This does **not** test the model -- if the hostvars match Ansible, and
    :func:`verify_one` asserts they do, the render must match too. What it tests
    is the pair (this path, this pyavd): an AVD upgrade that changes output slips
    past resolution equivalence and fails here. That is the job `test_xr_fold`
    does today through the fold, and this is its successor -- the fold reaches
    6 of 8 examples, this reaches 7.
    """
    import yaml

    from .engine import render_structured_configs
    from .verify_example import _diff

    golden = root / "intended" / "structured_configs"
    if not golden.is_dir():
        return "no golden", -1
    try:
        rendered = render_structured_configs(resolve(inputs_from_inventory(root)))
    except Exception as err:  # noqa: BLE001 - surface any AVD/render failure
        return f"error: {type(err).__name__}: {str(err)[:70]}", -1

    total = 0
    for hostname in sorted(rendered):
        golden_file = golden / f"{hostname}.yml"
        if not golden_file.is_file():
            continue  # a scenario may render hosts it keeps no golden for
        out: list[str] = []
        _diff(hostname, rendered[hostname], yaml.safe_load(golden_file.read_text()) or {}, out)
        total += len(out)
    return ("ok" if total == 0 else f"diff ({total})"), total


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
    args = [a for a in sys.argv[1:] if a != "--render"]
    rendering = "--render" in sys.argv[1:]
    roots = [Path(a) for a in args] or _discover(EXAMPLES_ROOT)
    check = render_one if rendering else verify_one
    deferrals = DEFERRED_RENDER if rendering else DEFERRED

    failures = deferred = 0
    for root in roots:
        status, _ = check(root)
        ok = status == "ok"
        reason = deferrals.get(root.name)
        if reason and not ok:
            mark, deferred = "DEFER", deferred + 1
            status = f"deferred: {reason}"
        elif reason and ok:
            mark, failures = "XPASS", failures + 1
            status = "passes now -- remove from the DEFERRED map"
        elif ok:
            mark = "OK  "
        else:
            mark, failures = "FAIL", failures + 1
        print(f"[{mark}] {root.name:26s} {status}")

    expected = len(roots) - deferred
    what = "reproduce golden" if rendering else "resolve identically to Ansible"
    print(f"\n{expected - failures}/{expected} inventories {what} ({deferred} deferred).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
