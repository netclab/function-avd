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

from function.kinds import Input, classify, resolve
from function.verify_kinds import (
    DEFERRED,
    DEFERRED_RENDER,
    EXAMPLES_ROOT,
    _by_category,
    _discover,
    _discover_molecule,
    inputs_from_inventory,
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
        Input("base", "SettingSet", {"ntp_settings": {"servers": ["a"]}}, all_devices=True),
        Input("narrow", "SettingSet", {"ntp_settings": {"servers": ["b"]}}, hosts=["leaf1"]),
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


def test_network_services_is_classified_under_either_spelling() -> None:
    """AVD 6.x reads two spellings for the same content, and both are services.

    `shared_utils/filtered_tenants.py` reads `inputs.network_services` and then
    the dynamic keys named by `network_services_keys` (default `tenants`).
    Classifying the native spelling as `SettingSet` would put a services
    document -- its BGP passwords and OSPF auth keys with it -- in the kind that
    owns fabric-wide settings, and RBAC is granted per kind.

    Latent when written: of 25 inventories in AVD's corpus only
    `eos_designs_unit_tests` uses the native spelling, and there it travels with
    `network_services_keys`, which already classified it correctly.
    """
    tenants = [{"name": "TENANT_A", "vrfs": [{"name": "VRF10"}]}]
    assert classify({"tenants": tenants}) == "NetworkServiceSet"
    assert classify({"network_services": tenants}) == "NetworkServiceSet"


def test_a_node_block_outranks_every_other_category() -> None:
    """Classification is per fragment, first match wins -- a fragment mixing a
    node block with anything else is a NodeSet, whole.

    Written down because it is what AVD's own host_vars do: one file carrying
    `wan_router` alongside `bgp_peer_groups` and `wan_ipsec_profiles` becomes a
    single NodeSet carrying those credentials. Ownership only -- `resolve` never
    reads a kind, so no render changes with it.
    """
    design = {
        "wan_router": {"nodes": [{"name": "wan1"}]},
        "bgp_peer_groups": {"wan_overlay_peers": {"password": "x"}},
        "tenants": [{"name": "TENANT_A"}],
    }
    assert classify(design) == "NodeSet"


def test_a_group_splits_into_one_input_per_category() -> None:
    """Merged as Ansible merges, then split -- so each XR carries one category.

    `cv-pathfinder`'s `group_vars/WAN/` is four files whose author had already
    separated settings, interface profiles, management and tenants. Ansible
    merges a group_vars directory into one namespace, and classifying that whole
    let a single `tenants` key decide the kind for 22.
    """
    parts = _by_category(
        "WAN",
        {
            "l3leaf": {"nodes": [{"name": "leaf1"}]},
            "tenants": [{"name": "TENANT_A"}],
            "aaa_settings": {"local_users": [{"name": "admin"}]},
        },
    )
    assert [(name, kind) for name, kind, _ in parts] == [
        ("WAN", "NodeSet"),
        ("WAN-services", "NetworkServiceSet"),
        ("WAN-settings", "SettingSet"),
    ]
    assert [sorted(design) for _, _, design in parts] == [
        ["l3leaf"], ["tenants"], ["aaa_settings"]
    ]


def test_a_group_of_one_category_keeps_its_bare_name() -> None:
    """No suffix where there is nothing to tell apart -- `NETWORK_SERVICES.yml`
    stays `NETWORK_SERVICES`, not `NETWORK_SERVICES-services`."""
    design = {"tenants": [{"name": "TENANT_A"}]}
    assert _by_category("NETWORK_SERVICES", design) == [
        ("NETWORK_SERVICES", "NetworkServiceSet", design)
    ]


def test_a_host_outside_the_play_is_not_a_device() -> None:
    """`cv-pathfinder` holds `cloudvision`, which is not a switch.

    Its inventory carries the CloudVision API server so `cv_deploy` can reach
    it; `build.yml` runs eos_designs on `hosts: WAN`, and there is no
    `cloudvision.cfg` in the example's intended configs. Declaring it would
    compose a Device for it -- and a declared device with no node type fails the
    whole fabric's render with AVD's `No device type found`, not just its own.

    This is the only host in the 8 bundled examples where the inventory and the
    play disagree, which is why the model collapsed the two lists for so long.
    """
    root = EXAMPLES_ROOT / "cv-pathfinder"
    if not root.is_dir():
        pytest.skip("AVD submodule not initialised")

    declared = {host for inp in inputs_from_inventory(root) for host in inp.declares}

    assert declared, "cv-pathfinder should still declare its WAN devices"
    assert "cloudvision" not in declared, (
        "cloudvision is an API server, not a fabric device -- play_hosts should "
        "have kept it out of the device list"
    )
    assert "pf1" in declared, "the play's own devices must survive the restriction"


def test_unscoped_nodeset_reaches_only_what_it_declares() -> None:
    """An omitted `appliesTo` means the whole fabric -- except on a NodeSet.

    Ansible has no unscoped group_vars file: a node-type block is read by its
    own group. Defaulting a NodeSet to the whole fabric made every migrated
    NodeSet name itself in `appliesTo`, 26 of 26 across AVD's examples.
    """
    inputs = [
        Input("spines", "NodeSet", {"spine": {"nodes": [{"name": "spine1"}]}},
              declares=["spine1"]),
        Input("leaves", "NodeSet", {"l3leaf": {"nodes": [{"name": "leaf1"}]}},
              declares=["leaf1"]),
        Input("base", "SettingSet", {"ntp_settings": {"servers": ["a"]}}),
    ]
    out = resolve(inputs)
    assert "spine" in out["spine1"] and "spine" not in out["leaf1"]
    assert "l3leaf" in out["leaf1"] and "l3leaf" not in out["spine1"]
    # Every other kind still defaults to the whole fabric.
    assert out["spine1"]["ntp_settings"] == out["leaf1"]["ntp_settings"]


def test_a_nodeset_may_be_seen_wider_than_it_declares() -> None:
    """The case the default must not swallow, and it is real.

    `eos_designs-twodc-5stage-clos` has a DC-level NodeSet declaring 4
    super_spines and visible to all 16 devices of that DC. Getting this wrong
    cost 96 hostvar diffs once.
    """
    inputs = [
        Input("dc1", "NodeSet", {"super_spine": {"nodes": [{"name": "ss1"}]}},
              declares=["ss1"], node_sets=["dc1", "dc1-pod1"]),
        Input("dc1-pod1", "NodeSet", {"l3leaf": {"nodes": [{"name": "leaf1"}]}},
              declares=["leaf1"]),
    ]
    out = resolve(inputs)
    assert "super_spine" in out["leaf1"], "a widened NodeSet must still reach the pod"
    assert "l3leaf" not in out["ss1"], "the pod's own block stays in the pod"


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
