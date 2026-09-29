"""The corpus tier: every repository in the `avd` submodule, emitted and rendered.

Each example and molecule scenario that commits `intended/structured_configs/` is emitted
by netadopt's own CLI, rendered by the Fabric, and compared with what AVD commits. The
rest are skipped with the reason, never left out: a suite that quietly collects less
reads as one that passed.

A molecule scenario says in molecule.yml how ansible-playbook runs it -- the -i, and
the converge playbook -- and that is read, not guessed; one that borrows either from
another directory is a wrapper around a repository tested on its own.

The render is the slow part, so it runs once per repository and every test reads it.
"""

from __future__ import annotations

import base64
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from function import fabric

AVD = Path(__file__).resolve().parents[2] / "avd" / "ansible_collections" / "arista" / "avd"
MOLECULE_FILE = "molecule.yml"
GOLDEN = Path("intended") / "structured_configs"
NS = "corpus"

# Scenarios whose goldens are no measure of a render, and why.
NOT_COVERAGE = {
    "eos_designs_negative_unit_tests": "its scenarios are meant to fail",
    "eos_designs_unit_tests": "it names a custom Python module that nothing carries",
}


def _repos() -> list[Path]:
    if not (AVD / "examples").is_dir():
        return []
    found = sorted((AVD / "examples").iterdir()) + sorted(
        (AVD / "extensions" / "molecule").iterdir()
    )
    return [path for path in found if path.is_dir()]


def _molecule(path: Path) -> dict[str, str | None]:
    """The -i molecule passes to ansible-playbook, and its converge playbook."""
    file = path / MOLECULE_FILE
    data = yaml.safe_load(file.read_text(encoding="utf-8")) if file.is_file() else None
    if not isinstance(data, dict):
        return {}
    ansible = data.get("ansible") or data.get("provisioner") or {}
    args = ((ansible.get("executor") or {}).get("args") or {}).get("ansible_playbook") or []
    inventory = next(
        (a.split("=", 1)[1] for a in args if isinstance(a, str) and a.startswith("--inventory=")),
        None,
    )
    return {"inventory": inventory, "converge": (ansible.get("playbooks") or {}).get("converge")}


def _skipped(path: Path) -> str | None:
    """Why a repository is no case for the render, or None."""
    if path.name in NOT_COVERAGE:
        return NOT_COVERAGE[path.name]
    for what, value in _molecule(path).items():
        if isinstance(value, str) and ".." in Path(value).parts:
            return f"molecule runs it with the {what} {value}"
    if not (path / GOLDEN).is_dir():
        return f"no {GOLDEN} to compare with"
    if not any((path / GOLDEN).glob("*.yml")):
        # cv_deploy and cv_workflow keep fixtures there, in directories of their own
        return f"no host's structured config in {GOLDEN}, only directories"
    return None


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "repo" not in metafunc.fixturenames:
        return
    repos = _repos()
    if not repos:
        skip = pytest.mark.skip(reason="no AVD checkout: run `git submodule update --init`")
        metafunc.parametrize("repo", [pytest.param(None, marks=skip)], scope="session")
        return
    params = []
    for path in repos:
        reason = _skipped(path)
        marks = [pytest.mark.skip(reason=reason)] if reason else []
        params.append(pytest.param(path, marks=marks, id=path.name))
    metafunc.parametrize("repo", params, scope="session")


def _inventory(repo: Path) -> str | None:
    if told := _molecule(repo).get("inventory"):
        return told
    if (repo / "inventory").is_dir():
        return "inventory/"
    if (repo / "inventory.yml").is_file():
        return "inventory.yml"
    return None


def _playbook(repo: Path) -> str | None:
    for name in (_molecule(repo).get("converge"), "converge.yml", "build.yml"):
        if name and (repo / name).is_file():
            return name
    return None


def _emit(repo: Path) -> list[dict]:
    """What netadopt's CLI emits for the repository's first play, placed in NS."""
    playbook = _playbook(repo)
    if playbook is None:
        pytest.skip("no playbook the repository is built with")
    command = [str(Path(sys.executable).parent / "netadopt"), "avd", "emit", str(repo)]
    command += ["--playbook", playbook]
    if inventory := _inventory(repo):
        command += ["--inventory", inventory]
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    docs = [doc for doc in yaml.safe_load_all(done.stdout) if doc]
    if not any(doc["kind"] == "Fabric" for doc in docs):
        pytest.skip(f"emit wrote no Fabric: {done.stderr.strip().splitlines()[-1:]}")
    for doc in docs:
        doc["metadata"]["namespace"] = NS
    return docs


def _vault_secret(repo: Path, fab: dict) -> list[dict]:
    """The Secret the Fabric names, holding the file the repository's ansible.cfg names."""
    ref = ((fab["spec"].get("vaultPassword") or {}).get("secretRef")) or None
    if ref is None:
        return []
    named = (fab["spec"].get("ansibleCfg") or {}).get("defaults", {}).get("vault_password_file")
    password = (repo / named).read_bytes().strip()
    return [
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": ref["name"], "namespace": NS},
            "data": {ref["key"]: base64.b64encode(password).decode()},
        }
    ]


def answered(fab: dict, docs: list[dict]) -> dict[str, list[dict]]:
    """Crossplane's answer to each of the Fabric's requirements, from `docs`."""
    required = {}
    for key, sel in fabric.requirements(fab).items():
        if "match_name" in sel:
            required[key] = [
                d
                for d in docs
                if d["kind"] == sel["kind"] and d["metadata"]["name"] == sel["match_name"]
            ]
        else:
            required[key] = [
                d
                for d in docs
                if d["kind"] == sel["kind"]
                and all(
                    (d["metadata"].get("labels") or {}).get(k) == v
                    for k, v in sel["match_labels"].items()
                )
            ]
    return required


_RENDERS: dict[Path, tuple] = {}


@pytest.fixture(scope="session")
def emitted(repo: Path) -> tuple[dict, dict[str, list[dict]]]:
    """The Fabric, and every requirement of it answered from what emit wrote."""
    docs = _emit(repo)
    fab = next(doc for doc in docs if doc["kind"] == "Fabric")
    return fab, answered(fab, docs + _vault_secret(repo, fab))


@pytest.fixture(scope="session")
def rendered(repo: Path, emitted) -> fabric.Render:
    """The Fabric's render of the repository, once."""
    if repo not in _RENDERS:
        fab, required = emitted
        _RENDERS[repo] = fabric.render(fab, fabric.gather(fab, required))
    return _RENDERS[repo]


@pytest.fixture(scope="session")
def golden(repo: Path) -> dict[str, dict]:
    """The structured configs AVD commits, by host."""
    return {
        file.stem: yaml.load(file.read_text(encoding="utf-8"), Loader=yaml.CSafeLoader)
        for file in sorted((repo / GOLDEN).glob("*.yml"))
    }
