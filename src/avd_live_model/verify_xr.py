"""Milestone 2 harness: prove the Fabric-XR path reproduces AVD's golden output.

For each AVD example, fold it into a single fabric document (`spec.design`),
render it the way the composite function will (`render_fabric_design`), and
deep-diff against the checked-in `intended/structured_configs`.

This is the XR-level analogue of `avd-verify`, and doubles as the regression net
for the Ansible->XR fold.

Usage:
    uv run avd-verify-xr [EXAMPLE_DIR ...]   # default: every bundled example
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from .ansible_inputs import build_all_inputs
from .engine import render_fabric_design
from .verify_example import _diff
from .xr import fabric_design_from_inputs

EXAMPLES_ROOT = Path("avd/ansible_collections/arista/avd/examples")


def _discover(root: Path) -> list[Path]:
    return sorted(
        d
        for d in root.iterdir()
        if (d / "inventory.yml").is_file()
        and (d / "intended" / "structured_configs").is_dir()
    )


def verify_one(example_dir: Path) -> tuple[str, int]:
    """Return (status, diff_count). status in {ok, diff, conflict, error}."""
    per_host = build_all_inputs(example_dir)
    fabric_name, design, conflicts = fabric_design_from_inputs(per_host)
    try:
        rendered = render_fabric_design(design, fabric_name)
    except Exception as exc:  # noqa: BLE001 - surface any AVD/render failure
        detail = f"conflicts={sorted(conflicts)} " if conflicts else ""
        return f"error: {detail}{type(exc).__name__}: {str(exc)[:80]}", -1

    golden_dir = example_dir / "intended" / "structured_configs"
    total = 0
    for hostname in sorted(rendered):
        gold = yaml.safe_load((golden_dir / f"{hostname}.yml").read_text()) or {}
        out: list[str] = []
        _diff(hostname, rendered[hostname], gold, out)
        total += len(out)
    if total:
        return f"diff ({total})", total
    return "ok", total


def main() -> int:
    args = [Path(a) for a in sys.argv[1:]]
    examples = args or _discover(EXAMPLES_ROOT)
    failures = 0
    for example_dir in examples:
        status, diffs = verify_one(example_dir)
        n = len(list((example_dir / "intended" / "structured_configs").glob("*.yml")))
        mark = "OK  " if status.startswith("ok") else "FAIL"
        if not status.startswith("ok"):
            failures += 1
        print(f"[{mark}] {example_dir.name:26s} devices={n:2d}  {status}")
    print()
    print(f"{len(examples) - failures}/{len(examples)} examples reproduce golden via the Fabric-XR fold.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
