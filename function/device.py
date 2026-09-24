"""A Device: its structured config rendered to eos.cfg, and the Request that pushes it.

Everything the Device decides is in `compose`, which reads dicts and returns dicts, so
it is tested without Crossplane.

What the Device composes:

    <device>-eos-cfg   ConfigMap, `eos.cfg` -- the configuration as pushed
    <device>           Request, only with spec.eapi

A structured config that pyavd refuses renders nothing: the ConfigMap and the Request
stay as they are, and `status.invalid` says why.
"""

from __future__ import annotations

import hashlib

import pyavd

from . import push
from .structs import Composed, Desired, unchanged

CONFIG_MAP = "eos-cfg"
REQUEST = "request"

EOS_CFG_KEY = "eos.cfg"

# Hex characters of the configHash: the marker alias on the device is named after it.
_HASH_LEN = 16


def config_hash(eos_cli: str) -> str:
    return "sha256:" + hashlib.sha256(eos_cli.encode()).hexdigest()[:_HASH_LEN]


def invalid(structured_config: dict) -> list[str]:
    """What pyavd's schema refuses in `structured_config`, one line per violation."""
    result = pyavd.validate_structured_config(structured_config).validation_result
    return [f"{'.'.join(v.path)}: {v.message}" for v in result.violations]


def compose(device: dict, observed: dict[str, dict]) -> Composed:
    """The resources and status of `device`, given its observed composed resources.

    `observed` is keyed as `Composed.resources` is. Raises what pyavd raises when a
    structured config it accepted still does not render.
    """
    meta = device.get("metadata") or {}
    name, namespace = meta["name"], meta["namespace"]
    spec = device.get("spec") or {}
    prev = device.get("status") or {}

    structured_config = spec.get("structuredConfig") or {}
    refused = invalid(structured_config)
    if refused:
        return _unchanged(prev, observed, refused)

    eos_cli = pyavd.get_device_config(structured_config)
    try:
        commands = push.config_commands(eos_cli)
    except ValueError as err:
        # A block with no end would swallow the rest of the session.
        return _unchanged(prev, observed, [f"eos.cfg: {err}"])
    new_hash = config_hash(eos_cli)
    resources = {
        CONFIG_MAP: Desired(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": f"{name}-{CONFIG_MAP}", "namespace": namespace},
                "data": {EOS_CFG_KEY: eos_cli},
            },
            # A ConfigMap has no Ready condition to wait for.
            ready=True,
        )
    }
    status = {"configHash": new_hash, "invalid": [], "error": ""}
    deployed = prev.get("deployed") or {}

    eapi = spec.get("eapi")
    if eapi:
        observed_request = observed.get(REQUEST) or {}
        recorded = deployed.get("digest") if deployed.get("configHash") == new_hash else None
        found = push.digest_from_observed(observed_request, new_hash)
        if found is not None:
            digest, source = found
            if source == "push" or recorded is None:
                recorded = digest
        if recorded:
            deployed = {"configHash": new_hash, "digest": recorded}

        error = push.error_from_observed(observed_request, new_hash)
        if error is None:
            # No push of this revision has answered: an earlier error of the same
            # revision still stands, one of another revision does not.
            error = prev.get("error", "") if prev.get("configHash") == new_hash else ""
        status["error"] = error

        secret = eapi["secretRef"]
        resources[REQUEST] = Desired(
            push.request_object(
                name=name,
                namespace=namespace,
                url=eapi["url"],
                secret_name=secret["name"],
                secret_key=secret["key"],
                insecure_skip_tls_verify=bool(eapi.get("insecureSkipTLSVerify", False)),
                commands=commands,
                config_hash=new_hash,
                deployed_digest=recorded,
            ),
            # Ready once the device runs this eos.cfg; a Request that eAPI refused is
            # Ready by its own conditions.
            ready=bool(recorded) and not error,
        )

    if deployed:
        status["deployed"] = deployed
    return Composed(status=status, resources=resources)


def _unchanged(prev: dict, observed: dict[str, dict], refused: list[str]) -> Composed:
    """Nothing composed changes, so a Request keeps pushing the last eos.cfg that could be."""
    status = {
        "invalid": refused,
        "error": prev.get("error", ""),
        **{key: prev[key] for key in ("configHash", "deployed") if key in prev},
    }
    return Composed(status=status, resources=unchanged(observed))
