"""A Fabric collects the inputs it names, and refuses to render without them.

Offline -- drives RunFunction directly, with no cluster and no Crossplane. The
gate is the most safety-critical piece in the collect path: requirements are
answered on the *next* reconcile, so the first one always arrives with nothing,
and a fabric rendered short of its inputs would be pushed to devices as a full
config replacement.
"""

from __future__ import annotations

import asyncio

import pytest
from crossplane.function import resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from function.fn import FunctionRunner

API = "avd.netclab.dev/v1alpha1"


def _run(req: fnv1.RunFunctionRequest) -> fnv1.RunFunctionResponse:
    return asyncio.run(FunctionRunner().RunFunction(req, None))


def _fabric(requires: list[dict], design: dict | None = None) -> dict:
    return {
        "apiVersion": API,
        "kind": "Fabric",
        "metadata": {"name": "fabric", "namespace": "avd"},
        "spec": {"fabricName": "FABRIC", "design": design or {}, "requires": requires},
    }


def _input_xr(kind: str, name: str, spec: dict) -> dict:
    return {
        "apiVersion": API,
        "kind": kind,
        "metadata": {"name": name, "namespace": "avd"},
        "spec": spec,
    }


def _request(xr: dict, required: dict[str, list[dict]] | None = None) -> fnv1.RunFunctionRequest:
    req = fnv1.RunFunctionRequest()
    req.observed.composite.resource.CopyFrom(resource.dict_to_struct(xr))
    for key, objects in (required or {}).items():
        # An empty list is Crossplane saying "I looked and found nothing", which
        # the proto distinguishes from a key that is absent entirely.
        entry = req.required_resources[key]
        for obj in objects:
            entry.items.add().resource.CopyFrom(resource.dict_to_struct(obj))
    return req


def _condition(rsp: fnv1.RunFunctionResponse, typ: str):
    return next((c for c in rsp.conditions if c.type == typ), None)


# A spine rather than a leaf, only because a leaf defaults to being a VTEP and
# would drag in the VXLAN pools -- this fixture is about the collect path, not
# about exercising AVD.
SPINES = _input_xr(
    "NodeSet",
    "spines",
    {
        # `type` rides in the same input: AVD needs it (or default_node_types)
        # to know what the device is, and it applies to whoever sees this input.
        "design": {
            "type": "spine",
            "spine": {
                "defaults": {"loopback_ipv4_pool": "10.255.0.0/27"},
                "nodes": [{"name": "spine1", "id": 1, "bgp_as": 65100}],
            },
        }
    },
)


def test_first_reconcile_asks_and_renders_nothing() -> None:
    """Requirements are answered next time round, so the first pass is empty.

    This is the case the gate exists for: not a slow operator, but the protocol.
    """
    rsp = _run(_request(_fabric([{"kind": "NodeSet", "name": "spines"}])))

    assert set(rsp.requirements.resources) == {"000-nodeset-spines"}
    selector = rsp.requirements.resources["000-nodeset-spines"]
    assert (selector.kind, selector.match_name, selector.namespace) == ("NodeSet", "spines", "avd")

    assert not rsp.desired.resources, "nothing may be composed before the inputs arrive"
    condition = _condition(rsp, "InputsResolved")
    assert condition.reason == "WaitingForInputs"


def test_missing_input_is_distinguished_from_not_yet_fetched() -> None:
    """An empty Resources means Crossplane looked and found nothing."""
    rsp = _run(
        _request(
            _fabric([{"kind": "NodeSet", "name": "spines"}]),
            required={"000-nodeset-spines": []},
        )
    )

    assert not rsp.desired.resources
    assert _condition(rsp, "InputsResolved").reason == "InputsMissing"
    assert "not found" in _condition(rsp, "InputsResolved").message


def test_resolved_inputs_compose_devices() -> None:
    rsp = _run(
        _request(
            _fabric([{"kind": "NodeSet", "name": "spines"}]),
            required={"000-nodeset-spines": [SPINES]},
        )
    )

    assert _condition(rsp, "InputsResolved").status == fnv1.STATUS_CONDITION_TRUE
    assert set(rsp.desired.resources) == {"spine1"}


def test_design_without_requires_still_composes() -> None:
    """The released path is untouched: one document, handed to every device.

    Guards the refactor that put both paths through render_structured_configs --
    v0.1.6 is published and netclab-xp pins it, so this must keep working with no
    inputs in sight.
    """
    rsp = _run(_request(_fabric(requires=[], design=SPINES["spec"]["design"])))

    assert not any(r.severity == fnv1.SEVERITY_FATAL for r in rsp.results)
    assert set(rsp.desired.resources) == {"spine1"}
    assert not rsp.requirements.resources, "a fabric with no requires asks for nothing"


def test_secret_input_is_refused_until_implemented() -> None:
    """It is in the enum so the mechanism can land without a schema change.

    Rendering a fabric whose credentials are silently absent would push a config
    without them, so refusing is the only safe placeholder.
    """
    rsp = _run(_request(_fabric([{"kind": "Secret", "name": "creds"}])))

    assert any(r.severity == fnv1.SEVERITY_FATAL for r in rsp.results)
    assert not rsp.desired.resources


def test_pattern_matching_no_device_is_refused() -> None:
    """A pattern is silent about matching nothing, so it cannot be allowed to."""
    settings = _input_xr(
        "SettingSet",
        "typo",
        # The fabric holds only spine1, so this pattern matches nothing.
        {"design": {"ntp_settings": {}}, "appliesTo": {"matchHostnames": ["leaf.*"]}},
    )
    rsp = _run(
        _request(
            _fabric(
                [
                    {"kind": "NodeSet", "name": "spines"},
                    {"kind": "SettingSet", "name": "typo"},
                ]
            ),
            required={"000-nodeset-spines": [SPINES], "001-settingset-typo": [settings]},
        )
    )

    fatal = [r for r in rsp.results if r.severity == fnv1.SEVERITY_FATAL]
    assert fatal and "matched no device" in fatal[0].message
    assert not rsp.desired.resources


@pytest.mark.parametrize("kind", ["NodeSet", "NetworkServiceSet", "ConnectedEndpointSet", "SettingSet"])
def test_input_kinds_reconcile_and_report_their_keys(kind: str) -> None:
    """Each input reports its own shape on its own object, composing nothing."""
    rsp = _run(_request(_input_xr(kind, "an-input", {"design": {"ntp_settings": {}, "type": "x"}})))

    assert not any(r.severity == fnv1.SEVERITY_FATAL for r in rsp.results)
    status = resource.struct_to_dict(rsp.desired.composite.resource).get("status", {})
    assert status["keys"] == ["ntp_settings", "type"]


# --- node-ID pools ----------------------------------------------------------

def _pooled(*, static_id: int | None) -> dict:
    """The spine design with node IDs coming from a pool.

    ⚠ `static_id` is the whole point of having two variants. AVD *reserves* an
    id written on the node, so a pool holding a different number for that device
    is refused rather than applied. Only a device with no id of its own takes
    what the pool holds.
    """
    node = {"name": "spine1", "bgp_as": 65100}
    if static_id is not None:
        node["id"] = static_id
    return {
        "type": "spine",
        "spine": {
            "defaults": {"loopback_ipv4_pool": "10.255.0.0/27"},
            "nodes": [node],
        },
        "fabric_numbering": {
            "node_id": {"algorithm": "pool_manager", "pools_file": "intended/data/x-ids.yml"}
        },
    }


POOLED = _pooled(static_id=None)


def _pool_configmap(pool: str) -> dict:
    from function import pools

    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "fabric-id-pool", "namespace": "avd"},
        "data": {pools.DATA_KEY: pool},
    }


def _with_observed(req: fnv1.RunFunctionRequest, name: str, obj: dict) -> fnv1.RunFunctionRequest:
    req.observed.resources[name].resource.CopyFrom(resource.dict_to_struct(obj))
    return req


def test_a_fabric_asking_for_a_pool_composes_one() -> None:
    """`pool_manager` keeps its assignments in a file. There is no file in a
    cluster, so the Fabric composes a ConfigMap and reads it back next time."""
    from function import pools

    rsp = _run(_request(_fabric(requires=[], design=POOLED)))

    assert not any(r.severity == fnv1.SEVERITY_FATAL for r in rsp.results), rsp.results
    assert pools.RESOURCE_NAME in rsp.desired.resources, "no pool was composed"

    composed = resource.struct_to_dict(rsp.desired.resources[pools.RESOURCE_NAME].resource)
    body = composed["data"][pools.DATA_KEY]
    assert "spine1" in body, body
    # `child_name` appends a hash, as it does for every composed resource.
    assert composed["metadata"]["name"].startswith("fabric-id-pool")


def test_a_fabric_without_a_pool_composes_none() -> None:
    """Nothing is created for a fabric that never asked -- an empty object with
    a warning about renumbering would be worse than no object."""
    from function import pools

    rsp = _run(_request(_fabric(requires=[], design=SPINES["spec"]["design"])))
    assert pools.RESOURCE_NAME not in rsp.desired.resources


def test_assignments_survive_the_next_reconcile() -> None:
    """Two identical reconciles produce the same pool and the same config.

    ⚠ **Weak on its own, deliberately kept.** Assignment is deterministic from
    the device set, so this passes even when the observed pool is ignored
    entirely -- verified by making `observed_pool` return nothing. It guards
    idempotency; `test_the_pool_decides_the_ids` is what proves the ConfigMap is
    read at all.
    """
    from function import pools

    first = _run(_request(_fabric(requires=[], design=POOLED)))
    pool = resource.struct_to_dict(
        first.desired.resources[pools.RESOURCE_NAME].resource
    )["data"][pools.DATA_KEY]

    second = _run(
        _with_observed(
            _request(_fabric(requires=[], design=POOLED)),
            pools.RESOURCE_NAME,
            _pool_configmap(pool),
        )
    )
    again = resource.struct_to_dict(
        second.desired.resources[pools.RESOURCE_NAME].resource
    )["data"][pools.DATA_KEY]

    assert again == pool, "the pool moved between two identical reconciles"

    def rendered(rsp: fnv1.RunFunctionResponse) -> str:
        cm = resource.struct_to_dict(rsp.desired.resources["spine1"].resource)
        return str(cm)

    assert rendered(second) == rendered(first), "the device changed although its ID did not"


def test_the_designs_own_pools_file_is_not_followed() -> None:
    """It names a path relative to somebody's working directory. Honouring it in
    a cluster would read nothing and quietly assign a fresh set of IDs."""
    from function import pools

    rsp = _run(_request(_fabric(requires=[], design=POOLED)))
    body = resource.struct_to_dict(
        rsp.desired.resources[pools.RESOURCE_NAME].resource
    )["data"][pools.DATA_KEY]
    assert body.strip(), "the pool came back empty -- the file was read from the wrong place"


def test_the_pool_decides_the_ids() -> None:
    """The pool is read, not merely written.

    A fabric handed a pool that assigns `spine1` the id 7 must render the device
    with id 7 -- AVD reserves an existing assignment rather than handing out the
    next free number. Without this, every test here passes on a function that
    throws the ConfigMap away and reassigns from scratch, because a fresh pool
    over an unchanged device set produces the same numbers.
    """
    from function import pools

    fresh = _run(_request(_fabric(requires=[], design=POOLED)))
    assert _loopback(fresh) == "10.255.0.1/32", "unexpected baseline"


    moved = _pool_configmap(
        "node_id_pools:\n"
        "  fabric_name=FABRIC/type=spine:\n"
        "    hostname=spine1: 7\n"
    )
    rsp = _run(
        _with_observed(
            _request(_fabric(requires=[], design=POOLED)), pools.RESOURCE_NAME, moved
        )
    )

    assert _loopback(rsp) == "10.255.0.7/32", (
        "the device did not take the id the pool holds -- the pool was not read"
    )
    kept = resource.struct_to_dict(
        rsp.desired.resources[pools.RESOURCE_NAME].resource
    )["data"][pools.DATA_KEY]
    assert "hostname=spine1: 7" in kept, kept


def _loopback(rsp: fnv1.RunFunctionResponse) -> str:
    device = resource.struct_to_dict(rsp.desired.resources["spine1"].resource)
    loopbacks = device["spec"]["structuredConfig"].get("loopback_interfaces") or []
    return next(i["ip_address"] for i in loopbacks if i["name"] == "Loopback0")


def test_an_id_written_on_the_node_outranks_the_pool() -> None:
    """AVD reserves a statically set id; the pool does not get to move it.

    Worth a test because the opposite is the natural expectation -- "the pool
    assigns ids" -- and getting it backwards would mean quietly renumbering a
    device whose id somebody wrote down on purpose.
    """
    from function import pools

    design = _pooled(static_id=1)
    rsp = _run(
        _with_observed(
            _request(_fabric(requires=[], design=design)),
            pools.RESOURCE_NAME,
            _pool_configmap(
                "node_id_pools:\n"
                "  fabric_name=FABRIC/type=spine:\n"
                "    hostname=spine1: 7\n"
            ),
        )
    )

    assert _loopback(rsp) == "10.255.0.1/32", "the pool overrode an id set on the node"


def _seeded(design: dict, name: str = "old-ids") -> dict:
    xr = _fabric(requires=[], design=design)
    xr["spec"]["nodeIdPool"] = {"seedConfigMapName": name}
    return xr


def test_a_named_seed_gates_the_first_reconcile() -> None:
    """⚠ Without this gate the seed could never work.

    Requirements are answered on the *next* reconcile, so the first one arrives
    with nothing. Rendering then would assign a fresh set of IDs and compose
    them as the pool, and the seed would be read into a fabric that had already
    renumbered itself.
    """
    from function import pools

    rsp = _run(_request(_seeded(POOLED)))

    assert pools.RESOURCE_NAME not in rsp.desired.resources, "a pool was composed anyway"
    assert not rsp.desired.resources, "nothing may be composed before the seed arrives"
    assert _condition(rsp, "InputsResolved").reason == "WaitingForSeed"
    assert pools.SEED_NAME in rsp.requirements.resources


def test_a_seed_that_does_not_exist_is_fatal() -> None:
    """Named and absent is an error, not an empty pool: carrying on renumbers
    every device, which is the one thing the field exists to prevent."""
    rsp = _run(_request(_seeded(POOLED), required={"id-pool-seed": []}))

    assert any(r.severity == fnv1.SEVERITY_FATAL for r in rsp.results)
    assert "renumber" in " ".join(r.message for r in rsp.results)


def test_a_seed_supplies_the_first_pool() -> None:
    """The migration case: a fabric that was already running elsewhere keeps the
    IDs AVD gave it, instead of starting over."""
    from function import pools

    seed = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "old-ids", "namespace": "avd"},
        "data": {
            pools.DATA_KEY: "node_id_pools:\n"
                            "  fabric_name=FABRIC/type=spine:\n"
                            "    hostname=spine1: 9\n"
        },
    }
    rsp = _run(_request(_seeded(POOLED), required={"id-pool-seed": [seed]}))

    assert _loopback(rsp) == "10.255.0.9/32", "the seeded assignment was not used"
    kept = resource.struct_to_dict(
        rsp.desired.resources[pools.RESOURCE_NAME].resource
    )["data"][pools.DATA_KEY]
    assert "hostname=spine1: 9" in kept


def test_a_seed_never_overrides_a_pool_the_fabric_already_has() -> None:
    """It seeds, it does not steer. Once the fabric keeps its own assignments,
    an old ConfigMap left lying around must not pull them back."""
    from function import pools

    seed = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "old-ids", "namespace": "avd"},
        "data": {
            pools.DATA_KEY: "node_id_pools:\n"
                            "  fabric_name=FABRIC/type=spine:\n"
                            "    hostname=spine1: 9\n"
        },
    }
    req = _request(_seeded(POOLED), required={"id-pool-seed": [seed]})
    _with_observed(
        req,
        pools.RESOURCE_NAME,
        _pool_configmap(
            "node_id_pools:\n  fabric_name=FABRIC/type=spine:\n    hostname=spine1: 3\n"
        ),
    )
    rsp = _run(req)

    assert _loopback(rsp) == "10.255.0.3/32", "the seed overrode the fabric's own pool"


# --- hostnames a Kubernetes name cannot spell -------------------------------

def _spines(*names: str) -> dict:
    return {
        "type": "spine",
        "spine": {
            "defaults": {"loopback_ipv4_pool": "10.255.0.0/27"},
            "nodes": [{"name": n, "id": i + 1, "bgp_as": 65100 + i}
                      for i, n in enumerate(names)],
        },
    }


def test_an_uppercase_hostname_still_composes() -> None:
    """AVD hostnames are free text, and two of its eight bundled examples --
    `campus-fabric` and `l2ls-fabric` -- write them entirely in capitals.

    ⚠ Found on a cluster, not here: a composed resource named after one is
    rejected outright (*"invalid name ... Must be a valid RFC 1123 subdomain
    name"*) and the whole fabric fails to compose. Offline tests never met it
    because they never reach an API server.
    """
    rsp = _run(_request(_fabric(requires=[], design=_spines("SPINE2"))))

    assert not any(r.severity == fnv1.SEVERITY_FATAL for r in rsp.results), rsp.results
    device = resource.struct_to_dict(rsp.desired.resources["SPINE2"].resource)
    assert device["metadata"]["name"].startswith("fabric-spine2-"), device["metadata"]["name"]
    # The hostname itself is untouched -- only the object's name is spelled
    # differently, or the render would be for a device that does not exist.
    assert device["spec"]["hostname"] == "SPINE2"


def test_two_hostnames_needing_one_object_name_are_refused() -> None:
    """One Device standing in for two switches would push one config to both."""
    rsp = _run(_request(_fabric(requires=[], design=_spines("SPINE1", "spine1"))))

    assert any(r.severity == fnv1.SEVERITY_FATAL for r in rsp.results), rsp.results
    assert "rename one" in " ".join(r.message for r in rsp.results)
