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

# Crossplane polls on a 60s interval by default, and a reclaim waits on it twice
# over (the Fabric noticing the unpause, then the Device re-rendering). Measured
# on a fresh kind cluster: drift 2-41s, reclaim 127.5s -- i.e. ~2 poll intervals
# plus overhead, and past the 120s this used to allow. Five intervals of budget;
# it only costs time when something is genuinely broken.
TIMEOUT = 300

# `Ready=True` means the last reconcile succeeded -- not that the create burst
# has stopped rewriting ConfigMaps. Both tests measure against a baseline, so
# both have to start from a cluster that has actually gone quiet, or they race.
QUIET_FOR = 15          # seconds a ConfigMap must go unwritten to count as settled
SETTLE_TIMEOUT = 240


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


def _cm_rv(configmap: str) -> str:
    return _kubectl("get", "cm", configmap, "-o", "jsonpath={.metadata.resourceVersion}")


def _synced_reason() -> str:
    return _kubectl(
        "get", "fabric", FABRIC,
        "-o", 'jsonpath={.status.conditions[?(@.type=="Synced")].reason}',
    )


def _report(what: str, started: float, budget: int) -> None:
    """Report how long a wait took, against the budget it had.

    A timeout tells you which wait blew its budget, but nothing about how close
    the others came -- so a run that passes in 287s looks identical whether every
    phase had room to spare or one of them nearly missed. pytest shows this on
    failure, and streams it live under `-s` (which is how CI runs e2e).
    """
    print(f"    [phase] {what}: {time.monotonic() - started:.1f}s of {budget}s budget")


def _wait_for_hash(device: str, want: Callable[[str], bool], what: str) -> str:
    started = time.monotonic()
    deadline = started + TIMEOUT
    seen = None
    while time.monotonic() < deadline:
        seen = _status(device).get("configHash")
        if want(seen):
            _report(what, started, TIMEOUT)
            return seen
        time.sleep(2)
    pytest.fail(f"timed out after {TIMEOUT}s waiting for {what} (configHash={seen})")


def _wait_for(want: Callable[[], bool], what: str) -> None:
    started = time.monotonic()
    deadline = started + TIMEOUT
    while time.monotonic() < deadline:
        if want():
            _report(what, started, TIMEOUT)
            return
        time.sleep(2)
    pytest.fail(f"timed out after {TIMEOUT}s waiting for {what}")


def _wait_until_quiesced(configmap: str) -> str:
    """Block until the rendered ConfigMap stops being rewritten; return its rv.

    This is also half of the idempotency assertion: a model that never reaches a
    fixed point never goes quiet, so this times out and fails -- which is the
    real perpetual-reconcile signal, as opposed to the settling churn that a
    fresh cluster produces for a while after `Ready=True`.
    """
    started = time.monotonic()
    deadline = started + SETTLE_TIMEOUT
    rv, quiet_since = None, 0.0
    while time.monotonic() < deadline:
        seen = _cm_rv(configmap)
        if seen != rv:
            rv, quiet_since = seen, time.monotonic()
        elif time.monotonic() - quiet_since >= QUIET_FOR:
            # Includes the QUIET_FOR window itself, so the floor is ~15s.
            _report("cluster to go quiet", started, SETTLE_TIMEOUT)
            return rv
        time.sleep(2)
    pytest.fail(
        f"{configmap} never went {QUIET_FOR}s without a write within "
        f"{SETTLE_TIMEOUT}s -- perpetual reconcile?"
    )


@pytest.fixture
def settled() -> tuple[str, str]:
    """A device and its ConfigMap, once the cluster has stopped writing to them."""
    device, configmap = _one_named("device"), _one_named("cm")
    _wait_until_quiesced(configmap)
    return device, configmap


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


def test_device_renders_on_its_own_and_fabric_reclaims_drift(settled, never_left_paused) -> None:
    device, configmap = settled

    baseline = _status(device)
    baseline_hash, baseline_time = baseline["configHash"], baseline["lastRenderedTime"]
    assert GOLDEN_NTP in _eos_cfg(configmap), "fabric is not at its golden config to start"

    _kubectl("annotate", "fabric", FABRIC, "crossplane.io/paused=true", "--overwrite")
    # Crossplane needs a moment to notice the annotation. Asserting on the
    # condition straight after writing it only held on an already-idle cluster.
    _wait_for(
        lambda: "ReconcilePaused" in _synced_reason(),
        "the Fabric to report ReconcilePaused",
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


def test_steady_state_is_idempotent(settled) -> None:
    """No perpetual reconcile: the model reaches a fixed point and stays there.

    Guards the rule that `lastRenderedTime` is only bumped when `configHash`
    actually changes -- a status value that moves every pass would spin the
    Device against its ConfigMap forever.

    Reaching the fixed point is asserted by `settled` itself, which fails if the
    ConfigMap never goes quiet. What is left to check is that it *stays* put.
    """
    device, configmap = settled

    before, rv_before = _status(device), _cm_rv(configmap)
    time.sleep(30)
    after, rv_after = _status(device), _cm_rv(configmap)

    assert rv_after == rv_before, "the rendered ConfigMap is being rewritten at rest"
    assert after["configHash"] == before["configHash"]
    assert after["lastRenderedTime"] == before["lastRenderedTime"]
