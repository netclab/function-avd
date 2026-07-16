"""Reconstruct pyavd ``all_inputs`` from an AVD Ansible example directory.

This replicates the parts of Ansible we rely on to feed :mod:`pyavd`:

* ``inventory.yml`` provides the group hierarchy and host membership.
* ``group_vars/<GROUP>/*.yml`` provide the layered AVD data model.

Ansible merges group_vars **per host**, in precedence order (``all`` first,
then groups sorted by depth and name, host_vars last), with default
``hash_behaviour = replace`` (top-level keys override wholesale). We reproduce
exactly that so the resulting hostvars equal what ``ansible-playbook`` would
hand to AVD.

The output is ``{hostname: hostvars}`` -- the ``all_inputs`` mapping consumed by
``pyavd.get_avd_facts``.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ALL_GROUP = "all"


class _AnsibleLoader(yaml.SafeLoader):
    """SafeLoader that tolerates Ansible-specific tags (e.g. ``!vault``).

    Vault-encrypted values are kept as opaque strings -- enough to parse the
    file. Reproducing configs that embed them still needs the vault password.
    """


_AnsibleLoader.add_constructor(
    "!vault", lambda loader, node: loader.construct_scalar(node)
)


def _yaml_load(path: Path):
    return yaml.load(path.read_text(), Loader=_AnsibleLoader) or {}


class AnsibleInventory:
    """Group hierarchy + host membership parsed from an ``inventory.yml`` tree."""

    def __init__(self) -> None:
        self.children: dict[str, set[str]] = {ALL_GROUP: set()}
        self.direct_hosts: dict[str, set[str]] = {}
        self.depth: dict[str, int] = {ALL_GROUP: 0}

    @classmethod
    def from_file(cls, inventory_path: Path) -> "AnsibleInventory":
        data = _yaml_load(inventory_path)
        inv = cls()
        if ALL_GROUP in data:
            root = data[ALL_GROUP] or {}
        else:
            # No explicit `all:` root -> every top-level key is an implicit
            # child group of `all` (standard Ansible inventory behaviour).
            root = {"children": {k: v for k, v in data.items() if k != "_meta"}}
        inv._walk(ALL_GROUP, root)
        inv._compute_depths()
        return inv

    def _walk(self, group: str, body: dict | None) -> None:
        body = body or {}
        self.children.setdefault(group, set())
        for host in (body.get("hosts") or {}):
            self.direct_hosts.setdefault(group, set()).add(host)
        for child, child_body in (body.get("children") or {}).items():
            self.children[group].add(child)
            self._walk(child, child_body)

    def _compute_depths(self) -> None:
        # Ansible depth = longest path from `all`. Iterate to a fixpoint.
        for g in self.children:
            self.depth.setdefault(g, 0)
        changed = True
        while changed:
            changed = False
            for parent, kids in self.children.items():
                for kid in kids:
                    d = self.depth[parent] + 1
                    if d > self.depth.get(kid, 0):
                        self.depth[kid] = d
                        changed = True

    def hosts(self) -> set[str]:
        return {h for hs in self.direct_hosts.values() for h in hs}

    def groups_for_host(self, host: str) -> list[str]:
        """All groups the host belongs to (transitively), in Ansible merge order."""
        direct = {g for g, hs in self.direct_hosts.items() if host in hs}
        groups: set[str] = {ALL_GROUP}
        for g in direct:
            groups.add(g)
            groups |= self._ancestors(g)
        return sorted(groups, key=lambda g: (self.depth.get(g, 0), g))

    def _ancestors(self, group: str) -> set[str]:
        parents = {p for p, kids in self.children.items() if group in kids}
        result = set(parents)
        for p in parents:
            result |= self._ancestors(p)
        return result


def _strip_ansible_keys(data: dict) -> dict:
    """Drop Ansible transport vars (ansible_connection, ansible_user, ...).

    They are not part of the AVD data model and will not exist on our XRs.
    """
    return {k: v for k, v in data.items() if not k.startswith("ansible_")}


def _load_group_vars(group_vars_dir: Path, group: str) -> dict:
    """Merge every ``*.yml`` under ``group_vars/<group>/`` (alphabetically)."""
    merged: dict = {}
    dir_path = group_vars_dir / group
    single_file = group_vars_dir / f"{group}.yml"
    files: list[Path] = []
    if dir_path.is_dir():
        files = sorted(dir_path.glob("*.yml")) + sorted(dir_path.glob("*.yaml"))
    elif single_file.is_file():
        files = [single_file]
    for f in files:
        data = _yaml_load(f)
        merged.update(data)  # hash_behaviour = replace
    return merged


def build_all_inputs(example_dir: str | Path) -> dict[str, dict]:
    """Return ``{hostname: hostvars}`` for a single-DC AVD example directory."""
    example_dir = Path(example_dir)
    inventory = AnsibleInventory.from_file(example_dir / "inventory.yml")
    group_vars_dir = example_dir / "group_vars"

    group_cache: dict[str, dict] = {}
    all_inputs: dict[str, dict] = {}
    for host in sorted(inventory.hosts()):
        hostvars: dict = {}
        for group in inventory.groups_for_host(host):
            if group not in group_cache:
                group_cache[group] = _load_group_vars(group_vars_dir, group)
            hostvars.update(group_cache[group])  # replace semantics, in precedence order
        all_inputs[host] = _strip_ansible_keys(hostvars)
    return all_inputs
