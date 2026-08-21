"""Where a Fabric keeps its node-ID pool.

`fabric_numbering.node_id.algorithm: pool_manager` asks AVD to hand out node IDs
instead of reading them off each node. AVD keeps those assignments in **a file**,
and the render is only reproducible while that file survives — lose it and every
device is renumbered, which reaches a switch as a full configuration replacement.

There is no such file in a cluster, so the Fabric keeps the pool in a ConfigMap
it composes and reads back on the next reconcile. The pool is therefore an
**output that is also the next run's input** — the one place a render stops being
a pure function of its inputs.

⚠ **`spec.design`'s own `pools_file` is overridden, not honoured.** It names a
path relative to a working directory, which is a statement about somebody's
laptop; the same design in a cluster has nowhere to point. The value is replaced
with a path inside a scratch directory that exists only for the length of one
reconcile, and the ConfigMap is the real home.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

#: composition-resource-name of the ConfigMap, and the key inside it
RESOURCE_NAME = "id-pool"
DATA_KEY = "node-id-pools.yml"

_ALGORITHM_PATH = ("fabric_numbering", "node_id")
_POOL_FILE = "pools.yml"


def wanted_by(all_inputs: dict[str, dict]) -> bool:
    """Does any device ask for pool-assigned node IDs?

    Read per device rather than fabric-wide because the setting is an ordinary
    input key: nothing stops one input from narrowing it to part of the fabric,
    and one device asking is enough to need a pool.
    """
    return any(_node_id(hostvars).get("algorithm") == "pool_manager"
               for hostvars in all_inputs.values())


def _node_id(hostvars: dict) -> dict:
    node = hostvars
    for key in _ALGORITHM_PATH:
        node = node.get(key) if isinstance(node, dict) else None
        if node is None:
            return {}
    return node if isinstance(node, dict) else {}


def observed_pool(observed_resources: Any) -> str:
    """The pool as the cluster last saw it, or empty on the first reconcile."""
    entry = (observed_resources or {}).get(RESOURCE_NAME)
    if entry is None:
        return ""
    from crossplane.function import resource

    data = (resource.struct_to_dict(entry.resource) or {}).get("data") or {}
    return data.get(DATA_KEY) or ""


@contextmanager
def pool_manager(all_inputs: dict[str, dict], previous: str) -> Iterator[tuple[Any, Path]]:
    """A ``PoolManager`` backed by ``previous``, and the file it ends up in.

    Rewrites every device's `pools_file` to the scratch copy first: AVD resolves
    that value verbatim against the working directory, so leaving the design's
    own value in place would read a path that does not exist here and silently
    assign a fresh set of IDs.
    """
    from pyavd.api.pool_manager import PoolManager

    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / _POOL_FILE
        if previous.strip():
            path.write_text(previous)
        for hostvars in all_inputs.values():
            node_id = _node_id(hostvars)
            if node_id.get("algorithm") == "pool_manager":
                node_id["pools_file"] = str(path)
        yield PoolManager(Path(scratch)), path


#: requirement key for a seed ConfigMap named by the Fabric
SEED_NAME = "id-pool-seed"


def seed(req: Any, rsp: Any, spec: dict, namespace: str) -> tuple[str, str]:
    """Assignments to start from, when this fabric has no pool of its own yet.

    Returns ``(state, text)`` where state is one of:

    * ``none``    -- no seed named; a new fabric assigns its own IDs;
    * ``pending`` -- asked for, not delivered yet. ⚠ **The caller must not
      render.** Requirements are answered on the *next* reconcile, so the first
      one always arrives with nothing; rendering then would assign a fresh set
      of IDs and compose them as the pool, and the seed would never be read;
    * ``missing`` -- Crossplane looked and it is not there. Named and absent is
      an error, not an empty pool: proceeding renumbers every device, which is
      the one thing this field exists to prevent;
    * ``ok``      -- the text.
    """
    from crossplane.function import resource, response

    pool_spec = spec.get("nodeIdPool") or {}
    name = pool_spec.get("seedConfigMapName")
    if not name:
        return "none", ""

    response.require_resources(
        rsp, name=SEED_NAME, api_version="v1", kind="ConfigMap",
        match_name=name, namespace=namespace,
    )
    if SEED_NAME not in req.required_resources:
        return "pending", ""
    items = req.required_resources[SEED_NAME].items
    if not items:
        return "missing", ""

    data = (resource.struct_to_dict(items[0].resource) or {}).get("data") or {}
    return "ok", data.get(pool_spec.get("seedKey") or DATA_KEY) or ""


def configmap(xr_name: str, namespace: str, fabric_name: str, pool: str) -> dict:
    """The ConfigMap the Fabric composes to keep its assignments."""
    from crossplane.function import resource

    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": resource.child_name(xr_name, "id-pool"),
            "namespace": namespace,
            # ⚠ `fabric` alone does not find this: every ConfigMap the fabric
            # composes carries it, so a selector returns the pool and one render
            # per device -- 27 objects for a 26-device fabric, with the pool in
            # no particular position. An object whose annotation says deleting it
            # renumbers the fabric has to be addressable, so it says what it is.
            "labels": {
                "avd.netclab.dev/fabric": fabric_name,
                "avd.netclab.dev/artifact": "node-id-pool",
            },
            "annotations": {
                "avd.netclab.dev/description":
                    "AVD node-ID assignments. Deleting this renumbers the fabric.",
            },
        },
        "data": {DATA_KEY: pool},
    }
