"""example/: the Function as it is installed, with its runtime config, before the
Configuration that depends on it; and the provider config a Device's Request takes."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import yaml

from function import push

ROOT = Path(__file__).parent.parent


@pytest.fixture(scope="module")
def by_kind() -> dict[str, dict]:
    docs = yaml.safe_load_all((ROOT / "example" / "functions.yaml").read_text())
    return {doc["kind"]: doc for doc in docs}


def test_the_function_is_the_version_released(by_kind):
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]

    assert by_kind["Function"]["spec"]["package"].endswith(f":v{version}")


def test_the_function_is_the_name_the_compositions_call(by_kind):
    for composition in sorted((ROOT / "apis").glob("*/composition.yaml")):
        steps = yaml.safe_load(composition.read_text())["spec"]["pipeline"]

        assert {step["functionRef"]["name"] for step in steps} == {
            by_kind["Function"]["metadata"]["name"]
        }


def test_the_configuration_depends_on_the_function_installed(by_kind):
    meta = yaml.safe_load((ROOT / "apis" / "crossplane.yaml").read_text())
    function = next(dep for dep in meta["spec"]["dependsOn"] if dep["kind"] == "Function")

    assert f"{function['package']}:{function['version']}" == by_kind["Function"]["spec"]["package"]


def test_the_function_runs_with_the_runtime_config_beside_it(by_kind):
    ref = by_kind["Function"]["spec"]["runtimeConfigRef"]

    assert ref["name"] == by_kind["DeploymentRuntimeConfig"]["metadata"]["name"]


def test_the_provider_config_is_the_default_a_request_takes():
    config = yaml.safe_load((ROOT / "example" / "providerconfig.yaml").read_text())

    assert config["apiVersion"].split("/")[0] == push.REQUEST_API_VERSION.split("/")[0]
    assert (config["kind"], config["metadata"]["name"]) == ("ClusterProviderConfig", "default")
