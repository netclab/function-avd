"""Build a ``Fabric`` XR (custom resource) from an AVD Ansible example.

Bridges Milestone 1 (Ansible example -> proven pyavd output) to Milestone 2
(Crossplane XR -> pyavd output): it folds the per-host Ansible inputs into a
single fabric-wide AVD design document -- the shape carried by
``Fabric.spec.design`` -- with device roles expressed via ``default_node_types``
instead of Ansible's per-group ``type``.

The resulting XR is a self-contained fixture for ``crossplane render`` and for
regression-testing the composite function.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path

import yaml

from .ansible_inputs import build_all_inputs

API_VERSION = "avd.netclab.dev/v1alpha1"
KIND = "Fabric"


def _is_node_type_block(value: object) -> bool:
    return isinstance(value, dict) and ("nodes" in value or "node_groups" in value)


def _canon(value: object) -> str:
    return yaml.safe_dump(value, sort_keys=True, default_flow_style=True)


def _conflicting_block_defaults(per_host: dict[str, dict]) -> set[tuple[str, str]]:
    """Return ``{(block, default_key)}`` whose value differs across DCs.

    These are per-DC/per-pod settings (pools, ASNs, uplinks) kept in a group's
    ``defaults``; they cannot share one block-level ``defaults`` and are instead
    pushed down to the node_groups/nodes of their originating DC (which override
    defaults in AVD, so effective values are unchanged).
    """
    seen: dict[tuple[str, str], set[str]] = {}
    for hostvars in per_host.values():
        for key, value in hostvars.items():
            if _is_node_type_block(value):
                for dk, dv in (value.get("defaults") or {}).items():
                    seen.setdefault((key, dk), set()).add(_canon(dv))
    return {kd for kd, values in seen.items() if len(values) > 1}


def _append_unique(dst_list: list, items, id_key: str, pushdown: dict) -> None:
    """Append node/node_group dicts not already present, baking pushdown defaults."""
    seen = {e[id_key] for e in dst_list if isinstance(e, dict) and id_key in e}
    for item in items or []:
        if isinstance(item, dict) and item.get(id_key) not in seen:
            item = copy.deepcopy(item)  # group_vars dicts are shared across hosts
            for pk, pv in pushdown.items():
                item.setdefault(pk, pv)
            dst_list.append(item)
            seen.add(item.get(id_key))


def fabric_design_from_inputs(
    per_host: dict[str, dict],
) -> tuple[str, dict, set[str]]:
    """Fold ``{hostname: hostvars}`` into ``(fabric_name, design, conflicts)``.

    Per-host hostvars differ by ``type`` (re-expressed as ``default_node_types``)
    and by per-DC node-type blocks. Node-type blocks are unioned; per-DC
    ``defaults`` that disagree are pushed down to that DC's node_groups/nodes so a
    single document stays lossless. Remaining ``conflicts`` are fabric-level keys
    (e.g. ``aaa_settings``) that are not node-scoped and need an explicit call.
    """
    fabric_name = "FABRIC"
    design: dict = {}
    roles: dict[str, list[str]] = {}
    conflicts: set[str] = set()
    conflict_defaults = _conflicting_block_defaults(per_host)

    for hostname, hostvars in per_host.items():
        for key, value in hostvars.items():
            if key == "type":
                roles.setdefault(value, []).append(hostname)
            elif key == "fabric_name":
                fabric_name = value
            elif _is_node_type_block(value):
                block = design.setdefault(key, {})
                shared_defaults = block.setdefault("defaults", {})
                pushdown: dict = {}
                for dk, dv in (value.get("defaults") or {}).items():
                    if (key, dk) in conflict_defaults:
                        pushdown[dk] = dv  # per-DC -> node_group/node level
                    else:
                        shared_defaults[dk] = dv  # uniform -> stays shared
                _append_unique(block.setdefault("nodes", []), value.get("nodes"), "name", pushdown)
                _append_unique(
                    block.setdefault("node_groups", []), value.get("node_groups"), "group", pushdown
                )
                for bk, bv in value.items():
                    if bk not in ("defaults", "nodes", "node_groups"):
                        block[bk] = bv
            else:
                if key in design and _canon(design[key]) != _canon(value):
                    conflicts.add(key)
                design[key] = value

    # Drop empty scaffolding left by setdefault.
    for block in design.values():
        if isinstance(block, dict):
            for empty_key in ("defaults", "nodes", "node_groups"):
                if empty_key in block and not block[empty_key]:
                    del block[empty_key]

    # Roles come from Ansible's per-group `type`. If the example instead assigns
    # roles fabric-wide via its own `default_node_types` (e.g. multipod), that
    # value already flowed into `design` -- keep it rather than clobbering it.
    if roles:
        design["default_node_types"] = [
            {
                "node_type": node_type,
                "match_hostnames": [f"^{re.escape(h)}$" for h in sorted(hosts)],
            }
            for node_type, hosts in sorted(roles.items())
        ]
    return fabric_name, design, conflicts


class FabricFoldConflict(Exception):
    """Raised when per-host Ansible data cannot collapse into one fabric document.

    Carries the conflicting keys -- typically per-DC/per-pod settings kept in
    group ``defaults`` that must be relocated to ``node_groups`` level to be
    representable in a single ``spec.design``.
    """


def fabric_xr_from_example(
    example_dir: str | Path,
    *,
    name: str | None = None,
    namespace: str = "default",
    strict: bool = True,
) -> dict:
    """Return a ``Fabric`` XR dict for an AVD Ansible example directory.

    With ``strict`` (default), raises :class:`FabricFoldConflict` if the example's
    Ansible group-vars cannot be folded losslessly into a single fabric document.
    """
    per_host = build_all_inputs(example_dir)
    fabric_name, design, conflicts = fabric_design_from_inputs(per_host)
    if conflicts and strict:
        raise FabricFoldConflict(sorted(conflicts))
    return {
        "apiVersion": API_VERSION,
        "kind": KIND,
        "metadata": {
            "name": name or fabric_name.lower().replace("_", "-"),
            "namespace": namespace,
        },
        "spec": {
            "fabricName": fabric_name,
            "design": design,
        },
    }
