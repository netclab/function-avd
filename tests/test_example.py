"""example/: the runtime config function-avd is installed with, applied before the
Configuration that installs it; the provider config a Device's Request takes; and a
fabric, as netadopt emits it for the lab."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

from function import push

ROOT = Path(__file__).parent.parent
FABRIC = ROOT / "example" / "single-dc-l3ls.yaml"

# The repository the fabric is emitted from.
REPO = ROOT / "avd" / "ansible_collections" / "arista" / "avd" / "examples" / "single-dc-l3ls"

NETADOPT = Path(sys.executable).parent / "netadopt"
CROSSPLANE_CLI = shutil.which("crossplane")
# Crossplane itself, whose built-in schemas the validation reads.
CROSSPLANE_VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["function-avd"][
    "crossplane"
]


@pytest.fixture(scope="module")
def by_kind() -> dict[str, dict]:
    docs = yaml.safe_load_all((ROOT / "example" / "runtime.yaml").read_text())
    return {doc["kind"]: doc for doc in docs}


@pytest.fixture(scope="module")
def dependency() -> dict:
    meta = yaml.safe_load((ROOT / "apis" / "crossplane.yaml").read_text())
    return next(dep for dep in meta["spec"]["dependsOn"] if dep["kind"] == "Function")


def test_the_configuration_depends_on_the_function_of_its_own_version(dependency):
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]

    assert dependency["version"] == f"v{version}"


def test_the_compositions_call_the_function_by_the_name_crossplane_installs_it_under(dependency):
    # Crossplane names a dependency after its repository, the registry left out:
    # xpkg.ToDNSLabel, which turns each "/" into "-".
    name = dependency["package"].split("/", 1)[1].replace("/", "-")

    for composition in sorted((ROOT / "apis").glob("*/composition.yaml")):
        steps = yaml.safe_load(composition.read_text())["spec"]["pipeline"]

        assert {step["functionRef"]["name"] for step in steps} == {name}


def test_the_image_config_matches_the_function_the_configuration_depends_on(by_kind, dependency):
    matches = by_kind["ImageConfig"]["spec"]["matchImages"]

    assert [match["prefix"] for match in matches] == [dependency["package"]]


def test_the_function_runs_with_the_runtime_config_beside_it(by_kind):
    ref = by_kind["ImageConfig"]["spec"]["runtime"]["configRef"]

    assert ref["name"] == by_kind["DeploymentRuntimeConfig"]["metadata"]["name"]


def test_the_provider_config_is_the_default_a_request_takes():
    config = yaml.safe_load((ROOT / "example" / "providerconfig.yaml").read_text())

    assert config["apiVersion"].split("/")[0] == push.REQUEST_API_VERSION.split("/")[0]
    assert (config["kind"], config["metadata"]["name"]) == ("ClusterProviderConfig", "default")


@pytest.mark.skipif(not REPO.is_dir(), reason="no AVD checkout: run `git submodule update --init`")
def test_the_fabric_is_what_netadopt_emits_for_the_lab(tmp_path):
    # The lab's vars are the Fabric's extraVars, as `emit -e` carried them; what
    # `netadopt avd lab` writes is netadopt's to test.
    fabric = next(doc for doc in yaml.safe_load_all(FABRIC.read_text()) if doc["kind"] == "Fabric")
    lab_vars = tmp_path / "lab-vars.yml"
    lab_vars.write_text(yaml.safe_dump(fabric["spec"]["extraVars"], sort_keys=False))
    emit = [NETADOPT, "avd", "emit", REPO, "--playbook", "build.yml", "-e", f"@{lab_vars}"]

    done = subprocess.run(emit, capture_output=True, text=True, check=True)

    assert done.stdout == FABRIC.read_text()


@pytest.mark.skipif(CROSSPLANE_CLI is None, reason="needs the crossplane CLI")
def test_the_fabric_validates_against_the_xrds():
    xrds = ",".join(str(xrd) for xrd in sorted((ROOT / "apis").glob("*/xrd.yaml")))
    command = [CROSSPLANE_CLI, "resource", "validate", xrds, str(FABRIC)]
    command += [
        f"--crossplane-image=xpkg.crossplane.io/crossplane/crossplane:{CROSSPLANE_VERSION}",
        "--error-on-missing-schemas",
    ]

    done = subprocess.run(command, capture_output=True, text=True, check=False)

    assert done.returncode == 0, done.stdout + done.stderr
