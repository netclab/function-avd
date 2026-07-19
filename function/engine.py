"""Thin wrapper around the pyavd pipeline.

``all_inputs`` (``{hostname: hostvars}``) -> per-device structured config.

This is the exact computation the Crossplane composite function will run later;
here it is fed from an Ansible example, later it will be fed from an XR.
"""

from __future__ import annotations

import pyavd


class InputValidationError(Exception):
    """Raised when AVD input validation reports blocking violations."""


def hostnames_from_design(design: dict) -> list[str]:
    """Enumerate device hostnames declared in a fabric-wide AVD design document.

    AVD derives the device set from the node-type blocks (``spine``, ``l3leaf``,
    ``l2leaf``, ...): each is a dict with ``nodes`` and/or ``node_groups``. This
    lets a single ``spec.design`` be self-contained -- the composite function
    needs no external host list.
    """
    hosts: set[str] = set()
    for value in design.values():
        if not isinstance(value, dict):
            continue
        if "nodes" not in value and "node_groups" not in value:
            continue
        groups = list(value.get("node_groups") or [])
        node_lists = [value.get("nodes") or []] + [g.get("nodes") or [] for g in groups]
        for nodes in node_lists:
            for node in nodes:
                if isinstance(node, dict) and node.get("name"):
                    hosts.add(node["name"])
    return sorted(hosts)


def device_roles_from_design(design: dict) -> dict[str, str]:
    """Map ``hostname -> node-type`` from the design's node-type blocks.

    Purely structural (no pyavd run) -- the node-type is the block key
    (``spine``/``l3leaf``/...) whose ``nodes``/``node_groups`` list the host.
    """
    roles: dict[str, str] = {}
    for key, value in design.items():
        if not isinstance(value, dict) or ("nodes" not in value and "node_groups" not in value):
            continue
        groups = list(value.get("node_groups") or [])
        node_lists = [value.get("nodes") or []] + [g.get("nodes") or [] for g in groups]
        for nodes in node_lists:
            for node in nodes:
                if isinstance(node, dict) and node.get("name"):
                    roles[node["name"]] = key
    return roles


def render_fabric_design(
    design: dict, fabric_name: str | None = None, *, validate: bool = True
) -> dict[str, dict]:
    """Render a whole fabric from one AVD design document.

    This is the core the Crossplane composite function will run: the same
    fabric-wide document is fed to every device, with roles resolved by AVD
    (``default_node_types`` / node-type blocks). ``fabric_name`` (from the XR's
    typed ``spec.fabricName``) is injected as AVD's ``fabric_name``.
    """
    doc = dict(design)
    if fabric_name is not None:
        doc["fabric_name"] = fabric_name
    all_inputs = {hostname: doc for hostname in hostnames_from_design(doc)}
    return render_structured_configs(all_inputs, validate=validate)


def validate_all(all_inputs: dict[str, dict]) -> dict[str, list]:
    """Validate every device's inputs. Returns ``{hostname: [violations]}``."""
    violations: dict[str, list] = {}
    for hostname, inputs in all_inputs.items():
        result = pyavd.validate_inputs(inputs)
        if result.validation_result.violations:
            violations[hostname] = list(result.validation_result.violations)
    return violations


def render_structured_configs(
    all_inputs: dict[str, dict], *, validate: bool = True
) -> dict[str, dict]:
    """Run facts + per-device structured config for the whole fabric.

    ``get_avd_facts`` is fabric-wide (needs every device at once); the structured
    config is then derived per device from those shared facts.
    """
    if validate:
        violations = validate_all(all_inputs)
        if violations:
            raise InputValidationError(violations)

    avd_facts = pyavd.get_avd_facts(all_inputs)
    return {
        hostname: pyavd.get_device_structured_config(
            hostname, inputs, avd_facts=avd_facts
        )._dump()
        for hostname, inputs in all_inputs.items()
    }
