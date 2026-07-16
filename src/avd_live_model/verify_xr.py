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

# Examples that are known not to fold into a single fabric document, with the
# reason. Expected-failure semantics: a deferred example that fails is not a
# regression, but one that starts passing IS reported (the deferral is stale and
# should be removed), so this list can never silently hide a fixed example.
DEFERRED = {
    "campus-fabric": "aaa_settings.radius differs by role; no node-scoped equivalent",
    "cv-pathfinder": "SD-WAN: WAN gateway across 2 routers + ansible-vault secrets",
}


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
    deferred = 0
    for example_dir in examples:
        status, diffs = verify_one(example_dir)
        n = len(list((example_dir / "intended" / "structured_configs").glob("*.yml")))
        ok = status.startswith("ok")
        reason = DEFERRED.get(example_dir.name)
        if reason and not ok:
            mark, deferred = "DEFER", deferred + 1
            status = f"deferred: {reason}"
        elif reason and ok:
            # Stale deferral: it folds now, so drop it from DEFERRED.
            mark = "XPASS"
            failures += 1
            status = "folds now -- remove from DEFERRED"
        elif ok:
            mark = "OK  "
        else:
            mark = "FAIL"
            failures += 1
        print(f"[{mark}] {example_dir.name:26s} devices={n:2d}  {status}")
    print()
    expected = len(examples) - deferred
    print(
        f"{expected - failures}/{expected} expected examples reproduce golden via the "
        f"Fabric-XR fold ({deferred} deferred)."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
