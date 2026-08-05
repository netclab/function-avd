"""Derive a netclab-chart lab topology from an AVD fabric.

AVD already resolves the cabling: every interface in a rendered structured config
carries `metadata.peer` and `metadata.peer_interface`. So the lab topology is not
a second source of truth to be kept in sync by hand -- it is generated from the
same model that produces the configs, and cannot drift from them.

Usage:
    uv run avd-topology examples/fabric/single-dc-l3ls.yaml \
        --hosts dc1-spine1,dc1-leaf1a > topology.yaml
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

from .engine import render_fabric_design

# netclab-chart derives a Linux veth name as `<release>-<network>-<hash(node)>`
# and fails the install if it cannot fit in 15 characters (see the chart's
# `netclab.vethName`). Network names are therefore kept short and opaque, with
# the readable link written alongside as a comment.
MAX_IFNAME = 15

# No registry, deliberately. The generated topology is committed and fetched at
# a tag by other repositories, so it is read far more often than it is run here
# -- and a machine-local registry address in a published artifact is wrong for
# everyone but the machine that has it. `docker import` + `kind load` names the
# image exactly this way, which is what a reader following netclab-xp's
# documentation ends up with.
#
# Bring-up scripts that do serve cEOS from a registry rewrite this: it is one
# line against a temp copy, and local specifics belong in a script rather than
# in something published. See `scripts/kind-up.sh`.
CEOS_IMAGE = "ceos:4.36.1F"
CEOS_MEMORY = "2Gi"
CEOS_CPU = "1000m"

_ETH = re.compile(r"^Ethernet(\d+)$")


def _lab_interface(eos_name: str) -> str:
    """`Ethernet1` -> `eth1`, which is how cEOS maps its container interfaces."""
    m = _ETH.match(eos_name)
    if not m:
        # Breakouts (`Ethernet3/1`) and management interfaces have no obvious
        # container equivalent; fail rather than guess at a mapping.
        raise ValueError(f"cannot map {eos_name!r} to a container interface")
    return f"eth{m.group(1)}"


def links(configs: dict[str, dict], hosts: list[str]) -> list[tuple[tuple[str, str], tuple[str, str]]]:
    """Every point-to-point link with both ends among `hosts`, deduplicated.

    Each link is described twice -- once from each end -- so the pair is sorted
    and collected into a set.
    """
    seen: set[tuple[tuple[str, str], tuple[str, str]]] = set()
    for host in hosts:
        for iface in configs[host].get("ethernet_interfaces") or []:
            meta = iface.get("metadata") or {}
            peer, peer_iface = meta.get("peer"), meta.get("peer_interface")
            if not peer or not peer_iface or peer not in hosts:
                continue
            ends = tuple(sorted([(host, iface["name"]), (peer, peer_iface)]))
            seen.add(ends)  # type: ignore[arg-type]
    return sorted(seen)


def build_topology(
    configs: dict[str, dict],
    hosts: list[str],
    image: str = CEOS_IMAGE,
    memory: str = CEOS_MEMORY,
    cpu: str = CEOS_CPU,
    release: str = "avd",
) -> tuple[dict, list[str]]:
    """Return (topology, comments) -- comments name each link for the reader."""
    found = links(configs, hosts)
    if not found:
        raise SystemExit(
            f"no links between {hosts}: a lab of unconnected devices proves nothing"
        )

    networks, comments = [], []
    interfaces: dict[str, list[dict]] = {h: [] for h in hosts}
    for n, (a, b) in enumerate(found, start=1):
        name = f"b{n}"
        if len(release) + len(name) + 2 >= MAX_IFNAME:
            raise SystemExit(
                f"release {release!r} + network {name!r} exceed netclab's "
                f"{MAX_IFNAME}-char veth budget"
            )
        networks.append({"name": name})
        comments.append(f"{name}: {a[0]}:{a[1]} <-> {b[0]}:{b[1]}")
        for host, eos_iface in (a, b):
            interfaces[host].append({"name": _lab_interface(eos_iface), "network": name})

    nodes = [
        {
            "name": host,
            "type": "ceos",
            "image": image,
            "memory": memory,
            "cpu": cpu,
            "interfaces": sorted(interfaces[host], key=lambda i: i["name"]),
        }
        for host in hosts
        if interfaces[host]
    ]
    return {"topology": {"networks": networks, "nodes": nodes}}, comments


def main() -> int:
    p = argparse.ArgumentParser(description="Generate a netclab topology from a Fabric XR")
    p.add_argument("fabric", type=Path, help="Fabric XR YAML (examples/fabric/*.yaml)")
    p.add_argument("--hosts", help="comma-separated subset; default: every device")
    p.add_argument("--image", default=CEOS_IMAGE)
    p.add_argument("--memory", default=CEOS_MEMORY)
    p.add_argument("--cpu", default=CEOS_CPU)
    p.add_argument("--release", default="avd", help="helm release name (bounds veth names)")
    args = p.parse_args()

    xr = yaml.safe_load(args.fabric.read_text())
    configs = render_fabric_design(xr["spec"]["design"], xr["spec"]["fabricName"])

    hosts = args.hosts.split(",") if args.hosts else sorted(configs)
    missing = [h for h in hosts if h not in configs]
    if missing:
        raise SystemExit(f"not in fabric {xr['spec']['fabricName']}: {missing}")

    topology, comments = build_topology(
        configs, hosts, args.image, args.memory, args.cpu, args.release
    )
    print(f"# Generated from {args.fabric} by avd-topology -- do not edit.")
    for c in comments:
        print(f"# {c}")
    print(yaml.safe_dump(topology, sort_keys=False, default_flow_style=False), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
