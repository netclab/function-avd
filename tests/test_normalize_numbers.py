"""The protobuf Struct number gotcha.

Crossplane passes resources as protobuf `Struct`, whose only numeric type is
`double`, so a VLAN id `4092` arrives as `4092.0` and pyavd's schema rejects it.
`_normalize_numbers` undoes that -- these pin its edges (bools, fractionals).
"""

from __future__ import annotations

from avd_live_model.composite_fn import _normalize_numbers


def test_whole_number_floats_become_ints() -> None:
    result = _normalize_numbers(4092.0)
    assert result == 4092
    assert isinstance(result, int)


def test_bools_survive() -> None:
    # bool subclasses int in Python, so a careless isinstance check turns True
    # into 1 -- which AVD's schema rejects where it wants a boolean.
    assert _normalize_numbers(True) is True
    assert _normalize_numbers(False) is False


def test_genuine_fractionals_are_left_alone() -> None:
    assert _normalize_numbers(1.5) == 1.5


def test_recurses_through_nested_design() -> None:
    design = {
        "vlans": [{"id": 11.0, "enabled": True}, {"id": 4092.0}],
        "l3leaf": {"node_groups": [{"bgp_as": 65101.0}]},
    }
    assert _normalize_numbers(design) == {
        "vlans": [{"id": 11, "enabled": True}, {"id": 4092}],
        "l3leaf": {"node_groups": [{"bgp_as": 65101}]},
    }
