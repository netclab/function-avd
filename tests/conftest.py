"""What the tests share: AVD's own goldens, from the `avd` submodule."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

AVD = Path(__file__).parent.parent / "avd" / "ansible_collections" / "arista" / "avd"

Golden = Callable[[str, str], tuple[dict, str]]


@pytest.fixture(scope="session")
def golden() -> Golden:
    """A host's structured config and eos.cfg, as AVD commits them under `repo`."""
    if not (AVD / "examples").is_dir():
        pytest.skip("the avd submodule is not checked out")

    def read(repo: str, host: str) -> tuple[dict, str]:
        intended = AVD / repo / "intended"
        structured = yaml.safe_load((intended / "structured_configs" / f"{host}.yml").read_text())
        return structured, (intended / "configs" / f"{host}.cfg").read_text()

    return read
