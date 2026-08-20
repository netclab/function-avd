"""AVD behaviours pyavd cannot reach, written as the classes AVD asks for.

pyavd implements **no Jinja templating**: `get_device_structured_config` passes
`templar=None` and the call raises `NotImplementedError`, and no public entry
point accepts a templar. So a design pinning a `.j2` path cannot render here,
wherever the file is carried.

The same schema blocks — `node_type_keys[].ip_addressing` and
`.interface_descriptions` — take `python_module` / `python_class_name` instead,
and **that route pyavd supports**: `load_python_class` imports the module by
dotted path and checks it against the public base class. A module shipped inside
this package is importable by dotted path, so pointing a design at
`function.avd_compat` loads no arbitrary code — it loads ours.

This is not a general answer. It reproduces one specific, published scheme.
Anyone whose fabric uses a template of their own writes their own class and
builds their own function image on this one.
"""

from __future__ import annotations

import ipaddress

from pyavd.api.ip_addressing import AvdIpAddressing


class AvdIpAddressingV2Spine(AvdIpAddressing):
    """AVD v2.x spine-to-super-spine P2P addressing.

    A transcription of the two templates `eos_designs-twodc-5stage-clos` pins,
    which say what they are: *"In AVD v2.x the spine to super-spine links used
    this special IP addressing scheme. This file may still be used by older
    inventories."*

    ⚠ The comment describes where they came from, not that they are inert. The
    scheme divides the uplink pool by `max_uplink_switches` so that adding a
    spine does not move existing addresses — which is why that fabric's golden
    puts a spine's two super-spine uplinks 64 apart rather than adjacent. AVD's
    native algorithm packs them contiguously and the two are not interchangeable.

    Everything else falls through to `AvdIpAddressing`, so a fabric selecting
    this class changes only its P2P uplinks.
    """

    def _v2_p2p(self, uplink_switch_index: int, last: int) -> str:
        pool = ipaddress.ip_network(self._uplink_ipv4_pool, strict=False)
        offset = (self._id - 1) % self._max_parallel_uplinks
        index = (
            (pool.num_addresses // self._max_uplink_switches) * int(uplink_switch_index)
            + ((self._id - 1) * self._max_parallel_uplinks + offset) * 2
            + last
        )
        return str(pool.network_address + index)

    def p2p_uplinks_ip(self, uplink_switch_index: int) -> str:
        return self._v2_p2p(uplink_switch_index, 1)

    def p2p_uplinks_peer_ip(self, uplink_switch_index: int) -> str:
        return self._v2_p2p(uplink_switch_index, 0)
