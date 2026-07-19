"""Milestone 2: the Ansible -> Fabric-XR fold still reproduces golden.

Offline -- no cluster. Guards `xr.fabric_design_from_inputs` (block union +
defaults push-down), which is the part most likely to silently lose a value.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from function.verify_xr import DEFERRED, EXAMPLES_ROOT, _discover, verify_one


@pytest.mark.parametrize("example_dir", _discover(EXAMPLES_ROOT), ids=lambda p: p.name)
def test_fold_reproduces_golden(example_dir: Path) -> None:
    status, _ = verify_one(example_dir)

    if example_dir.name in DEFERRED:
        # Expected failure. Asserting it still fails means a deferral can never
        # rot: if the example starts folding, this fails and tells us to drop it.
        assert not status.startswith("ok"), (
            f"{example_dir.name} folds now -- remove it from verify_xr.DEFERRED "
            f"(was deferred: {DEFERRED[example_dir.name]})"
        )
        return

    assert status == "ok", f"{example_dir.name}: {status}"
