"""The input-kind model resolves exactly as Ansible does.

Offline -- no cluster, no pyavd render. Guards :func:`function.kinds.resolve`
and the migration that feeds it, over AVD's own corpus: the 8 bundled examples
and every molecule scenario with an inventory of its own, up to 501 devices.

This is the regression net for the collect path. It is stricter than a render
comparison on purpose: it fails on a hostvar difference even where AVD would
have rendered the same config, so a divergence cannot hide until it matters.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from function.kinds import Input, resolve
from function.verify_kinds import (
    DEFERRED,
    DEFERRED_RENDER,
    EXAMPLES_ROOT,
    _discover,
    _discover_molecule,
    render_one,
    verify_one,
)

CORPUS = _discover(EXAMPLES_ROOT) + _discover_molecule()
EXAMPLES = _discover(EXAMPLES_ROOT)


@pytest.mark.parametrize("root", CORPUS, ids=lambda p: p.name)
def test_resolves_identically_to_ansible(root: Path) -> None:
    status, _ = verify_one(root)

    if root.name in DEFERRED:
        # Expected failure. Asserting it still fails keeps a deferral from
        # rotting: if it starts resolving, this fails and says to drop it.
        assert status != "ok", (
            f"{root.name} resolves now -- remove it from verify_kinds.DEFERRED "
            f"(was deferred: {DEFERRED[root.name]})"
        )
        return

    assert status == "ok", f"{root.name}: {status}"


@pytest.mark.parametrize("root", EXAMPLES, ids=lambda p: p.name)
def test_render_reproduces_golden(root: Path) -> None:
    """Rendered configs still match the checked-in golden.

    Redundant as a check on the model -- matching hostvars render identically --
    and that is not what it is for. It is the guard against pyavd itself
    changing: an AVD upgrade slips past resolution equivalence and fails here.

    Examples only. The molecule scenarios need AVD features this path does not
    carry yet (templates loaded from files, ID pools, custom Python classes), so
    they stay on the equivalence test until those land.
    """
    status, _ = render_one(root)

    if root.name in DEFERRED_RENDER:
        assert status != "ok", (
            f"{root.name} renders clean now -- remove it from "
            f"verify_kinds.DEFERRED_RENDER (was: {DEFERRED_RENDER[root.name]})"
        )
        return

    assert status == "ok", f"{root.name}: {status}"


def test_corpus_is_not_empty() -> None:
    # The submodule is optional in a fresh worktree; an empty parametrisation
    # would make this whole file pass while testing nothing.
    assert len(CORPUS) >= 8, f"expected the AVD corpus, found {len(CORPUS)} inventories"


def test_later_input_overwrites_earlier() -> None:
    """Precedence is list order, and replacement is whole-key."""
    inputs = [
        Input("nodes", "NodeSet", {"l3leaf": {"nodes": [{"name": "leaf1"}]}},
              node_sets=["nodes"], declares=["leaf1"]),
        Input("base", "Settings", {"ntp_settings": {"servers": ["a"]}}, all_devices=True),
        Input("narrow", "Settings", {"ntp_settings": {"servers": ["b"]}}, hosts=["leaf1"]),
    ]
    assert resolve(inputs)["leaf1"]["ntp_settings"] == {"servers": ["b"]}


def test_input_applies_only_where_scoped() -> None:
    """A device sees an input only if appliesTo names it -- this is what
    replaces group membership, and what keeps two DCs' node blocks apart."""
    inputs = [
        Input("dc1", "NodeSet", {"l3leaf": {"defaults": {"loopback_ipv4_pool": "10.0.0.0/24"}}},
              node_sets=["dc1"], declares=["leaf1"]),
        Input("dc2", "NodeSet", {"l3leaf": {"defaults": {"loopback_ipv4_pool": "10.1.0.0/24"}}},
              node_sets=["dc2"], declares=["leaf2"]),
    ]
    out = resolve(inputs)
    assert out["leaf1"]["l3leaf"]["defaults"]["loopback_ipv4_pool"] == "10.0.0.0/24"
    assert out["leaf2"]["l3leaf"]["defaults"]["loopback_ipv4_pool"] == "10.1.0.0/24"


def test_undeclared_node_is_not_a_device() -> None:
    """A block may name a node the fabric does not declare -- AVD's own
    anta_runner does -- and it must not become a device."""
    inputs = [
        Input(
            "leaves",
            "NodeSet",
            {"l3leaf": {"nodes": [{"name": "leaf1"}, {"name": "ghost"}]}},
            node_sets=["leaves"],
            declares=["leaf1"],
        )
    ]
    assert set(resolve(inputs)) == {"leaf1"}
