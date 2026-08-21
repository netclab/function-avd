"""The two things `eos_designs-twodc-5stage-clos` needs, proven against golden.

That scenario is the only fabric in AVD's corpus that turns on `pool_manager`,
and it also pins two `.j2` addressing templates. Both are unreachable through
pyavd's public API as written — Jinja is not implemented, and nothing composes a
pool — so it renders as a failure and the reason it reports is whichever one it
meets first.

This is the offline oracle for both. With the pool supplied and the templates
replaced by the class AVD's own schema offers instead, the render must reproduce
AVD's checked-in golden **exactly**. Without that, a later failure in a cluster
could be the pool, the class or the render, and nothing would say which.

⚠ It does not claim the *migrated* design renders. It pins `.j2` and always
will; the substitution below is what a fabric would carry instead.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
import yaml

from function.kinds import resolve
from function.migrate import migrate

pytestmark = pytest.mark.corpus

SCENARIO = Path(
    "avd/ansible_collections/arista/avd/extensions/molecule/eos_designs-twodc-5stage-clos"
)
COLLECTIONS = Path("avd").resolve()

#: what the scenario's own group_vars name, relative to the working directory
POOL = Path("intended/data/test-ids.yml")


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


def _use_compat_class(design: dict) -> int:
    """Point the node-type block at the class instead of the templates."""
    swapped = 0
    for entry in design.get("node_type_keys") or []:
        block = entry.get("ip_addressing") if isinstance(entry, dict) else None
        if isinstance(block, dict) and any(str(v).endswith(".j2") for v in block.values()):
            entry["ip_addressing"] = {
                "python_module": "function.avd_compat",
                "python_class_name": "AvdIpAddressingV2Spine",
            }
            swapped += 1
    return swapped


def _point_at(design: dict, pool_file: Path) -> bool:
    """Redirect `pools_file` at a copy of the pool.

    ⚠ When `pools_file` is set it is used **verbatim**, relative to the current
    working directory — it is *not* joined to the PoolManager's output_dir, which
    only supplies the default path. Getting that wrong silently assigns fresh IDs
    and every address moves.
    """
    node_id = (design.get("fabric_numbering") or {}).get("node_id")
    if isinstance(node_id, dict) and node_id.get("pools_file"):
        node_id["pools_file"] = str(pool_file)
        return True
    return False


def test_the_pool_and_the_compat_class_reproduce_avds_golden() -> None:
    if not SCENARIO.is_dir():
        pytest.skip("AVD submodule not initialised")

    from pyavd.api.pool_manager import PoolManager

    from function.engine import render_structured_configs

    fabric = migrate(SCENARIO, collections=COLLECTIONS)[0]
    assert sum(_use_compat_class(i.design) for i in fabric.inputs) == 1, (
        "expected exactly one node-type block pinning .j2 addressing"
    )

    with tempfile.TemporaryDirectory() as workdir:
        # A copy, so a run can never write into AVD's own tree.
        pool_file = Path(workdir) / POOL.name
        shutil.copy(SCENARIO / POOL, pool_file)
        assert any(_point_at(i.design, pool_file) for i in fabric.inputs), (
            "the scenario should still name a pools_file"
        )

        rendered = render_structured_configs(
            resolve(fabric.inputs), pool_manager=PoolManager(Path(workdir))
        )

        golden_dir = SCENARIO / "intended" / "structured_configs"
        differences: list[str] = []
        compared = 0
        for hostname, structured in sorted(rendered.items()):
            target = golden_dir / f"{hostname}.yml"
            if not target.is_file():
                continue
            compared += 1
            _differences(hostname, structured,
                         yaml.safe_load(target.read_text()) or {}, differences)

        assert compared == 26, f"expected AVD's 26 devices, compared {compared}"
        assert not differences, (
            f"{len(differences)} differences: {differences[:5]}"
        )


def test_the_compat_class_only_changes_p2p_uplinks() -> None:
    """Everything else falls through to AVD's own implementation, so selecting
    the class cannot quietly move an address it was not written for."""
    from pyavd.api.ip_addressing import AvdIpAddressing

    from function.avd_compat import AvdIpAddressingV2Spine

    overridden = {
        name for name in vars(AvdIpAddressingV2Spine)
        if not name.startswith("_") and callable(getattr(AvdIpAddressingV2Spine, name))
    }
    assert overridden == {"p2p_uplinks_ip", "p2p_uplinks_peer_ip"}, overridden
    assert issubclass(AvdIpAddressingV2Spine, AvdIpAddressing)
