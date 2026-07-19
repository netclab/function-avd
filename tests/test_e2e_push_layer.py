"""E2E: the config push closes the loop from the Device XR to the box itself.

Requires the netclab lab (cEOS nodes + provider-http) with the lab fabric:

    WITH_NETCLAB=1 scripts/kind-up.sh
    kubectl --context kind-avd apply -k examples/lab/
    uv run pytest -m e2e

Skips (rather than fails) when the lab is not running, so the plain device-layer
e2e can still be exercised on a cluster without cEOS.

The scenario mirrors the device-layer test one level down: the lab device is
brought to its golden running config by the push Request, drifted by hand over
its own CLI, and the provider's observe/update loop must take the drift back --
byte-identical, which EOS's own `show running-config digest` certifies.
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
# The first LAB_HOSTS default: guaranteed to exist whenever the lab runs at all.
HOSTNAME = os.getenv("AVD_LAB_HOST", "dc1-spine1")

# Reclaim rides provider-http's poll: OBSERVE sees the digest mismatch, UPDATE
# replays the full replace session. One poll interval plus the push itself;
# five intervals of budget, same reasoning as the device-layer test.
TIMEOUT = 300


def _kubectl(*args: str) -> str:
    proc = subprocess.run(
        ["kubectl", "--context", CTX, "-n", NS, *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _cli(command: str) -> str:
    """Run an EOS CLI command on the lab device, privileged."""
    return _kubectl("exec", HOSTNAME, "--", "Cli", "-p", "15", "-c", command)


def _device_name() -> str:
    name = _kubectl(
        "get", "device", "-l", f"avd.netclab.dev/device={HOSTNAME}",
        "-o", "jsonpath={.items[0].metadata.name}",
    )
    if not name:
        pytest.skip(f"no Device for {HOSTNAME} on context {CTX}")
    return name


def _push_status(device: str) -> dict:
    status = json.loads(_kubectl("get", "device", device, "-o", "jsonpath={.status}"))
    return status.get("push") or {}


def _running_digest() -> str:
    return _cli("show running-config digest").strip()


def _report(what: str, started: float, budget: int) -> None:
    print(f"    [phase] {what}: {time.monotonic() - started:.1f}s of {budget}s budget")


def _wait_for(want: Callable[[], bool], what: str) -> None:
    started = time.monotonic()
    deadline = started + TIMEOUT
    while time.monotonic() < deadline:
        if want():
            _report(what, started, TIMEOUT)
            return
        time.sleep(5)
    pytest.fail(f"timed out after {TIMEOUT}s waiting for {what}")


@pytest.fixture
def deployed() -> tuple[str, str]:
    """The lab Device once its push has converged; returns (device, digest).

    Skips when the lab is not running (no cEOS pod, or a fabric without
    spec.push): this file only makes sense against `examples/lab/`.
    """
    probe = subprocess.run(
        ["kubectl", "--context", CTX, "-n", NS, "get", "pod", HOSTNAME],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        pytest.skip(f"no cEOS pod {HOSTNAME} -- bring the lab up with WITH_NETCLAB=1")
    device = _device_name()
    if not _push_status(device).get("configHash"):
        pytest.skip(f"Device {device} has no spec.push -- apply -k examples/lab/")
    _wait_for(
        lambda: bool(_push_status(device).get("digest")),
        "the initial push to converge (Deployed digest recorded)",
    )
    return device, _push_status(device)["digest"]


def test_push_reclaims_drift_on_the_box(deployed) -> None:
    device, golden = deployed

    # The recorded digest is not bookkeeping -- it must be what the box reports.
    assert _running_digest() == golden, "device does not run the recorded golden config"

    # Drift: a change made on the device itself, behind the model's back.
    _cli("configure\nvlan 999\nname drift-test")
    assert _running_digest() != golden, "drift did not change the running-config digest"

    # Reclaim: OBSERVE spots the digest mismatch, UPDATE replays the session.
    _wait_for(
        lambda: _running_digest() == golden,
        "the push Request to reclaim the on-box drift",
    )
    assert "drift-test" not in _cli("show vlan brief")

    # The digest is deterministic across a full replace, so reclaiming drift
    # restores the *recorded* state rather than defining a new one -- and the
    # idempotency rule (bump only on change) keeps lastDeployedTime put.
    assert _push_status(device).get("digest") == golden


def test_push_is_idempotent_at_rest(deployed) -> None:
    """At rest the loop only reads: OBSERVE matches, nothing rewrites the box.

    Guards the recording rule in fn._reconcile_push: an observe
    response may never redefine the golden digest once one is recorded, so a
    quiet device must produce a byte-stable Device push status.
    """
    device, golden = deployed

    before = _push_status(device)
    time.sleep(30)
    after = _push_status(device)

    assert after == before, "push status churns at rest"
    assert _running_digest() == golden
