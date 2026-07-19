"""Milestone 1: the pyavd engine reproduces AVD's golden structured config.

Offline -- no cluster. This is `avd-verify` as assertions instead of a report.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from function.ansible_inputs import build_all_inputs
from function.engine import render_structured_configs
from function.verify_example import _diff

EXAMPLES_ROOT = Path("avd/ansible_collections/arista/avd/examples")

# Every bundled example that reproduces golden from full Ansible inputs.
# cv-pathfinder is deferred (ansible-vault secrets); `common` holds shared vars
# and is not a runnable fabric.
EXAMPLES = [
    "single-dc-l3ls",
    "single-dc-l3ls-ipv6",
    "single-dc-multipod-l3ls",
    "dual-dc-l3ls",
    "campus-fabric",
    "l2ls-fabric",
    "isis-ldp-ipvpn",
]


@pytest.mark.parametrize("example", EXAMPLES)
def test_reproduces_golden_structured_config(example: str) -> None:
    example_dir = EXAMPLES_ROOT / example
    golden_dir = example_dir / "intended" / "structured_configs"

    rendered = render_structured_configs(build_all_inputs(example_dir))

    # The device sets must match first: "zero diffs" over an empty render would
    # otherwise pass vacuously.
    assert set(rendered) == {p.stem for p in golden_dir.glob("*.yml")}

    diffs: list[str] = []
    for hostname in sorted(rendered):
        gold = yaml.safe_load((golden_dir / f"{hostname}.yml").read_text()) or {}
        _diff(hostname, rendered[hostname], gold, diffs)
    assert diffs == [], "\n".join(diffs[:20])
