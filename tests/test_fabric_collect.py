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
        "Settings",
        "typo",
        # The fabric holds only spine1, so this pattern matches nothing.
        {"design": {"ntp_settings": {}}, "appliesTo": {"matchHostnames": ["leaf.*"]}},
    )
    rsp = _run(
        _request(
            _fabric(
                [
                    {"kind": "NodeSet", "name": "spines"},
                    {"kind": "Settings", "name": "typo"},
                ]
            ),
            required={"000-nodeset-spines": [SPINES], "001-settings-typo": [settings]},
        )
    )

    fatal = [r for r in rsp.results if r.severity == fnv1.SEVERITY_FATAL]
    assert fatal and "matched no device" in fatal[0].message
    assert not rsp.desired.resources


@pytest.mark.parametrize("kind", ["NodeSet", "NetworkServices", "ConnectedEndpoints", "Settings"])
def test_input_kinds_reconcile_and_report_their_keys(kind: str) -> None:
    """Each input reports its own shape on its own object, composing nothing."""
    rsp = _run(_request(_input_xr(kind, "an-input", {"design": {"ntp_settings": {}, "type": "x"}})))

    assert not any(r.severity == fnv1.SEVERITY_FATAL for r in rsp.results)
    status = resource.struct_to_dict(rsp.desired.composite.resource).get("status", {})
    assert status["keys"] == ["ntp_settings", "type"]
