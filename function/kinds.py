"""The input-kind model: several XRs layered into per-device AVD inputs.

A ``Fabric`` names its inputs in ``spec.requires``. Each input XR carries a
fragment of the eos_designs document in ``spec.design`` plus ``spec.appliesTo``
saying which devices see it. Per device, the inputs that apply are layered in
``requires`` order with ``dict.update()`` -- Ansible's default
``hash_behaviour=replace``, which is what group_vars resolution does and what
``pyavd.get_avd_facts`` expects to be handed.

**Nothing is merged.** Two NodeSets carrying the same node-type key never meet,
because no device sees both: in a dual-DC fabric a DC1 leaf sees DC1's
``l3leaf.defaults`` and a DC2 leaf sees DC2's. That is why this path needs
neither a fabric-wide document nor the fold in :mod:`function.xr`.

Two things are separate that look like one:

* which devices an input *declares* (``spec.declares``, plus the nodes its
  blocks name) -- the union of these is the fabric's device list, and there is
  no second list;
* which devices *see* it (``spec.appliesTo``). They coincide in simple
  topologies and diverge in a 5-stage CLOS, where a DC's ``super_spine`` block
  names four devices but is visible to every device of that DC.

Measured against AVD's own corpus: the hostvars this produces are byte-identical
to faithfully reproduced Ansible for all 8 bundled examples and every eos_designs
molecule scenario -- 25 inventories, up to 501 devices. See
:mod:`function.verify_kinds`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

KINDS = ("NodeSet", "NetworkServiceSet", "ConnectedEndpointSet", "SettingSet")


def matches(pattern: str, hostname: str) -> bool:
    """AVD's own hostname-matching semantics, copied from the code not the docs.

    ``shared_utils/node_type.py`` resolves ``default_node_types`` with
    ``search(f"^{regex}$", hostname)`` -- **AVD anchors the pattern for you**, so
    ``dc1-leaf.*`` matches a whole name. The schema's description reads as though
    the author must anchor it; the code does it for them. Copying the description
    instead of the code would make the same pattern mean different things in the
    two places.
    """
    return re.search(f"^{pattern}$", hostname) is not None


def is_node_block(value: Any) -> bool:
    """A node-type block is a dict carrying ``nodes`` and/or ``node_groups``."""
    return isinstance(value, dict) and ("nodes" in value or "node_groups" in value)


def hosts_in_blocks(design: dict) -> set[str]:
    """Device names a design's node-type blocks mention."""
    hosts: set[str] = set()
    for value in design.values():
        if not is_node_block(value):
            continue
        groups = list(value.get("node_groups") or [])
        for nodes in [value.get("nodes") or []] + [g.get("nodes") or [] for g in groups]:
            for node in nodes:
                if isinstance(node, dict) and node.get("name"):
                    hosts.add(node["name"])
    return hosts


def classify(design: dict) -> str:
    """Which kind a fragment belongs to.

    Advisory: the kinds exist for ownership (RBAC is granted per kind), not as a
    partition the schema could enforce -- eos_designs' top-level key names come
    from its own content, so no OpenAPI schema can describe them.
    """
    if any(is_node_block(v) for v in design.values()):
        return "NodeSet"
    # Both spellings. AVD 6.x reads a native `network_services` list as well as
    # the dynamic keys named by `network_services_keys` (default `tenants`) --
    # `shared_utils/filtered_tenants.py` reads one after the other. A custom key
    # name is only recognisable when `network_services_keys` travels with it.
    if {"network_services", "tenants", "network_services_keys"} & design.keys():
        return "NetworkServiceSet"
    if {
        "servers", "firewalls", "routers", "load_balancers", "storage_arrays",
        "cpes", "workstations", "access_points", "phones", "printers",
        "generic_devices", "port_profiles", "network_ports",
        "connected_endpoints_keys", "custom_connected_endpoints_keys",
    } & design.keys():
        return "ConnectedEndpointSet"
    return "SettingSet"


@dataclass
class Input:
    """One input XR, reduced to what resolution needs."""

    name: str
    kind: str
    design: dict
    # spec.appliesTo -- the criteria are unioned; none set means every device
    all_devices: bool = False
    node_sets: list[str] = field(default_factory=list)
    hosts: list[str] = field(default_factory=list)
    match_hostnames: list[str] = field(default_factory=list)
    # spec.declares -- devices this input brings into the fabric. Never a
    # pattern: visibility may be matched, existence may not. A typo in a pattern
    # would silently drop devices from the fabric.
    declares: list[str] = field(default_factory=list)

    @classmethod
    def from_xr(cls, xr: dict) -> "Input":
        """Build from an XR as ``required_resources`` delivers it."""
        spec = xr.get("spec") or {}
        applies = spec.get("appliesTo") or {}
        design = spec.get("design") or {}
        declares = list(spec.get("declares") or [])
        kind = xr.get("kind") or classify(design)
        if kind == "NodeSet" and not declares:
            # A NodeSet that declares nothing explicitly declares what its
            # blocks name -- the common case, where the two coincide.
            declares = sorted(hosts_in_blocks(design))
        return cls(
            name=(xr.get("metadata") or {}).get("name", ""),
            kind=kind,
            design=design,
            all_devices=bool(applies.get("all")),
            node_sets=list(applies.get("nodeSets") or []),
            hosts=list(applies.get("hosts") or []),
            match_hostnames=list(applies.get("matchHostnames") or []),
            declares=declares,
        )

    def scope(self, declared_by: dict[str, set[str]], devices: set[str]) -> set[str]:
        """Devices that see this input. The criteria are unioned.

        Omitting ``appliesTo`` means the whole fabric -- except on a ``NodeSet``,
        where it means the devices that NodeSet declares. A node-type block seen
        fabric-wide is not a thing Ansible can express: a ``group_vars`` file is
        read by its group. Every NodeSet in AVD's 8 examples is scoped to exactly
        what it declares, 26 of 26, so the default carries the common case and
        ``appliesTo`` is left to say the uncommon one -- which is real: in a
        5-stage CLOS a DC's ``super_spine`` block declares 4 devices and is seen
        by all 16 of that DC.
        """
        if self.all_devices:
            return devices
        if not (self.node_sets or self.hosts or self.match_hostnames):
            return devices & set(self.declares) if self.kind == "NodeSet" else devices
        named: set[str] = set()
        for name in self.node_sets:
            named |= declared_by.get(name, set())
        named |= set(self.hosts)
        named |= {h for h in devices for p in self.match_hostnames if matches(p, h)}
        return devices & named


def resolve(inputs: list[Input]) -> dict[str, dict]:
    """Layer ordered inputs into ``{hostname: hostvars}`` for ``get_avd_facts``.

    List order is precedence order: later inputs overwrite earlier ones key by
    key, whole-key, exactly as Ansible resolves group_vars. An overwrite is
    therefore intentional -- it is what the fabric owner declared by ordering --
    and belongs on status as a warning, never as an error.
    """
    declared_by = {i.name: set(i.declares) for i in inputs if i.declares}
    devices: set[str] = set()
    for hosts in declared_by.values():
        devices |= hosts

    out: dict[str, dict] = {host: {} for host in devices}
    for inp in inputs:
        for host in inp.scope(declared_by, devices):
            out[host].update(inp.design)
    return out


def unmatched_patterns(inputs: list[Input]) -> list[tuple[str, str]]:
    """``(input name, pattern)`` for every ``matchHostnames`` entry matching no device.

    A pattern is silent in both directions: a typo matches nothing and the input
    quietly reaches no device, while a wide pattern quietly reaches devices it
    was not meant to. The second is visible on status (`devices`); the first is
    not, so the caller is expected to treat this as an error and refuse to
    render -- the render is pushed as a full config replacement.
    """
    devices: set[str] = set()
    for inp in inputs:
        devices |= set(inp.declares)
    return [
        (inp.name, pattern)
        for inp in inputs
        for pattern in inp.match_hostnames
        if not any(matches(pattern, host) for host in devices)
    ]


def overwrites(inputs: list[Input]) -> list[tuple[str, str, str, str]]:
    """``(device, key, earlier input, later input)`` for every value replaced.

    Ansible resolves these silently. Here the ordering is written down by a
    person, so surfacing them is cheap and worth doing -- on status, as a
    warning.
    """
    declared_by = {i.name: set(i.declares) for i in inputs if i.declares}
    devices: set[str] = set()
    for hosts in declared_by.values():
        devices |= hosts

    seen: dict[tuple[str, str], tuple[str, Any]] = {}
    found: list[tuple[str, str, str, str]] = []
    for inp in inputs:
        for host in inp.scope(declared_by, devices):
            for key, value in inp.design.items():
                previous = seen.get((host, key))
                if previous is not None and previous[1] != value:
                    found.append((host, key, previous[0], inp.name))
                seen[(host, key)] = (inp.name, value)
    return found
