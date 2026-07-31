"""`examples/lab/topology.yaml` must be what the generator produces today.

The lab topology is derived from the AVD model rather than written by hand, so
the cabling cannot drift from the config it runs. Committing the result buys
two things a temp file could not:

- `scripts/kind-up.sh` boots a reviewed artifact, and consumers outside this
  repo can fetch it at a tag instead of running the generator themselves.
- **An AVD upgrade that changes the cabling shows up as a failing test here.**
  Without it, the topology is regenerated at bring-up on a developer's laptop
  and a changed peering is invisible in review. This is the only guard on
  `netclab_topology`, whose sole other consumer is a script CI never runs --
  the cEOS path cannot run in Actions, since the image is licensed.

If this fails because the model legitimately moved, regenerate:

    uv run avd-topology examples/fabric/single-dc-l3ls.yaml \
        --hosts dc1-spine1,dc1-leaf1a > examples/lab/topology.yaml

and read the diff before committing it -- that diff is the point.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "examples" / "lab" / "topology.yaml"

# The arguments the committed file was generated with. `kind-up.sh` runs the
# same subset by default; `--hosts` is an input to generation, not a filter
# applied afterwards, because a link is kept only when both of its ends are
# selected.
FABRIC = "examples/fabric/single-dc-l3ls.yaml"
HOSTS = "dc1-spine1,dc1-leaf1a"


def test_committed_topology_matches_the_generator() -> None:
    generated = subprocess.run(
        [sys.executable, "-m", "function.netclab_topology", FABRIC, "--hosts", HOSTS],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert generated == GOLDEN.read_text(), (
        f"{GOLDEN.relative_to(ROOT)} is stale -- regenerate it (see this "
        f"module's docstring) and review the diff."
    )
