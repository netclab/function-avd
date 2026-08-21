"""An explicit null survives the trip through an object that cannot hold one.

Offline. The property under test is not "encode then decode returns the input"
-- that passes on an encoder that does nothing at all. It is that the value
survives *the pruning*, so every round trip here prunes in the middle, the way
an API server does. Measured against a real one: a null map value is dropped, a
null list item is not.
"""

from __future__ import annotations

import pytest

from function import nulls
from function.kinds import Input, resolve
from function.migrate import Fabric, MigrationError, to_manifests


def pruned(node):
    """What an API server stores: map values that are null are gone."""
    if isinstance(node, dict):
        return {k: pruned(v) for k, v in node.items() if v is not None}
    if isinstance(node, list):
        return [None if v is None else pruned(v) for v in node]
    return node


def through_apiserver(design):
    """A design as it comes back out of an object it was applied to."""
    return nulls.restored(pruned(nulls.encoded(design)))


def test_a_null_map_value_survives_the_pruning():
    # twodc's own shape: a fabric-wide default, cancelled on one link.
    design = {
        "p2p_uplinks_qos_profile": "QOS-PROFILE",
        "l3_edge": {"p2p_links": [{"id": 1, "qos_profile": None, "mtu": 1499}]},
    }
    assert through_apiserver(design) == design


def test_the_encoder_is_what_makes_it_survive():
    """The same document without the marker loses the key -- the guard's point."""
    design = {"l3_edge": {"p2p_links": [{"id": 1, "qos_profile": None}]}}
    assert pruned(design) == {"l3_edge": {"p2p_links": [{"id": 1}]}}


def test_a_null_list_item_is_left_alone():
    """It survives on its own, so marking it would change what AVD reads."""
    design = {"items": ["a", None, "b"]}
    assert nulls.encoded(design) == design
    assert through_apiserver(design) == design


def test_nothing_else_is_touched():
    design = {"none_the_value": "none", "empty": "", "zero": 0, "no": False}
    assert nulls.encoded(design) == design
    assert through_apiserver(design) == design


def test_restoring_happens_after_layering():
    """A later input may overwrite a null with a value, or a value with a null."""
    inputs = [
        Input(name="fabric", kind="SettingSet", design={"qos": "P1"}, all_devices=True,
              declares=["s1"]),
        Input(name="site", kind="SettingSet", design=nulls.encoded({"qos": None}),
              all_devices=True),
    ]
    assert resolve(inputs) == {"s1": {"qos": None}}

    inputs[0], inputs[1] = inputs[1], inputs[0]
    inputs[0].declares, inputs[1].declares = ["s1"], []
    assert resolve(inputs) == {"s1": {"qos": "P1"}}


def test_a_document_already_carrying_the_marker_is_refused():
    """Encoding it would make somebody's string indistinguishable from a null."""
    fabric = Fabric(
        name="f",
        devices=("s1",),
        inputs=[Input(name="settings", kind="SettingSet",
                      design={"description": nulls.MARKER}, all_devices=True)],
    )
    with pytest.raises(MigrationError, match="already contains"):
        to_manifests(fabric)
