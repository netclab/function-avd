"""What `avd-migrate` emits applies to a cluster and renders AVD's own output.

Requires a cluster from `scripts/kind-up.sh`:

    uv run pytest -m e2e tests/test_e2e_migrated_corpus.py -s

The offline suite proves the migration reproduces Ansible's hostvars and that
pyavd turns them into AVD's golden. Neither of those meets the API server. This
does the whole chain -- AVD inventory -> XRs -> Crossplane -> rendered config --
and compares the result against AVD's checked-in golden.

⚠ It exists because a hand-written fabric is not the same shape as a migrated
one. The first e2e here carried `spec.design`, passed, and hid that the Fabric
XRD marked `design` **required** -- so every Fabric the migration emits, which
names its inputs in `spec.requires` and has no `design` at all, was rejected by
the API server. No offline test could see it: they drive RunFunction directly.

No devices are pushed to: these fabrics have no `spec.push`, so nothing boots.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.e2e

CTX = os.getenv("AVD_KUBE_CONTEXT", "kind-avd")
# ⚠ One namespace per scenario, and it is not tidiness. `single-dc-l3ls`,
# `single-dc-l3ls-ipv6` and `single-dc-multipod-l3ls` all name their devices
# `dc1-leaf1a`, `dc1-spine1` and so on. Applied side by side, a lookup by
# `avd.netclab.dev/device=` label matched whichever came first and compared one
# scenario's render against another's golden -- 420 differences that said
# nothing about the code.
NS_PREFIX = "migrated"
EXAMPLES = Path("avd/ansible_collections/arista/avd/examples")
COLLECTIONS = Path("avd").resolve()
TIMEOUT = 300

# Every bundled example. They render clean offline, so anything that fails here
# is about the cluster -- which is the whole point of running them here.
# `campus-fabric` and `l2ls-fabric` write their hostnames in capitals, which is
# what found the RFC 1123 bug; `cv-pathfinder` carries ansible-vault and Jinja,
# resolved by the migration before the XRs are written.
SCENARIOS = [
    "single-dc-l3ls",
    "single-dc-l3ls-ipv6",
    "single-dc-multipod-l3ls",
    "dual-dc-l3ls",
    "l2ls-fabric",
    "campus-fabric",
    "isis-ldp-ipvpn",
    "cv-pathfinder",
]


def _kubectl(namespace: str, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["kubectl", "--context", CTX, "-n", namespace, *args],
        capture_output=True, text=True, check=check,
    )
    return proc.stdout.strip()


def _differences(path: str, ours: object, golden: object, out: list[str]) -> None:
    if type(ours) is not type(golden) and not (
        isinstance(ours, (int, float)) and isinstance(golden, (int, float))
    ):
        out.append(f"{path}: type {type(ours).__name__} != {type(golden).__name__}")
    elif isinstance(golden, dict):
        assert isinstance(ours, dict)
        for key in sorted(set(ours) | set(golden)):
            if key not in ours:
                out.append(f"{path}.{key}: only in golden")
            elif key not in golden:
                out.append(f"{path}.{key}: only in ours")
            else:
                _differences(f"{path}.{key}", ours[key], golden[key], out)
    elif isinstance(golden, list):
        assert isinstance(ours, list)
        if len(ours) != len(golden):
            out.append(f"{path}: list len {len(ours)} != {len(golden)}")
        for index, (a, b) in enumerate(zip(ours, golden)):
            _differences(f"{path}[{index}]", a, b, out)
    elif ours != golden:
        out.append(f"{path}: {ours!r} != {golden!r}")


def _wait_ready(namespace: str, fabric: str) -> str:
    deadline = time.monotonic() + TIMEOUT
    last = ""
    while time.monotonic() < deadline:
        last = _kubectl(
            namespace, "get", "fabric", fabric,
            "-o", 'jsonpath={.status.conditions[?(@.type=="Ready")].status}',
            check=False,
        )
        if last == "True":
            return last
        time.sleep(5)
    pytest.fail(f"{fabric} never went Ready within {TIMEOUT}s; last saw {last!r}")


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_migrated_xrs_render_the_golden_on_a_cluster(scenario: str) -> None:
    root = EXAMPLES / scenario
    if not root.is_dir():
        pytest.skip("AVD submodule not initialised")

    namespace = f"{NS_PREFIX}-{scenario}"
    subprocess.run(
        ["kubectl", "--context", CTX, "apply", "-f", "-"],
        input=yaml.safe_dump({"apiVersion": "v1", "kind": "Namespace",
                              "metadata": {"name": namespace}}),
        capture_output=True, text=True, check=True,
    )

    with tempfile.TemporaryDirectory() as out:
        subprocess.run(
            ["uv", "run", "avd-migrate", str(root), "--emit", out,
             "--namespace", namespace, "--collections", str(COLLECTIONS)],
            capture_output=True, text=True, check=True,
        )
        manifests = sorted(Path(out).glob("*.yaml"))
        assert len(manifests) == 1, f"expected one fabric, emitted {manifests}"
        subprocess.run(
            ["kubectl", "--context", CTX, "apply", "-f", str(manifests[0])],
            capture_output=True, text=True, check=True,
        )
        fabric = yaml.safe_load(manifests[0].read_text().split("---")[-1])["metadata"]["name"]

    _wait_ready(namespace, fabric)

    golden_dir = root / "intended" / "structured_configs"
    differences: list[str] = []
    compared = 0
    for golden_file in sorted(golden_dir.glob("*.yml")):
        hostname = golden_file.stem
        raw = _kubectl(
            namespace, "get", "device", "-l", f"avd.netclab.dev/device={hostname}",
            "-o", "jsonpath={.items[0].spec.structuredConfig}", check=False,
        )
        if not raw:
            differences.append(f"{hostname}: no Device composed")
            continue
        compared += 1
        _differences(hostname, json.loads(raw),
                     yaml.safe_load(golden_file.read_text()) or {}, differences)

    assert compared == len(list(golden_dir.glob("*.yml"))), (
        f"{scenario}: rendered {compared} of {len(list(golden_dir.glob('*.yml')))} devices"
    )
    assert not differences, f"{scenario}: {len(differences)} differences: {differences[:5]}"
