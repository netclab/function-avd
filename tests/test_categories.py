"""The kind a key belongs to is AVD's statement, not ours.

`function.kinds` classifies a top-level eos_designs key two ways, and this
guards both against upstream moving under us:

* the **generated** part -- which key names exist at all -- comes from pyavd's
  public schema at runtime, so a family gaining a member (upstream added
  `cameras` to `connected_endpoints_keys`) needs no edit here. Nothing to guard;
  it cannot drift.
* the **named** part -- the handful of keys that are not settings -- is a
  literal, because AVD publishes that categorisation only as documentation
  metadata. This test reads `documentation_options.table` out of AVD's own
  schema and requires the two to agree in both directions.

Offline, and reads the `avd` submodule the same way the golden tests do.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from function.kinds import Vocabulary, kind_of

SCHEMA = Path("avd/python-avd/pyavd/_eos_designs/schema/eos_designs.schema.yml")

#: AVD's documentation table -> the kind that owns it. The one editorial step,
#: and it is made on AVD's names rather than on any key's content.
TABLE_KIND = {
    "node-type-structure": "NodeSet",
    "type-setting": "NodeSet",
    "node-type-keys": "NodeSet",
    "node-type-l3-interfaces-configuration": "NodeSet",
    "network-services": "NetworkServiceSet",
    "network-services-l2vlans-settings": "NetworkServiceSet",
    "network-services-vrfs-settings": "NetworkServiceSet",
    "evpn-vlan-bundles": "NetworkServiceSet",
    "connected-endpoints": "ConnectedEndpointSet",
    "connected-endpoints-keys": "ConnectedEndpointSet",
    "default-connected-endpoints-description": "ConnectedEndpointSet",
    "default-network-ports-description": "ConnectedEndpointSet",
}

#: Keys AVD tags with no table at all, placed here by this repo. Listed so the
#: test states them rather than silently tolerating them.
UNTAGGED = {
    "port_profiles": "ConnectedEndpointSet",
    "network_ports": "ConnectedEndpointSet",
    "network_services_keys": "NetworkServiceSet",
}


def _schema() -> dict:
    if not SCHEMA.is_file():
        pytest.skip(f"{SCHEMA} missing -- run `git submodule update --init`")
    return yaml.safe_load(SCHEMA.read_text())


def _tables() -> dict[str, str]:
    """Top-level key -> AVD's documentation table."""
    return {
        key: (body.get("documentation_options") or {}).get("table") or ""
        for key, body in (_schema().get("keys") or {}).items()
    }


def test_every_key_avd_categorises_lands_in_that_kind() -> None:
    """AVD tags a key; we must agree. This is the direction that catches a
    *new* upstream key we have never seen."""
    vocabulary = Vocabulary.default()
    wrong = {
        key: (kind_of(key, None, vocabulary), TABLE_KIND[table])
        for key, table in _tables().items()
        if table in TABLE_KIND
    }
    wrong = {k: v for k, v in wrong.items() if v[0] != v[1]}
    assert not wrong, f"kind_of disagrees with AVD's own table: {wrong}"


def test_no_key_is_promoted_out_of_settings_without_avd_saying_so() -> None:
    """The other direction: nothing is quietly special-cased. A key we do not
    call a setting must be one AVD categorises, a dynamic key name, or listed in
    UNTAGGED with a reason."""
    vocabulary = Vocabulary.default()
    dynamic = vocabulary.node_types | vocabulary.network_services | vocabulary.connected_endpoints
    tables = _tables()
    unexplained = {
        key: kind_of(key, None, vocabulary)
        for key in tables
        if kind_of(key, None, vocabulary) != "SettingSet"
        and key not in dynamic
        and tables[key] not in TABLE_KIND
        and key not in UNTAGGED
    }
    assert not unexplained, f"promoted out of SettingSet with nothing backing it: {unexplained}"


def test_untagged_placements_are_still_untagged_upstream() -> None:
    """If AVD starts tagging one of these, the entry moves to TABLE_KIND and
    stops being this repo's opinion."""
    tables = _tables()
    now_tagged = {k: tables[k] for k in UNTAGGED if tables.get(k)}
    assert not now_tagged, f"AVD now categorises these; move them to TABLE_KIND: {now_tagged}"


def test_the_dynamic_families_come_from_pyavd_not_from_a_literal() -> None:
    """The defaults are read, so they cannot be short. `cameras` is the case
    that proves it: it exists upstream and the literal this replaced lacked it."""
    vocabulary = Vocabulary.default()
    schema_defaults = {
        source: {
            str(entry[field])
            for entry in ((_schema()["keys"].get(source) or {}).get("default") or [])
            if isinstance(entry, dict) and entry.get(field)
        }
        for source, field in (
            ("node_type_keys", "key"),
            ("network_services_keys", "name"),
            ("connected_endpoints_keys", "key"),
        )
    }
    assert schema_defaults["node_type_keys"] <= vocabulary.node_types
    assert schema_defaults["network_services_keys"] <= vocabulary.network_services
    assert schema_defaults["connected_endpoints_keys"] <= vocabulary.connected_endpoints
