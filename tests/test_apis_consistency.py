"""The published API and the code that serves it do not drift apart.

Offline. Cheap checks over `apis/`, each guarding a failure that is silent:

* a new input kind gets an XRD but `fn.py` never learns to reconcile it (or the
  reverse), and the XR sits unready with "unsupported composite kind";
* an XRD ships without `categories`, so `kubectl get netclab` does not list it --
  the exact defect this repo carried in Fabric and Device until it was found by
  running the command, not by reading the file;
* an XRD points `defaultCompositionRef` at a Composition that is not there, or at
  one built for a different kind, so nothing selects it.

None of these break a build. They break in a cluster, one release later.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from function.kinds import KINDS

APIS = Path("apis")
# Fabric and Device are composed, not collected -- they are not input kinds.
COMPOSED_KINDS = {"Fabric", "Device"}


def _xrds() -> dict[str, dict]:
    return {p.parent.name: yaml.safe_load(p.read_text()) for p in sorted(APIS.glob("*/xrd.yaml"))}


XRDS = _xrds()


def test_apis_directory_is_not_empty() -> None:
    assert len(XRDS) >= 6, f"expected the six XRDs, found {sorted(XRDS)}"


@pytest.mark.parametrize("name", sorted(XRDS), ids=str)
def test_xrd_declares_categories(name: str) -> None:
    """Every XRD is reachable by `kubectl get netclab` and `kubectl get crossplane`."""
    names = XRDS[name]["spec"]["names"]
    assert names.get("categories") == ["crossplane", "netclab"], (
        f"{name}: categories are {names.get('categories')!r}; netclab-xp's twelve "
        f"XRDs all carry ['crossplane', 'netclab'] and these must match"
    )


@pytest.mark.parametrize("name", sorted(XRDS), ids=str)
def test_xrd_has_its_composition(name: str) -> None:
    """`defaultCompositionRef` resolves, and to a Composition for this kind."""
    xrd = XRDS[name]["spec"]
    wanted = xrd["defaultCompositionRef"]["name"]
    composition = yaml.safe_load((APIS / name / "composition.yaml").read_text())
    assert composition["metadata"]["name"] == wanted
    assert composition["spec"]["compositeTypeRef"]["kind"] == xrd["names"]["kind"]


def test_input_kinds_match_the_function() -> None:
    """The XRDs that exist and the kinds fn.py reconciles are the same set."""
    from_apis = {x["spec"]["names"]["kind"] for x in XRDS.values()} - COMPOSED_KINDS
    assert from_apis == set(KINDS), (
        f"apis/ serves {sorted(from_apis)} but function.kinds.KINDS is "
        f"{sorted(KINDS)} -- fn.py would answer 'unsupported composite kind'"
    )


def test_fabric_requires_accepts_every_input_kind_and_secret() -> None:
    """A Fabric can name each input kind, plus a Secret carrying credentials.

    Secret is in the enum from the first version deliberately: adding it later
    would be a schema change to a released API, and the mechanism it enables --
    a Secret layered like any other input -- needs no other schema footprint.
    """
    spec = XRDS["fabric"]["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
    enum = spec["properties"]["spec"]["properties"]["requires"]["items"]["properties"]["kind"][
        "enum"
    ]
    assert set(enum) == set(KINDS) | {"Secret"}
