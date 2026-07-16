"""E2E: what the Fabric -> Device split actually buys, proven on a live cluster.

Requires a cluster from `scripts/kind-up.sh` plus an applied fabric:

    scripts/kind-up.sh
    kubectl --context kind-avd apply -f examples/fabric/single-dc-l3ls.yaml
    uv run pytest -m e2e

The scenario: pause the Fabric so it stops propagating, patch a Device directly,
and watch the change reach the end of the chain (configHash + rendered
ConfigMap). Then unpause and watch the Fabric take it back.

Together these pin the contract of the two-layer split, which nothing else
checks: a Device is a *real* reconcile unit -- it renders on its own, which is
what lets a config-push managed resource attach there -- but it is *not*
authoritative, because the Fabric owns spec.structuredConfig and reverts drift.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable

import pytest

pytestmark = pytest.mark.e2e

CTX = os.getenv("AVD_KUBE_CONTEXT", "kind-avd")
NS = os.getenv("AVD_NAMESPACE", "default")
FABRIC = "single-dc-l3ls"
HOSTNAME = "dc1-leaf1a"
DRIFT_NTP = "drift.pool.ntp.org"
GOLDEN_NTP = "0.pool.ntp.org"

# Generous: a Device whose watch circuit breaker has tripped reconciles on a
# throttle rather than immediately, so propagation can take ~15s, not ~1s.
TIMEOUT = 120


def _kubectl(*args: str) -> str:
    proc = subprocess.run(
        ["kubectl", "--context", CTX, "-n", NS, *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _one_named(kind: str) -> str:
    name = _kubectl(
        "get", kind, "-l", f"avd.netclab.dev/device={HOSTNAME}",
        "-o", "jsonpath={.items[0].metadata.name}",
    )
    if not name:
        pytest.skip(
            f"no {kind} for {HOSTNAME} on context {CTX} -- run scripts/kind-up.sh and "
            f"apply examples/fabric/{FABRIC}.yaml"
        )
    return name


def _status(device: str) -> dict:
    return json.loads(_kubectl("get", "device", device, "-o", "jsonpath={.status}"))


def _eos_cfg(configmap: str) -> str:
    return _kubectl("get", "cm", configmap, "-o", r"jsonpath={.data.eos\.cfg}")


def _wait_for_hash(device: str, want: Callable[[str], bool], what: str) -> str:
    deadline = time.monotonic() + TIMEOUT
    seen = None
    while time.monotonic() < deadline:
        seen = _status(device).get("configHash")
        if want(seen):
            return seen
        time.sleep(2)
    pytest.fail(f"timed out after {TIMEOUT}s waiting for {what} (configHash={seen})")


@pytest.fixture
def never_left_paused():
    """A failed assertion must not leave the fabric paused for the next run."""
    yield
    subprocess.run(
        ["kubectl", "--context", CTX, "-n", NS, "annotate", "fabric", FABRIC,
         "crossplane.io/paused-", "--overwrite"],
        capture_output=True,
        text=True,
    )


def test_device_renders_on_its_own_and_fabric_reclaims_drift(never_left_paused) -> None:
    device, configmap = _one_named("device"), _one_named("cm")

    baseline = _status(device)
    baseline_hash, baseline_time = baseline["configHash"], baseline["lastRenderedTime"]
    assert GOLDEN_NTP in _eos_cfg(configmap), "fabric is not at its golden config to start"

    _kubectl("annotate", "fabric", FABRIC, "crossplane.io/paused=true", "--overwrite")
    assert "ReconcilePaused" in _kubectl(
        "get", "fabric", FABRIC,
        "-o", 'jsonpath={.status.conditions[?(@.type=="Synced")].reason}',
    )

    # Drift: with nothing propagating from above, patch the Device itself.
    _kubectl(
        "patch", "device", device, "--type=json",
        "-p", json.dumps([{
            "op": "replace",
            "path": "/spec/structuredConfig/ntp/servers/0/name",
            "value": DRIFT_NTP,
        }]),
    )

    _wait_for_hash(device, lambda h: h != baseline_hash, "the Device to re-render its drift")
    # The change reached the end of the chain with the Fabric paused: the Device
    # layer is live by itself.
    assert f"ntp server vrf MGMT {DRIFT_NTP} prefer" in _eos_cfg(configmap)
    drift_time = _status(device)["lastRenderedTime"]
    assert drift_time != baseline_time, "configHash changed but lastRenderedTime did not bump"

    # Reclaim: the Fabric owns spec.structuredConfig and rewrites it every pass.
    _kubectl("annotate", "fabric", FABRIC, "crossplane.io/paused-", "--overwrite")

    _wait_for_hash(device, lambda h: h == baseline_hash, "the Fabric to reclaim the drift")
    # Back to a byte-identical config, not merely a valid one.
    assert _kubectl(
        "get", "device", device,
        "-o", "jsonpath={.spec.structuredConfig.ntp.servers[0].name}",
    ) == GOLDEN_NTP
    assert f"ntp server vrf MGMT {GOLDEN_NTP} prefer" in _eos_cfg(configmap)
    assert _status(device)["lastRenderedTime"] != drift_time


def test_steady_state_is_idempotent() -> None:
    """No perpetual reconcile: at rest, nothing may rewrite the rendered config.

    Guards the rule that `lastRenderedTime` is only bumped when `configHash`
    actually changes -- a status value that moves every pass would spin the
    Device against its ConfigMap forever.
    """
    device, configmap = _one_named("device"), _one_named("cm")

    before = _status(device)
    rv_before = _kubectl("get", "cm", configmap, "-o", "jsonpath={.metadata.resourceVersion}")

    time.sleep(30)

    after = _status(device)
    rv_after = _kubectl("get", "cm", configmap, "-o", "jsonpath={.metadata.resourceVersion}")

    assert after["configHash"] == before["configHash"]
    assert after["lastRenderedTime"] == before["lastRenderedTime"]
    assert rv_after == rv_before, "the rendered ConfigMap is being rewritten at rest"
