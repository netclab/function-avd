"""Resources as Crossplane passes them, and what goes back."""

from __future__ import annotations

from dataclasses import dataclass, field

# What a composed resource keeps when it goes back as desired unchanged. Everything
# else is the API server's or its controller's.
_KEPT_METADATA = ("name", "namespace", "labels", "annotations")
_KEPT = ("apiVersion", "kind", "spec", "data")


@dataclass(frozen=True)
class Desired:
    """A composed resource to return, and whether it is ready."""

    resource: dict
    ready: bool


@dataclass(frozen=True)
class Composed:
    """What a composite comes to: its status, and its composed resources by key."""

    status: dict
    resources: dict[str, Desired] = field(default_factory=dict)


def numbers(obj: object) -> object:
    """`obj` with every whole float an int again.

    A protobuf Struct holds numbers as doubles only, so VLAN 4092 arrives as 4092.0, and
    pyavd's schema refuses a float where it wants an int. A bool is left alone, and so
    is a fraction.
    """
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        return int(obj) if obj.is_integer() else obj
    if isinstance(obj, dict):
        return {key: numbers(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [numbers(value) for value in obj]
    return obj


def ready(observed: dict | None) -> bool:
    """An observed composed resource's Ready condition, or True when it has none.

    A ConfigMap or a Secret has no condition to wait for; a resource not observed yet is
    not ready.
    """
    if observed is None:
        return False
    conditions = (observed.get("status") or {}).get("conditions")
    if conditions is None:
        return True
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)


def kept(observed: dict) -> dict:
    """An observed composed resource as a desired one that changes nothing.

    Crossplane deletes a composed resource the function does not return, so one that is
    to stay as it is goes back like this.
    """
    metadata = observed.get("metadata") or {}
    out = {key: observed[key] for key in _KEPT if key in observed}
    out["metadata"] = {key: metadata[key] for key in _KEPT_METADATA if key in metadata}
    return out


def unchanged(observed: dict[str, dict]) -> dict[str, Desired]:
    """Every observed composed resource, kept as it is."""
    return {key: Desired(kept(res), ready=ready(res)) for key, res in observed.items()}
