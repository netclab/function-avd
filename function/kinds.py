"""The input-kind model: several XRs layered into per-device AVD inputs.

A ``Fabric`` names its inputs in ``spec.requires``. Each input XR carries a
fragment of the eos_designs document in ``spec.design`` plus ``spec.appliesTo``
saying which devices see it. Per device, the inputs that apply are layered in
``requires`` order with ``dict.update()`` -- Ansible's default
``hash_behaviour=replace``, which is what group_vars resolution does and what
``pyavd.get_avd_facts`` expects to be handed.

**Nothing is merged.** Two NodeSets carrying the same node-type key never meet,
because no device sees both: in a dual-DC fabric a DC1 leaf sees DC1's
``l3leaf.defaults`` and a DC2 leaf sees DC2's. So there is no fabric-wide
document to assemble and no conflict to resolve.

Two things are separate that look like one:

* which devices an input *declares* (``spec.declares``, plus the nodes its
  blocks name) -- the union of these is the fabric's device list, and there is
  no second list;
* which devices *see* it (``spec.appliesTo``). They coincide in simple
  topologies and diverge in a 5-stage CLOS, where a DC's ``super_spine`` block
  names four devices but is visible to every device of that DC.

Measured against AVD's own corpus: the hostvars this produces are byte-identical
to what **Ansible itself** reports -- all 8 bundled examples and 19 molecule
scenarios, up to 501 devices in one play. :mod:`function.migrate` builds the
inputs and :mod:`function.ansible_cli` supplies the reference.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
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


@lru_cache(maxsize=1)
def _default_vocabulary() -> "Vocabulary":
    """The dynamic key names AVD invents when a document says nothing.

    Read from pyavd's own public schema rather than copied into a literal: the
    three generators ship defaults (13 node types, 12 endpoint kinds, `tenants`),
    and a hand-kept copy goes stale silently. It already had -- `cameras` was
    added upstream and the literal this replaces never grew it.
    """
    from pyavd.api.schemas import AVDDesign

    design = AVDDesign()
    return Vocabulary(
        node_types=frozenset(e.key for e in design.node_type_keys),
        network_services=frozenset(e.name for e in design.network_services_keys),
        connected_endpoints=frozenset(e.key for e in design.connected_endpoints_keys),
    )


@dataclass(frozen=True)
class Vocabulary:
    """The top-level key names in force for a document.

    eos_designs generates key names from its own content -- `node_type_keys`,
    `network_services_keys` and `connected_endpoints_keys` each name a family of
    top-level keys. So no static map can classify every key, and this is that
    map made per document instead: AVD's defaults, extended by whatever
    generators the document carries.
    """

    node_types: frozenset[str]
    network_services: frozenset[str]
    connected_endpoints: frozenset[str]

    @classmethod
    def default(cls) -> "Vocabulary":
        return _default_vocabulary()

    def extend(self, design: dict) -> "Vocabulary":
        """Add the key names this document's own generators declare."""

        def named(source: str, field_name: str) -> frozenset[str]:
            entries = design.get(source)
            if not isinstance(entries, list):
                return frozenset()
            return frozenset(
                str(e[field_name]) for e in entries
                if isinstance(e, dict) and e.get(field_name)
            )

        return Vocabulary(
            node_types=self.node_types | named("node_type_keys", "key")
            | named("custom_node_type_keys", "key"),
            network_services=self.network_services | named("network_services_keys", "name"),
            connected_endpoints=self.connected_endpoints
            | named("connected_endpoints_keys", "key")
            | named("custom_connected_endpoints_keys", "key"),
        )


# The keys eos_designs names itself that are *not* settings. Everything else in
# its schema is, so only the exceptions are listed -- and `test_categories`
# checks each one against `documentation_options.table` in AVD's own schema, so
# an upstream recategorisation fails the suite instead of drifting.
_NODE_SET_KEYS = frozenset({
    "type",                    # table: type-setting -- the device's node type
    "node_type_keys",          # table: node-type-keys -- names the node families
    "custom_node_type_keys",
    "l3_interface_profiles",   # table: node-type-l3-interfaces-configuration
})
_NETWORK_SERVICE_KEYS = frozenset({
    "network_services",        # table: network-services
    "network_services_keys",
    "evpn_vlan_bundles",       # table: evpn-vlan-bundles
    "l2vlan_profiles",         # table: network-services-l2vlans-settings
    "mlag_ibgp_peering_vrfs",  # table: network-services-vrfs-settings
})
_CONNECTED_ENDPOINT_KEYS = frozenset({
    "connected_endpoints_keys",           # table: connected-endpoints-keys
    "custom_connected_endpoints_keys",
    "default_connected_endpoints_description",
    "default_connected_endpoints_port_channel_description",
    "default_network_ports_description",
    "default_network_ports_port_channel_description",
    # AVD tags neither of these; they are endpoint content by their own reading
    # and this repo places them here. Nothing upstream contradicts it.
    "port_profiles",
    "network_ports",
})


def kind_of(key: str, value: Any, vocabulary: "Vocabulary | None" = None) -> str:
    """Which kind one top-level key belongs to."""
    vocabulary = vocabulary or Vocabulary.default()
    if key in vocabulary.node_types or key in _NODE_SET_KEYS or is_node_block(value):
        return "NodeSet"
    if key in vocabulary.network_services or key in _NETWORK_SERVICE_KEYS:
        return "NetworkServiceSet"
    if key in vocabulary.connected_endpoints or key in _CONNECTED_ENDPOINT_KEYS:
        return "ConnectedEndpointSet"
    return "SettingSet"


def by_kind(design: dict, vocabulary: "Vocabulary | None" = None) -> dict[str, dict]:
    """Partition a fragment into ``{kind: design}``, keys in their own order."""
    vocabulary = (vocabulary or Vocabulary.default()).extend(design)
    parts: dict[str, dict] = {}
    for key, value in design.items():
        parts.setdefault(kind_of(key, value, vocabulary), {})[key] = value
    return {kind: parts[kind] for kind in KINDS if kind in parts}


def classify(design: dict, vocabulary: "Vocabulary | None" = None) -> str:
    """The kind a whole fragment belongs to: the one holding most of its keys.

    Advisory. The kinds exist for ownership -- RBAC is granted per kind -- not
    as a partition a schema could enforce, because eos_designs' top-level key
    names come from its own content. A fragment spanning categories is split by
    :func:`by_kind` rather than resolved by this.
    """
    parts = by_kind(design, vocabulary)
    if not parts:
        return "SettingSet"
    return max(parts, key=lambda kind: len(parts[kind]))


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
