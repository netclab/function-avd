"""The function end to end, as Crossplane's own guide tests one: served locally, called by
`crossplane composition render`.

What the in-process tests do not reach is here: gRPC, the Compositions in apis/, and
Crossplane's own loop of calling again with what the function asked for. The function
runs here, the render engine in Docker, at the Crossplane a cluster runs -- never
`stable`, which moves with no commit here. The CLI is released on its own, under its
own numbers, and only drives it.

Skipped without the CLI, Docker, or the `avd` submodule.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parent.parent
REPO = ROOT / "avd" / "ansible_collections" / "arista" / "avd" / "examples" / "single-dc-l3ls"
NS = "l3ls"

CROSSPLANE = shutil.which("crossplane")
# Crossplane itself, as the cluster's helm chart installs it; not the CLI's XP_VERSION.
ENGINE = "v2.4.2"


def _docker() -> bool:
    docker = shutil.which("docker")
    if not docker:
        return False
    return subprocess.run([docker, "info"], capture_output=True, check=False).returncode == 0


pytestmark = pytest.mark.skipif(
    CROSSPLANE is None or not REPO.is_dir() or not _docker(),
    reason="needs the crossplane CLI, Docker and the avd submodule",
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    """The function listening on a free port, its log, and the Function pointing at it."""
    work = tmp_path_factory.mktemp("served")
    port = _free_port()
    log = work / "function.log"
    with log.open("w") as out:
        server = subprocess.Popen(
            # Every interface, as the function's own default: the engine calls it from
            # a container, and a loopback address is the container's own.
            [sys.executable, "-m", "function.main", "--insecure", "--debug"]
            + ["--address", f"0.0.0.0:{port}"],
            cwd=ROOT,
            stdout=out,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), 1).close()
                break
            except OSError:
                if time.monotonic() > deadline or server.poll() is not None:
                    pytest.fail(f"the function never listened: {log.read_text()}")
                time.sleep(0.2)
        function = work / "functions.yaml"
        function.write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "pkg.crossplane.io/v1",
                    "kind": "Function",
                    "metadata": {
                        "name": "netclab-function-avd",
                        "annotations": {
                            "render.crossplane.io/runtime": "Development",
                            "render.crossplane.io/runtime-development-target": f"localhost:{port}",
                        },
                    },
                    "spec": {"package": "xpkg.crossplane.io/netclab/function-avd:v0.0.0"},
                }
            )
        )
        yield function, log
    finally:
        server.terminate()
        server.wait(timeout=10)


def render(served, work: Path, xr: dict, composition: str, required=(), observed=()) -> list[dict]:
    """What `crossplane composition render` prints for `xr`, the XR first."""
    function, _ = served
    files = {"xr.yaml": [xr], "required.yaml": list(required), "observed.yaml": list(observed)}
    for name, docs in files.items():
        (work / name).write_text(yaml.safe_dump_all(docs))
    command = [CROSSPLANE, "composition", "render", str(work / "xr.yaml")]
    command += [str(ROOT / composition), str(function), f"--crossplane-version={ENGINE}"]
    if required:
        command.append(f"--required-resources={work / 'required.yaml'}")
    if observed:
        command.append(f"--observed-resources={work / 'observed.yaml'}")
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    return [doc for doc in yaml.safe_load_all(done.stdout) if doc]


def renders(served) -> int:
    """How many renders the function has logged so far."""
    _, log = served
    return log.read_text().count("rendered")


@pytest.fixture(scope="module")
def emitted() -> tuple[dict, list[dict]]:
    """single-dc-l3ls as netadopt emits it, placed in NS: the Fabric, and the rest."""
    command = [str(Path(sys.executable).parent / "netadopt"), "avd", "emit", str(REPO)]
    command += ["--playbook", "build.yml"]
    done = subprocess.run(command, capture_output=True, text=True, check=True)
    docs = [doc for doc in yaml.safe_load_all(done.stdout) if doc]
    for doc in docs:
        doc["metadata"]["namespace"] = NS
    fab = next(doc for doc in docs if doc["kind"] == "Fabric")
    return fab, [doc for doc in docs if doc is not fab]


@pytest.fixture(scope="module")
def first(served, emitted, tmp_path_factory) -> list[dict]:
    fab, rest = emitted
    return render(
        served, tmp_path_factory.mktemp("first"), fab, "apis/fabric/composition.yaml", rest
    )


def test_the_fabric_composes_a_device_per_host_and_the_eapi_secret(first):
    kinds = sorted(doc["kind"] for doc in first[1:])

    assert kinds == ["Device"] * 8 + ["Secret"]
    assert first[0]["status"]["error"] == ""
    assert first[0]["status"]["rendered"].startswith("sha256:")


def test_a_device_carries_avds_own_structured_config(first):
    device = next(d for d in first if d["metadata"].get("name") == "single-dc-l3ls-dc1-leaf1a")
    golden = REPO / "intended" / "structured_configs" / "dc1-leaf1a.yml"

    assert device["spec"]["structuredConfig"] == yaml.safe_load(golden.read_text())


def test_a_second_call_with_nothing_changed_does_not_render(
    served, emitted, first, tmp_path_factory
):
    fab, rest = emitted
    status = {k: v for k, v in first[0]["status"].items() if k != "conditions"}
    composed = [dict(doc, metadata={**doc["metadata"], "namespace": NS}) for doc in first[1:]]
    before = renders(served)

    again = render(
        served,
        tmp_path_factory.mktemp("again"),
        {**fab, "status": status},
        "apis/fabric/composition.yaml",
        rest,
        composed,
    )

    assert renders(served) == before
    assert again[0]["status"]["rendered"] == status["rendered"]
    assert sorted(d["metadata"]["name"] for d in again[1:]) == sorted(
        d["metadata"]["name"] for d in first[1:]
    )


def test_a_device_composes_its_eos_cfg_and_its_request(served, first, tmp_path_factory):
    device = next(d for d in first if d["metadata"].get("name") == "single-dc-l3ls-dc1-leaf1a")
    device = dict(device, metadata={**device["metadata"], "namespace": NS})

    out = render(served, tmp_path_factory.mktemp("device"), device, "apis/device/composition.yaml")

    by_kind = {doc["kind"]: doc for doc in out[1:]}
    golden = REPO / "intended" / "configs" / "dc1-leaf1a.cfg"
    assert by_kind["ConfigMap"]["data"]["eos.cfg"] == golden.read_text()
    assert by_kind["Request"]["metadata"]["name"] == "single-dc-l3ls-dc1-leaf1a"
    assert out[0]["status"]["invalid"] == []


def test_a_fabric_input_composes_nothing(served, emitted, tmp_path_factory):
    _, rest = emitted
    fabric_input = next(doc for doc in rest if doc["kind"] == "FabricInput")

    out = render(
        served, tmp_path_factory.mktemp("input"), fabric_input, "apis/fabricinput/composition.yaml"
    )

    assert [doc["kind"] for doc in out] == ["FabricInput"]
