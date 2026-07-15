"""Milestone 1: prove the pyavd path reproduces AVD's golden output.

Reads an AVD Ansible example, rebuilds ``all_inputs`` the way Ansible would,
runs the pyavd pipeline, and deep-diffs the resulting structured configs against
the example's checked-in ``intended/structured_configs/*.yml``.

Green = the engine our Crossplane function will wrap is faithful to AVD.

Usage:
    uv run avd-verify [EXAMPLE_DIR]
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from .ansible_inputs import build_all_inputs
from .engine import render_structured_configs

DEFAULT_EXAMPLE = (
    "avd/ansible_collections/arista/avd/examples/single-dc-l3ls"
)


def _diff(path: str, ours, gold, out: list[str]) -> None:
    """Collect human-readable differences between two nested structures."""
    if type(ours) is not type(gold) and not (
        isinstance(ours, (int, float)) and isinstance(gold, (int, float))
    ):
        out.append(f"{path}: type {type(ours).__name__} != {type(gold).__name__}")
        return
    if isinstance(gold, dict):
        for k in sorted(set(ours) | set(gold)):
            if k not in ours:
                out.append(f"{path}.{k}: missing (only in golden)")
            elif k not in gold:
                out.append(f"{path}.{k}: extra (only in ours)")
            else:
                _diff(f"{path}.{k}", ours[k], gold[k], out)
    elif isinstance(gold, list):
        if len(ours) != len(gold):
            out.append(f"{path}: list len {len(ours)} != {len(gold)}")
        for i, (a, b) in enumerate(zip(ours, gold)):
            _diff(f"{path}[{i}]", a, b, out)
    elif ours != gold:
        out.append(f"{path}: {ours!r} != {gold!r}")


def verify(example_dir: str | Path) -> int:
    example_dir = Path(example_dir)
    golden_dir = example_dir / "intended" / "structured_configs"

    all_inputs = build_all_inputs(example_dir)
    rendered = render_structured_configs(all_inputs)

    # Guard: the set of rendered devices must match the golden set, otherwise a
    # transcoder that produces zero devices would falsely report "all match".
    golden_hosts = {p.stem for p in golden_dir.glob("*.yml")}
    rendered_hosts = set(rendered)
    if rendered_hosts != golden_hosts:
        missing = sorted(golden_hosts - rendered_hosts)
        extra = sorted(rendered_hosts - golden_hosts)
        print(
            f"DEVICE SET MISMATCH: rendered {len(rendered_hosts)} vs golden "
            f"{len(golden_hosts)}"
        )
        if missing:
            print(f"  not rendered (in golden): {missing}")
        if extra:
            print(f"  rendered but no golden:   {extra}")
        print()
        print("MISMATCH: device sets differ, cannot claim reproduction.")
        return 1

    total_diffs = 0
    for hostname in sorted(rendered):
        golden_file = golden_dir / f"{hostname}.yml"
        gold = yaml.safe_load(golden_file.read_text()) or {}
        diffs: list[str] = []
        _diff(hostname, rendered[hostname], gold, diffs)
        status = "OK  " if not diffs else "DIFF"
        print(f"[{status}] {hostname}  ({len(diffs)} diffs)")
        for d in diffs[:20]:
            print(f"        {d}")
        if len(diffs) > 20:
            print(f"        ... and {len(diffs) - 20} more")
        total_diffs += len(diffs)

    print()
    if total_diffs == 0:
        print(f"MATCH: all {len(rendered)} devices reproduce golden structured config.")
        return 0
    print(f"MISMATCH: {total_diffs} total diffs across the fabric.")
    return 1


def main() -> int:
    example = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_EXAMPLE
    return verify(example)


if __name__ == "__main__":
    raise SystemExit(main())
