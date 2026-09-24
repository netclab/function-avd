"""A Fabric, from what it names to the Devices it composes -- the render stubbed."""

from __future__ import annotations

import asyncio
import base64
import json

import pytest
from crossplane.function import resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from function import fabric
from function.fn import FunctionRunner

NS = "l3ls"
NAME = "single-dc-l3ls"
POOL_CM = f"{NAME}-pools"
POOL_KEY = "intended.data.FABRIC-ids.yml"


def a_fabric(status: dict | None = None, **spec) -> dict:
    obj = {
        "apiVersion": "avd.netclab.dev/v1alpha1",
        "kind": "Fabric",
        "metadata": {"name": NAME, "namespace": NS},
        "spec": {
            "inputs": [f"{NAME}-fabric", f"{NAME}-dc1"],
            "play": {"hosts": "FABRIC"},
            "groups": {"all": {}},
            "ansibleCfg": {},
            **spec,
        },
    }
    if status:
        obj["status"] = status
    return obj


def an_input(name: str, generation: int = 1, labelled: bool = True) -> dict:
    meta = {"name": name, "namespace": NS, "generation": generation}
    if labelled:
        meta["labels"] = {"avd.netclab.dev/fabric": NAME}
    return {
        "apiVersion": "avd.netclab.dev/v1alpha1",
        "kind": "FabricInput",
        "metadata": meta,
        "spec": {"appliesTo": {"group": name}, "beside": "inventory", "design": {"a": 1}},
    }


def a_config_map(name: str, data: dict) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": NS, "resourceVersion": "7"},
        "data": data,
    }


POOLS = [
    {"configMapRef": {"name": POOL_CM, "key": POOL_KEY}, "path": "p.yml", "beside": "inventory"}
]


def found(fab: dict, *extra: dict) -> dict[str, list[dict]]:
    """Every requirement of `fab` answered from its listed inputs and `extra`."""
    docs = [an_input(name) for name in fab["spec"]["inputs"]] + list(extra)
    required = {}
    for key, sel in fabric.requirements(fab).items():
        if "match_name" in sel:
            required[key] = [
                d
                for d in docs
                if d["kind"] == sel["kind"] and d["metadata"]["name"] == sel["match_name"]
            ]
        else:
            required[key] = [
                d
                for d in docs
                if d["kind"] == sel["kind"]
                and (d["metadata"].get("labels") or {}).get("avd.netclab.dev/fabric") == NAME
            ]
    return required


PUSH = {
    "host": "dc1-leaf1a.l3ls.svc",
    "user": "arista",
    "password": "arista",
    "port": "",
    "ssl": True,
    "validate": False,
}


def rendered(hosts=("dc1-leaf1a", "dc1-leaf1b"), push=None, pools=None) -> fabric.Render:
    return fabric.Render(
        structured={host: {"hostname": host} for host in hosts},
        push={host: dict(PUSH, host=f"{host}.l3ls.svc") for host in hosts} | (push or {}),
        pools=pools or {},
    )


class Renderer:
    """A stand-in for `fabric.render`, counting its calls."""

    def __init__(self, result: fabric.Render):
        self.result = result
        self.calls = 0

    def __call__(self, fab, read):
        self.calls += 1
        return self.result


def test_the_fabric_asks_for_its_inputs_config_maps_and_vault_secret():
    fab = a_fabric(
        pools=POOLS,
        files=[
            {
                "configMapRef": {"name": f"{NAME}-files", "key": "a.j2"},
                "path": "a.j2",
                "beside": "playbook",
            }
        ],
        vaultPassword={"secretRef": {"name": f"{NAME}-vault", "key": "password"}},
    )

    wanted = fabric.requirements(fab)

    assert sorted(wanted) == [
        f"configmap:{NAME}-files",
        f"configmap:{POOL_CM}",
        f"input:{NAME}-dc1",
        f"input:{NAME}-fabric",
        "labelled",
        "vault",
    ]
    assert wanted["labelled"]["match_labels"] == {"avd.netclab.dev/fabric": NAME}
    assert all(sel["namespace"] == NS for sel in wanted.values())


def test_what_is_missing_or_unlisted_is_reported_and_nothing_renders():
    fab = a_fabric(pools=POOLS)
    required = found(fab, a_config_map(POOL_CM, {}), an_input(f"{NAME}-stray"))
    required[f"input:{NAME}-dc1"] = []
    renderer = Renderer(rendered())
    observed = {"x": a_config_map("x", {"k": "v"})}

    composed = fabric.compose(fab, required, observed, renderer)

    assert composed.status["missing"] == {
        "inputs": [f"{NAME}-dc1"],
        "configMaps": [{"name": POOL_CM, "key": POOL_KEY}],
    }
    assert composed.status["unlisted"] == [f"{NAME}-stray"]
    assert renderer.calls == 0
    assert composed.resources["x"].resource["data"] == {"k": "v"}


def test_a_missing_vault_secret_is_the_error():
    fab = a_fabric(vaultPassword={"secretRef": {"name": f"{NAME}-vault", "key": "password"}})
    renderer = Renderer(rendered())

    composed = fabric.compose(fab, found(fab), {}, renderer)

    assert "no Secret single-dc-l3ls-vault" in composed.status["error"]
    assert renderer.calls == 0


def test_the_vault_password_reaches_the_render():
    fab = a_fabric(vaultPassword={"secretRef": {"name": f"{NAME}-vault", "key": "password"}})
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": f"{NAME}-vault", "namespace": NS},
        "data": {"password": base64.b64encode(b"s3cr3t").decode()},
    }

    read = fabric.gather(fab, found(fab, secret))

    assert read.vault_password == "s3cr3t"
    assert read.problem is None


def test_each_host_is_a_device_with_its_eapi():
    fab = a_fabric()

    composed = fabric.compose(fab, found(fab), {}, Renderer(rendered()))

    device = composed.resources["single-dc-l3ls-dc1-leaf1a"].resource
    assert device["metadata"]["annotations"] == {"avd.netclab.dev/host": "dc1-leaf1a"}
    assert device["metadata"]["labels"] == {"avd.netclab.dev/fabric": NAME}
    assert device["spec"] == {
        "structuredConfig": {"hostname": "dc1-leaf1a"},
        "eapi": {
            "url": "https://dc1-leaf1a.l3ls.svc:443/command-api",
            "insecureSkipTLSVerify": True,
            "secretRef": {"name": "single-dc-l3ls-eapi", "key": "default"},
        },
    }
    assert composed.status["error"] == ""


def test_httpapis_defaults_are_plain_http_on_80_with_certificates_checked():
    fab = a_fabric()
    push = {
        "dc1-leaf1a": {
            "host": "10.0.0.1",
            "user": "a",
            "password": "b",
            "port": "",
            "ssl": False,
            "validate": True,
        }
    }

    composed = fabric.compose(fab, found(fab), {}, Renderer(rendered(push=push)))

    eapi = composed.resources["single-dc-l3ls-dc1-leaf1a"].resource["spec"]["eapi"]
    assert eapi["url"] == "http://10.0.0.1:80/command-api"
    assert eapi["insecureSkipTLSVerify"] is False


def secret_values(composed) -> dict[str, str]:
    data = composed.resources[fabric.EAPI].resource["data"]
    return {key: base64.b64decode(base64.b64decode(value)).decode() for key, value in data.items()}


def test_the_common_pair_is_default_and_a_host_that_differs_has_its_own_key():
    fab = a_fabric()
    hosts = ("dc1-leaf1a", "dc1-leaf1b", "dc1-leaf2a")
    push = {"dc1-leaf2a": dict(PUSH, host="dc1-leaf2a.l3ls.svc", user="admin", password="other")}

    composed = fabric.compose(fab, found(fab), {}, Renderer(rendered(hosts, push=push)))

    assert secret_values(composed) == {"default": "arista:arista", "dc1-leaf2a": "admin:other"}
    ref = composed.resources["single-dc-l3ls-dc1-leaf2a"].resource["spec"]["eapi"]["secretRef"]
    assert ref == {"name": "single-dc-l3ls-eapi", "key": "dc1-leaf2a"}


def test_a_host_named_default_with_its_own_pair_is_the_error():
    fab = a_fabric()
    push = {"default": dict(PUSH, user="admin")}

    composed = fabric.compose(
        fab, found(fab), {}, Renderer(rendered(("a", "b", "default"), push=push))
    )

    assert "'default'" in composed.status["error"]
    assert not composed.resources


def test_two_hosts_that_spell_one_device_is_the_error():
    fab = a_fabric()

    composed = fabric.compose(fab, found(fab), {}, Renderer(rendered(("DC1.LEAF1", "dc1-leaf1"))))

    assert "would both be Device single-dc-l3ls-dc1-leaf1" in composed.status["error"]


def test_a_failed_render_keeps_the_devices_and_is_not_repeated():
    fab = a_fabric()
    device = {
        "apiVersion": "avd.netclab.dev/v1alpha1",
        "kind": "Device",
        "metadata": {"name": "d"},
        "spec": {"x": 1},
    }
    renderer = Renderer(fabric.Render(problem="fatal: [dc1-leaf1a]: FAILED!"))

    first = fabric.compose(fab, found(fab), {"d": device}, renderer)
    again = fabric.compose(a_fabric(status=first.status), found(fab), {"d": device}, renderer)

    assert first.status["error"] == "fatal: [dc1-leaf1a]: FAILED!"
    assert first.resources["d"].resource["spec"] == {"x": 1}
    assert again.status["error"] == first.status["error"]
    assert renderer.calls == 1


def test_the_pool_goes_back_as_rewritten_and_does_not_render_again():
    fab = a_fabric(pools=POOLS)
    renderer = Renderer(rendered(pools={(POOL_CM, POOL_KEY): "pool: rewritten\n"}))
    applied = a_config_map(POOL_CM, {POOL_KEY: "pool: as emitted\n"})

    first = fabric.compose(fab, found(fab, applied), {}, renderer)
    pool = first.resources[fabric.POOLS].resource
    # What the next call finds: the ConfigMap as the Fabric wrote it.
    rewritten = a_config_map(POOL_CM, pool["data"]) | {
        "metadata": {"name": POOL_CM, "namespace": NS, "resourceVersion": "8"}
    }
    again = fabric.compose(
        a_fabric(status=first.status, pools=POOLS), found(fab, rewritten), {}, renderer
    )

    assert pool["metadata"]["name"] == POOL_CM
    assert pool["data"] == {POOL_KEY: "pool: rewritten\n"}
    assert renderer.calls == 1
    assert again.status["rendered"] == first.status["rendered"]


def test_a_changed_input_renders_again():
    fab = a_fabric()
    renderer = Renderer(rendered())
    first = fabric.compose(fab, found(fab), {}, renderer)
    required = found(fab)
    required[f"input:{NAME}-dc1"][0]["spec"]["design"] = {"a": 2}

    fabric.compose(a_fabric(status=first.status), required, {}, renderer)

    assert renderer.calls == 2


def run(composite: dict, required: dict | None = None) -> fnv1.RunFunctionResponse:
    req = fnv1.RunFunctionRequest(
        observed=fnv1.State(composite=fnv1.Resource(resource=resource.dict_to_struct(composite)))
    )
    for key, docs in (required or {}).items():
        req.required_resources[key].items.extend(
            fnv1.Resource(resource=resource.dict_to_struct(doc)) for doc in docs
        )
    return asyncio.run(FunctionRunner().RunFunction(req, None))


def test_the_runner_asks_first_and_changes_nothing_until_answered():
    rsp = run(a_fabric())

    assert set(rsp.requirements.resources) == {
        "labelled",
        f"input:{NAME}-fabric",
        f"input:{NAME}-dc1",
    }
    assert not rsp.desired.resources
    assert not resource.struct_to_dict(rsp.desired.composite.resource)
    assert not rsp.results


def test_the_runner_reports_what_is_missing_once_answered():
    fab = a_fabric()
    required = found(fab)
    required[f"input:{NAME}-dc1"] = []

    rsp = run(fab, required)

    status = resource.struct_to_dict(rsp.desired.composite.resource)["status"]
    assert status["missing"]["inputs"] == [f"{NAME}-dc1"]


def test_the_renders_own_extra_vars_come_last_and_outrank_the_fabrics(tmp_path):
    spec = {"extraVars": {"ansible_connection": "httpapi", "ansible_host": "x.l3ls.svc"}}

    command = fabric.playbook_command(
        "ansible-playbook", "inventory/hosts.yml", spec, tmp_path, tmp_path
    )

    given = [json.loads(command[i + 1]) for i, arg in enumerate(command) if arg == "-e"]
    assert given[0] == spec["extraVars"]
    assert given[-1]["ansible_connection"] == "local"


def test_without_extra_vars_only_the_renders_own_are_given(tmp_path):
    command = fabric.playbook_command(
        "ansible-playbook", "inventory/hosts.yml", {}, tmp_path, tmp_path
    )

    assert command.count("-e") == 1


@pytest.mark.parametrize("host", ["dc1-leaf1a", "DC1.POD1.LEAF2A"])
def test_a_device_is_named_as_emit_names_an_input(host):
    assert fabric.device_name(NAME, host) == f"{NAME}-{host.lower().replace('.', '-')}"
