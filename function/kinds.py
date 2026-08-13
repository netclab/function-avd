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

from dataclasses import dataclass, field
from typing import Any

KINDS = ("NodeSet", "NetworkServices", "ConnectedEndpoints", "Settings")


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
    if {"tenants", "network_services_keys"} & design.keys():
        return "NetworkServices"
    if {
        "servers", "firewalls", "routers", "load_balancers", "storage_arrays",
        "cpes", "workstations", "access_points", "phones", "printers",
        "generic_devices", "port_profiles", "network_ports",
        "connected_endpoints_keys", "custom_connected_endpoints_keys",
    } & design.keys():
        return "ConnectedEndpoints"
    return "Settings"


@dataclass
class Input:
    """One input XR, reduced to what resolution needs."""

    name: str
    kind: str
    design: dict
    # spec.appliesTo -- exactly one of the three
    all_devices: bool = False
    node_sets: list[str] = field(default_factory=list)
    hosts: list[str] = field(default_factory=list)
    # spec.declares -- devices this input brings into the fabric
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
            declares=declares,
        )

    def scope(self, declared_by: dict[str, set[str]], devices: set[str]) -> set[str]:
        """Devices that see this input."""
        if self.all_devices:
            return devices
        if self.node_sets:
            named: set[str] = set()
            for name in self.node_sets:
                named |= declared_by.get(name, set())
            return devices & named
        return devices & set(self.hosts)


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
