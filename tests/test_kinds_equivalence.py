"""The input-kind model resolves exactly as Ansible does.

The reference side is **real Ansible** -- `ansible-inventory --list`, run by
:mod:`function.ansible_cli`. That matters more than it sounds. The previous
version of this test compared two of our own readers against each other, so a
source neither read vanished from both sides and the comparison agreed. Four
such gaps sat behind a green result and surfaced only when a render disagreed
with AVD's own golden: `host_vars/*.yaml`, `host_vars/<host>/` directories,
inline group `vars:` blocks, and ansible-vault.

Byte equality is the assertion. It is stricter than necessary -- a hostvar AVD
never reads cannot change a rendered config -- and that is deliberate: it fails
before a render can hide a difference.

The 8 bundled examples run by default. The molecule corpus is behind
``-m corpus``: it reaches 501 devices in one play and 71 plays in one scenario,
and every one of them costs an ansible subprocess.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from function.ansible_cli import read_inventory
from function.kinds import Input, Vocabulary, by_kind, classify, resolve
from function.migrate import (
    Fabric,
    MigrationError,
    _discover,
    drop_description_templates,
    migrate,
    to_manifests,
    unsupported,
)

AVD = Path("avd/ansible_collections/arista/avd")
EXAMPLES_ROOT = AVD / "examples"
MOLECULE_ROOT = AVD / "extensions" / "molecule"
COLLECTIONS = Path("avd").resolve()

EXAMPLES = _discover(EXAMPLES_ROOT) if EXAMPLES_ROOT.is_dir() else []
MOLECULE = _discover(MOLECULE_ROOT) if MOLECULE_ROOT.is_dir() else []

# Scenarios with no eos_designs play at all. Not deferrals -- correct answers:
# the three `eos_cli_config_gen` scenarios carry structured config directly and
# render at the device layer, and cv_deploy/cv_workflow push what was built.
NO_DESIGN_PLAY = {
    "cv_deploy", "cv_workflow", "eos_cli_config_gen",
    "eos_cli_config_gen_deprecated_vars", "eos_cli_config_gen_negative_unit_tests",
}

# Renders that still meet an AVD feature this path does not carry. Expected-
# failure semantics: one that starts passing is reported, so a deferral cannot
# rot silently. **All eight examples render clean**, so this covers the molecule
# corpus only -- and every entry is one of three causes, not eight.
DEFERRED_RENDER: dict[str, str] = {}

DEFERRED_RENDER_CORPUS = {
    # pyavd implements no Jinja templating: `get_device_structured_config` passes
    # `templar=None` and the call raises `NotImplementedError`. Neither public
    # entry point accepts a templar, so carrying the .j2 files on an XR would
    # change nothing -- nothing would read them. Upstream, not ours.
    "ansible_only": "custom ip_addressing template -- pyavd implements no Jinja templating",
    "evpn_underlay_ebgp_overlay_ebgp": "custom interface_descriptions templates -- "
                                       "pyavd implements no Jinja templating",
    # Two blockers, not one: `pool_manager` needs a pool that survives
    # reconciliation, and the design also pins .j2 addressing. The *migrated*
    # design always fails here, because it faithfully carries both.
    # ⚠ Both are solved and proven in `tests/test_avd_compat.py`, which renders
    # this scenario clean against AVD's golden with the pool supplied and the
    # templates replaced by the class AVD's schema offers instead. What is left
    # is somewhere for a Fabric to *keep* the pool.
    "eos_designs-twodc-5stage-clos": "pool_manager + .j2 addressing -- see test_avd_compat",
    # Code, not data: a function image is immutable and must not load arbitrary
    # Python. ⚠ And this scenario is not a fabric -- 59 unrelated feature groups
    # under one play, with six different `fabric_name` values.
    "eos_designs_unit_tests": "templates.*.python_module -- code, not data",
}


def _differences(path: str, ours: object, golden: object, out: list[str]) -> None:
    """Collect readable differences between a rendered config and its golden."""
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


def _fabrics(root: Path) -> tuple[list[Fabric], dict[str, dict]]:
    inv = read_inventory(root, collections=COLLECTIONS)
    return migrate(root, collections=COLLECTIONS, inv=inv), inv.hostvars


def _assert_equivalent(root: Path) -> None:
    if root.name in NO_DESIGN_PLAY:
        with pytest.raises(MigrationError):
            migrate(root, collections=COLLECTIONS)
        return

    fabrics, hostvars = _fabrics(root)
    assert fabrics, f"{root.name}: no fabric produced"
    for fabric in fabrics:
        got = resolve(fabric.inputs)
        want = {device: hostvars.get(device, {}) for device in fabric.devices}
        differing = sorted(
            f"{host}.{key}"
            for host in set(got) | set(want)
            for key in set(got.get(host, {})) | set(want.get(host, {}))
            if got.get(host, {}).get(key) != want.get(host, {}).get(key)
        )
        assert set(got) == set(want), (
            f"{fabric.name}: device sets differ "
            f"(missing {sorted(set(want) - set(got))[:5]}, "
            f"extra {sorted(set(got) - set(want))[:5]})"
        )
        assert not differing, f"{fabric.name}: {len(differing)} differ: {differing[:5]}"


@pytest.mark.parametrize("root", EXAMPLES, ids=lambda p: p.name)
def test_resolves_identically_to_ansible(root: Path) -> None:
    _assert_equivalent(root)


@pytest.mark.corpus
@pytest.mark.parametrize("root", MOLECULE, ids=lambda p: p.name)
def test_resolves_identically_to_ansible_over_the_molecule_corpus(root: Path) -> None:
    _assert_equivalent(root)


def _assert_renders(root: Path, deferrals: dict[str, str]) -> None:
    import yaml

    from function.engine import render_structured_configs

    golden = root / "intended" / "structured_configs"
    if not golden.is_dir():
        pytest.skip(f"{root.name} ships no golden")

    fabrics, _ = _fabrics(root)
    total: list[str] = []
    try:
        for fabric in fabrics:
            rendered = render_structured_configs(resolve(fabric.inputs))
            for hostname, structured in sorted(rendered.items()):
                target = golden / f"{hostname}.yml"
                if target.is_file():
                    _differences(hostname, structured,
                                 yaml.safe_load(target.read_text()) or {}, total)
    except Exception as err:  # noqa: BLE001 - a render failure is a difference too
        total.append(f"{type(err).__name__}: {err}")

    if root.name in deferrals:
        assert total, (
            f"{root.name} renders clean now -- drop its deferral "
            f"(was: {deferrals[root.name]})"
        )
        return
    assert not total, f"{root.name}: {len(total)} differences: {total[:5]}"


@pytest.mark.parametrize("root", EXAMPLES, ids=lambda p: p.name)
def test_render_reproduces_golden(root: Path) -> None:
    """Rendered configs still match the checked-in golden.

    Redundant as a check on the model -- matching hostvars render identically --
    and that is not what it is for. It guards pyavd itself changing: an AVD
    upgrade slips past resolution equivalence and fails here.
    """
    _assert_renders(root, DEFERRED_RENDER)


@pytest.mark.corpus
@pytest.mark.parametrize("root", MOLECULE, ids=lambda p: p.name)
def test_render_reproduces_golden_over_the_molecule_corpus(root: Path) -> None:
    if root.name in NO_DESIGN_PLAY:
        pytest.skip("no play runs eos_designs")
    if root.name == "eos_designs_negative_unit_tests":
        # Every fixture is deliberately invalid and every play asserts a specific
        # failure message. Rendering it clean would mean AVD's own negative
        # suite had stopped working.
        pytest.skip("AVD's negative corpus -- these are meant to fail")
    _assert_renders(root, DEFERRED_RENDER_CORPUS)


def test_corpus_is_not_empty() -> None:
    # The submodule is optional in a fresh worktree; an empty parametrisation
    # would make this whole file pass while testing nothing.
    assert len(EXAMPLES) == 8, f"expected AVD's 8 examples, found {len(EXAMPLES)}"


def test_the_migration_refuses_an_order_it_cannot_stand_behind(monkeypatch) -> None:
    """The safety property, exercised rather than described.

    Group order (depth, name) is the one rule the Ansible CLI does not print, so
    the migration layers the fragments with it and compares the result against
    `ansible-inventory --list`. Break the order and nothing is emitted.

    ⚠ `campus-fabric` specifically, and the reason is worth keeping: reversing
    the order changes **nothing** in `single-dc-l3ls` or `dual-dc-l3ls`, because
    no two fragments there set the same key for the same device. Precedence is
    load-bearing in exactly two of AVD's eight examples -- `campus-fabric`'s
    role-level `aaa_settings` and `cv-pathfinder`'s `ipv4_acls`. A guard written
    against either of the other six would pass while proving nothing.
    """
    root = EXAMPLES_ROOT / "campus-fabric"
    if not root.is_dir():
        pytest.skip("AVD submodule not initialised")

    import function.migrate as migrate_module

    original = migrate_module._fragments

    def reversed_order(inv, devices):
        return list(reversed(original(inv, devices)))

    monkeypatch.setattr(migrate_module, "_fragments", reversed_order)
    with pytest.raises(MigrationError) as refused:
        migrate(root, collections=COLLECTIONS)

    # Two gates catch a wrong order, and the earlier one is the more useful:
    # reading a resolved value back off the devices where a fragment wins finds
    # `aaa_settings` disagreeing and names it, before the layering comparison
    # gets to count differences.
    assert "aaa_settings" in str(refused.value), str(refused.value)


def test_a_host_outside_the_play_is_not_a_device() -> None:
    """`cv-pathfinder` holds `cloudvision`, which is not a switch.

    Its inventory carries the CloudVision API server so `cv_deploy` can reach
    it; `build.yml` runs eos_designs on `hosts: WAN`. Declaring it would compose
    a Device for it -- and a declared device with no node type fails the whole
    fabric's render with AVD's `No device type found`, not just its own.
    """
    root = EXAMPLES_ROOT / "cv-pathfinder"
    if not root.is_dir():
        pytest.skip("AVD submodule not initialised")

    fabrics = migrate(root, collections=COLLECTIONS)
    devices = {device for fabric in fabrics for device in fabric.devices}
    assert "cloudvision" not in devices, "an API server is not a fabric device"
    assert "pf1" in devices, "the play's own devices must survive the restriction"


def test_manifests_carry_every_input_in_requires_order() -> None:
    fabric = Fabric(
        name="f", devices=("leaf1",), fabric_name="FABRIC",
        inputs=[
            Input("leaves", "NodeSet", {"l3leaf": {"nodes": [{"name": "leaf1"}]}},
                  declares=["leaf1"]),
            Input("base", "SettingSet", {"ntp_settings": {}}, all_devices=True),
        ],
    )
    manifests = to_manifests(fabric, namespace="avd")
    assert [m["kind"] for m in manifests] == ["NodeSet", "SettingSet", "Fabric"]
    assert manifests[-1]["spec"]["requires"] == [
        {"kind": "NodeSet", "name": "leaves", "namespace": "avd"},
        {"kind": "SettingSet", "name": "base", "namespace": "avd"},
    ]
    assert manifests[0]["spec"]["declares"] == ["leaf1"]
    assert "appliesTo" not in manifests[0]["spec"], "a NodeSet seen by what it declares says nothing"
    assert manifests[1]["spec"]["appliesTo"] == {"all": True}


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
    """
    tenants = [{"name": "TENANT_A", "vrfs": [{"name": "VRF10"}]}]
    assert classify({"tenants": tenants}) == "NetworkServiceSet"
    assert classify({"network_services": tenants}) == "NetworkServiceSet"


def test_a_bare_type_is_node_content() -> None:
    """`type` is the commonest key in AVD's whole corpus -- 639 fragments -- and
    it says which node-type block a device belongs to. AVD gives it a table of
    its own (`type-setting`). A group whose only variable is `type` is the
    purest NodeSet there is, and calling it a setting is what forced the
    migration to invent synthetic `<group>-devices` NodeSets beside it.
    """
    assert classify({"type": "spine"}) == "NodeSet"
    assert by_kind({"type": "spine", "ntp_settings": {}}) == {
        "NodeSet": {"type": "spine"},
        "SettingSet": {"ntp_settings": {}},
    }


def test_a_fragment_is_split_by_category_not_won_by_one() -> None:
    """Merged as Ansible merges, then split -- so each XR carries one category.

    `cv-pathfinder`'s `group_vars/WAN/` is four files whose author had already
    separated settings, interface profiles, management and tenants. Ansible
    merges a group_vars directory into one namespace, so the fragment arrives
    carrying all four -- and one `tenants` key among 22 others must not decide
    what the other 21 are.
    """
    parts = by_kind({
        "l3leaf": {"nodes": [{"name": "leaf1"}]},
        "tenants": [{"name": "TENANT_A"}],
        "aaa_settings": {"local_users": [{"name": "admin"}]},
    })
    assert list(parts) == ["NodeSet", "NetworkServiceSet", "SettingSet"]
    assert [sorted(design) for design in parts.values()] == [
        ["l3leaf"], ["tenants"], ["aaa_settings"]
    ]


def test_a_custom_dynamic_key_is_classified_when_its_generator_travels_along() -> None:
    """The open-kinds limit, and the half of it that is not a limit.

    Top-level key names come from the document's own content, so no static map
    can name them all. But a fragment carrying its own `network_services_keys`
    says what its keys mean, and then they are classifiable.
    """
    design = {"network_services_keys": [{"name": "tenant_a"}], "tenant_a": [{"name": "T"}]}
    assert by_kind(design) == {"NetworkServiceSet": design}
    # Without the generator there is nothing to read it by.
    assert by_kind({"tenant_a": [{"name": "T"}]}) == {"SettingSet": {"tenant_a": [{"name": "T"}]}}


def test_the_endpoint_family_is_read_from_pyavd_not_from_a_literal() -> None:
    """`cameras` is the case that proves it: AVD ships it in
    `connected_endpoints_keys`, and the hand-written list this replaced never
    grew it. Nothing here names the members."""
    assert "cameras" in Vocabulary.default().connected_endpoints
    assert classify({"cameras": [{"name": "cam1"}]}) == "ConnectedEndpointSet"


def test_unscoped_nodeset_reaches_only_what_it_declares() -> None:
    """An omitted `appliesTo` means the whole fabric -- except on a NodeSet.

    Ansible has no unscoped group_vars file: a node-type block is read by its
    own group.
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
    assert out["spine1"]["ntp_settings"] == out["leaf1"]["ntp_settings"]


def test_a_nodeset_may_be_seen_wider_than_it_declares() -> None:
    """`eos_designs-twodc-5stage-clos` has a DC-level NodeSet declaring 4
    super_spines and visible to all 16 devices of that DC."""
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
            "leaves", "NodeSet",
            {"l3leaf": {"nodes": [{"name": "leaf1"}, {"name": "ghost"}]}},
            node_sets=["leaves"], declares=["leaf1"],
        )
    ]
    assert set(resolve(inputs)) == {"leaf1"}


def test_only_description_templates_are_droppable() -> None:
    """Both are `.j2` paths pyavd cannot honour; only one is safe to lose.

    An `interface_descriptions` template decides a `description` string -- on
    `evpn_underlay_ebgp_overlay_ebgp`, dropping them renders all 16 devices and
    differs from AVD's golden in 168 places, every one of them a `description`.
    An `ip_addressing` template decides an address: `eos_designs-twodc-5stage-clos`
    computes its P2P uplink IPs that way. Dropping that would emit a different
    network without saying so, which is why one flag cannot cover both.
    """
    design = {
        "node_type_keys": [{
            "key": "spine",
            "interface_descriptions": {"underlay_ethernet_interfaces": "d/eth.j2"},
            "ip_addressing": {"p2p_uplinks_ip": "a/p2p.j2"},
        }],
    }
    assert unsupported(design) == {
        "interface_descriptions": ["node_type_keys.[0].interface_descriptions."
                                   "underlay_ethernet_interfaces"],
        "ip_addressing": ["node_type_keys.[0].ip_addressing.p2p_uplinks_ip"],
    }

    removed = drop_description_templates(design)
    assert removed == ["node_type_keys.[0].interface_descriptions."
                       "underlay_ethernet_interfaces"]
    entry = design["node_type_keys"][0]
    assert "interface_descriptions" not in entry, "an emptied key is removed, not left bare"
    assert entry["ip_addressing"] == {"p2p_uplinks_ip": "a/p2p.j2"}, (
        "addressing must survive the flag that drops descriptions"
    )


def test_custom_python_modules_are_reported_and_never_dropped() -> None:
    """Code, not data. A function image is immutable and loads no arbitrary
    Python, so this cannot travel and cannot be quietly discarded either."""
    design = {"templates": {"ip_addressing": {"python_module": "custom_ip_addressing"}}}
    assert unsupported(design) == {
        "python_module": ["templates.ip_addressing.python_module"]
    }
    assert drop_description_templates(design) == []


def test_a_migration_says_what_it_could_not_carry() -> None:
    """Reported without the flag too -- someone migrating a real inventory has
    to learn this from the tool, not from a diff on a device."""
    root = MOLECULE_ROOT / "evpn_underlay_ebgp_overlay_ebgp"
    if not root.is_dir():
        pytest.skip("AVD submodule not initialised")

    fabric = migrate(root, collections=COLLECTIONS)[0]
    assert any("interface_descriptions" in note for note in fabric.notes), fabric.notes
    assert all("dropped" not in note for note in fabric.notes), (
        "nothing may be dropped unless asked"
    )

    dropped = migrate(root, collections=COLLECTIONS, drop_descriptions=True)[0]
    assert any(note.startswith("dropped ") for note in dropped.notes), dropped.notes
