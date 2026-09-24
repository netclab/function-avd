"""A Device, from its structured config to eos.cfg and the Request pushing it."""

from __future__ import annotations

import asyncio
import json

import pytest
from crossplane.function import resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from function import device, push
from function.fn import FunctionRunner

REPO = "examples/single-dc-l3ls"
HOST = "dc1-leaf1a"
NAME = f"single-dc-l3ls-{HOST}"

EAPI = {
    "url": f"https://{HOST}.l3ls.svc:443/command-api",
    "insecureSkipTLSVerify": True,
    "secretRef": {"name": "single-dc-l3ls-eapi", "key": "default"},
}


@pytest.fixture(scope="module")
def leaf(golden):
    return golden(REPO, HOST)


def a_device(structured_config: dict, eapi: dict | None = EAPI, status: dict | None = None):
    spec = {"structuredConfig": structured_config}
    if eapi:
        spec["eapi"] = eapi
    obj = {
        "apiVersion": "avd.netclab.dev/v1alpha1",
        "kind": "Device",
        "metadata": {"name": NAME, "namespace": "l3ls"},
        "spec": spec,
    }
    if status:
        obj["status"] = status
    return obj


def answered(body: dict, sent_id: str) -> dict:
    """An observed Request whose last response is `body`, to the call with `sent_id`."""
    return {
        "apiVersion": push.REQUEST_API_VERSION,
        "kind": "Request",
        "metadata": {"name": NAME, "namespace": "l3ls"},
        "spec": {"forProvider": {}},
        "status": {
            "response": {"body": json.dumps(body)},
            "requestDetails": {"body": json.dumps({"id": sent_id})},
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }


def test_eos_cfg_is_avds_own(leaf):
    structured_config, eos_cfg = leaf

    composed = device.compose(a_device(structured_config, eapi=None), {})

    config_map = composed.resources[device.CONFIG_MAP]
    assert config_map.resource["metadata"]["name"] == f"{NAME}-eos-cfg"
    assert config_map.resource["data"] == {"eos.cfg": eos_cfg}
    assert config_map.ready
    assert composed.status == {
        "configHash": device.config_hash(eos_cfg),
        "invalid": [],
        "error": "",
    }


def test_without_eapi_nothing_is_pushed(leaf):
    composed = device.compose(a_device(leaf[0], eapi=None), {})

    assert device.REQUEST not in composed.resources


def test_the_request_waits_for_its_first_push(leaf):
    composed = device.compose(a_device(leaf[0]), {})

    request = composed.resources[device.REQUEST]
    assert request.resource["metadata"] == {"name": NAME, "namespace": "l3ls"}
    assert not request.ready
    assert "deployed" not in composed.status


def test_a_push_response_is_deployed(leaf):
    structured_config, eos_cfg = leaf
    rev = push.revision(device.config_hash(eos_cfg))
    observed = {device.REQUEST: answered({"result": [{}, {"digest": "d1"}]}, f"push-{rev}")}

    composed = device.compose(a_device(structured_config), observed)

    assert composed.status["deployed"] == {"configHash": f"sha256:{rev}", "digest": "d1"}
    assert composed.status["error"] == ""
    assert composed.resources[device.REQUEST].ready
    check = composed.resources[device.REQUEST].resource["spec"]["forProvider"]
    assert '"d1"' in check["expectedResponseCheck"]["logic"]


def observe_answer(rev: str, digest: str) -> dict:
    marker = {"output": f"alias avd_cfg_{rev} show clock\n"}
    return {"result": [{"output": ""}, marker, {"output": f"{digest}\n"}]}


def test_an_observe_response_is_deployed_only_while_nothing_is(leaf):
    structured_config, eos_cfg = leaf
    new_hash = device.config_hash(eos_cfg)
    rev = push.revision(new_hash)
    observed = {device.REQUEST: answered(observe_answer(rev, "edited"), f"observe-{rev}")}

    first = device.compose(a_device(structured_config), observed)
    kept = device.compose(
        a_device(structured_config, status={"deployed": {"configHash": new_hash, "digest": "d1"}}),
        observed,
    )

    assert first.status["deployed"]["digest"] == "edited"
    assert kept.status["deployed"]["digest"] == "d1"


def test_a_new_eos_cfg_leaves_deployed_on_the_old_one_until_pushed(leaf):
    earlier = {"configHash": "sha256:0000000000000000", "digest": "d0"}

    composed = device.compose(a_device(leaf[0], status={"deployed": earlier}), {})

    assert composed.status["deployed"] == earlier
    assert composed.status["configHash"] != earlier["configHash"]
    assert not composed.resources[device.REQUEST].ready


REFUSED = {"error": {"code": 1002, "message": "CLI command 4 of 9 failed", "data": []}}


def test_a_refused_push_is_the_error_and_not_ready(leaf):
    # Pushed once already: the push taking an edit back is the one refused.
    structured_config, eos_cfg = leaf
    new_hash = device.config_hash(eos_cfg)
    observed = {device.REQUEST: answered(REFUSED, f"push-{push.revision(new_hash)}")}
    deployed = {"configHash": new_hash, "digest": "d1"}

    composed = device.compose(a_device(structured_config, status={"deployed": deployed}), observed)

    assert composed.status["error"] == "CLI command 4 of 9 failed"
    assert composed.status["deployed"] == deployed
    assert not composed.resources[device.REQUEST].ready


def test_an_error_stands_through_an_observe_of_its_own_revision_only(leaf):
    structured_config, eos_cfg = leaf
    new_hash = device.config_hash(eos_cfg)
    rev = push.revision(new_hash)
    observed = {device.REQUEST: answered(observe_answer("0" * 16, "x"), f"observe-{rev}")}

    same = device.compose(
        a_device(structured_config, status={"configHash": new_hash, "error": "refused"}), observed
    )
    other = device.compose(
        a_device(structured_config, status={"configHash": "sha256:1", "error": "refused"}),
        observed,
    )

    assert same.status["error"] == "refused"
    assert other.status["error"] == ""


def test_an_invalid_structured_config_keeps_what_was_composed(leaf):
    structured_config, _eos_cfg = leaf
    good = device.compose(a_device(structured_config), {})
    observed = {
        key: {**d.resource, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
        for key, d in good.resources.items()
    }
    prev = {"configHash": good.status["configHash"], "error": "", "invalid": []}
    broken = {**structured_config, "router_bgp": {**structured_config["router_bgp"], "nope": 1}}

    composed = device.compose(a_device(broken, status=prev), observed)

    assert composed.status["invalid"] == ["router_bgp.nope: Invalid key."]
    assert composed.status["configHash"] == good.status["configHash"]
    for key, desired in good.resources.items():
        assert composed.resources[key].resource == desired.resource


def test_an_eos_cfg_that_cannot_be_pushed_keeps_what_was_composed(leaf):
    # As AVD's own eos_cli_config_gen test host4 has it: a comment with no EOF, which
    # would swallow the rest of the session.
    structured_config = leaf[0]
    good = device.compose(a_device(structured_config), {})
    observed = {key: d.resource for key, d in good.resources.items()}

    composed = device.compose(
        a_device({**structured_config, "eos_cli": "comment\nVLAN BGP coverage"}), observed
    )

    assert composed.status["invalid"] == ["eos.cfg: 'comment' has no EOF line"]
    assert composed.resources[device.REQUEST].resource == good.resources[device.REQUEST].resource


def run(composite: dict, observed: dict | None = None) -> fnv1.RunFunctionResponse:
    req = fnv1.RunFunctionRequest(
        observed=fnv1.State(
            composite=fnv1.Resource(resource=resource.dict_to_struct(composite)),
            resources={
                key: fnv1.Resource(resource=resource.dict_to_struct(res))
                for key, res in (observed or {}).items()
            },
        )
    )
    return asyncio.run(FunctionRunner().RunFunction(req, None))


def test_the_runner_renders_what_crossplane_passes(leaf):
    # A Struct holds every number as a double; the runner makes whole ones ints again,
    # or pyavd would refuse VLAN 4092.0.
    structured_config, eos_cfg = leaf

    rsp = run(a_device(structured_config))

    config_map = resource.struct_to_dict(rsp.desired.resources[device.CONFIG_MAP].resource)
    assert config_map["data"]["eos.cfg"] == eos_cfg
    assert rsp.desired.resources[device.CONFIG_MAP].ready == fnv1.READY_TRUE
    assert rsp.desired.resources[device.REQUEST].ready == fnv1.READY_FALSE
    status = resource.struct_to_dict(rsp.desired.composite.resource)["status"]
    assert status["invalid"] == []
    assert not rsp.results


def test_the_runner_refuses_a_kind_it_does_not_reconcile():
    rsp = run({"apiVersion": "avd.netclab.dev/v1alpha1", "kind": "Nope", "metadata": {}})

    assert rsp.results[0].severity == fnv1.SEVERITY_FATAL
