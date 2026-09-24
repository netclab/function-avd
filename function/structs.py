"""Resources as Crossplane passes them, and back."""

from __future__ import annotations

# What a composed resource keeps when it goes back as desired unchanged. Everything
# else is the API server's or its controller's.
_KEPT_METADATA = ("name", "namespace", "labels", "annotations")
_KEPT = ("apiVersion", "kind", "spec", "data")


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


def kept(observed: dict) -> dict:
    """An observed composed resource as a desired one that changes nothing.

    Crossplane deletes a composed resource the function does not return, so one that is
    to stay as it is goes back like this.
    """
    metadata = observed.get("metadata") or {}
    out = {key: observed[key] for key in _KEPT if key in observed}
    out["metadata"] = {key: metadata[key] for key in _KEPT_METADATA if key in metadata}
    return out
